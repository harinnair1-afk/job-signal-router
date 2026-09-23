#!/usr/bin/env python3
"""
job-signal-router — deterministic scoring/routing engine + optional LLM rationale pass.

Formalizes an ad hoc targeting workflow (originally run manually inside Claude sessions
against ~100+ tracked companies) into a standalone, testable pipeline:

    CSV in -> filter/score/tier (pure Python, no LLM) -> validate against schema.json
           -> optional: one narrow LLM call per record to fill "rationale"
           -> ranked digest out (JSON + Markdown)

Design intent: keep every judgment call that can be made deterministically OUT of the LLM.
The model is used for exactly one thing — writing a defensible one-line rationale — under a
schema-constrained, narrowly-scoped system prompt (see system_prompt.md). This mirrors how
the underlying targeting logic actually runs today as a Claude Skill, just extracted into a
portable, versioned, independently runnable form.

Usage:
    python router.py sample_input.csv --top 5
    python router.py sample_input.csv --top 5 --use-llm   # requires ANTHROPIC_API_KEY
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

FUNCTION_MAP = {
    "strategy": "Strategy",
    "gtm": "GTM",
    "pmm": "PMM",
    "bizdev": "BizDev",
    "bd": "BizDev",
    "product": "Product",
    "pm": "Product",
    "cs": "CS",
    "finance": "Finance",
}

BASELINE_MAP = {
    "Strategy": "Strategy",
    "GTM": "PMM",
    "PMM": "PMM",
    "BizDev": "BD",
    "Product": "PM",
    "CS": "CS",
    "Finance": "Finance",
}

SENIORITY_MAP = {
    "intern": "Intern",
    "associate": "Associate",
    "manager": "Target",
    "senior": "Target",
    "senior manager": "Target",
    "director": "Director",
    "vp": "VP+",
    "svp": "VP+",
    "evp": "VP+",
    "c-level": "VP+",
}

TIER_1_COMPANIES = {
    "anthropic", "openai", "stripe", "doordash", "playstation", "sony",
    "rockstar games", "take-two interactive", "roblox", "epic games",
    "coinbase", "robinhood", "asana", "netflix", "riot games",
}

EXCLUDED_FUNCTIONS = {"engineering", "data science", "legal", "accounting"}

MIN_FRESHNESS_DAYS = 3          # priority 1/2 requires posted <= this many days ago
KNOCKOUT_MAX_SENIORITY = {"VP+"}  # seniorities that are hard blockers by default


@dataclass
class Opportunity:
    company: str
    role_title: str
    function: str
    seniority: str
    location: str
    posted_days_ago: int
    salary_range: str = ""
    fit_score: float = 0.0
    priority_tier: int = 5
    company_tier: str = "Tier 3"
    baseline_recommendation: str = "Strategy"
    knockout_flags: list = field(default_factory=list)
    rationale: str = ""

    def to_schema_dict(self) -> dict:
        return {
            "company": self.company,
            "role_title": self.role_title,
            "function": self.function,
            "seniority": self.seniority,
            "fit_score": round(self.fit_score, 3),
            "priority_tier": self.priority_tier,
            "company_tier": self.company_tier,
            "baseline_recommendation": self.baseline_recommendation,
            "knockout_flags": self.knockout_flags,
            "rationale": self.rationale,
        }


def load_records(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def normalize_function(raw: str) -> str:
    key = raw.strip().lower()
    return FUNCTION_MAP.get(key, "Other")


def normalize_seniority(raw: str) -> str:
    key = raw.strip().lower()
    return SENIORITY_MAP.get(key, "Target")


def score_and_route(raw: dict) -> Opportunity:
    function = normalize_function(raw["function_raw"])
    seniority = normalize_seniority(raw["seniority_raw"])
    posted_days_ago = int(raw["posted_days_ago"])
    company = raw["company"].strip()

    opp = Opportunity(
        company=company,
        role_title=raw["role_title"].strip(),
        function=function,
        seniority=seniority,
        location=raw["location"].strip(),
        posted_days_ago=posted_days_ago,
        salary_range=raw.get("salary_range", "").strip(),
    )

    # --- knockout checks (deterministic, run first) ---
    if raw["function_raw"].strip().lower() in EXCLUDED_FUNCTIONS:
        opp.knockout_flags.append(f"Function mismatch: {raw['function_raw']} is out of scope.")
    if seniority in KNOCKOUT_MAX_SENIORITY:
        opp.knockout_flags.append("Seniority above target band (VP+).")

    # --- company tier ---
    if company.lower() in TIER_1_COMPANIES:
        opp.company_tier = "Tier 1"
    elif function in {"Strategy", "GTM", "PMM", "BizDev", "Product", "CS", "Finance"}:
        opp.company_tier = "Tier 2"

    # --- baseline routing ---
    opp.baseline_recommendation = BASELINE_MAP.get(function, "Strategy")

    # --- priority tier (mirrors the 5-tier targeting-session rule set) ---
    is_core_function = function in {"Strategy", "GTM", "PMM", "BizDev", "CS", "Product"}
    is_fresh = posted_days_ago <= MIN_FRESHNESS_DAYS
    is_target_level = seniority == "Target"
    is_reach = seniority == "Director"

    if opp.knockout_flags:
        opp.priority_tier = 5
    elif is_core_function and is_target_level and is_fresh and function in {"Strategy"}:
        opp.priority_tier = 1
    elif is_core_function and is_target_level and is_fresh and function in {"GTM", "PMM"}:
        opp.priority_tier = 2
    elif is_core_function and is_target_level and is_fresh and function in {"BizDev", "CS"}:
        opp.priority_tier = 3
    elif is_core_function and is_target_level and not is_fresh:
        opp.priority_tier = 4
    elif is_reach:
        opp.priority_tier = 5
    else:
        opp.priority_tier = 4

    # --- composite fit score (0-1), deterministic ---
    score = 0.0
    score += 0.40 if is_core_function else 0.0
    score += 0.25 if is_target_level else (0.10 if is_reach else 0.0)
    score += 0.20 if is_fresh else max(0.0, 0.20 - 0.03 * (posted_days_ago - MIN_FRESHNESS_DAYS))
    score += 0.15 if opp.company_tier == "Tier 1" else (0.08 if opp.company_tier == "Tier 2" else 0.0)
    score -= 0.30 * len(opp.knockout_flags)
    opp.fit_score = max(0.0, min(1.0, score))

    return opp


def deterministic_rationale(opp: Opportunity) -> str:
    """Fallback rationale generator used when --use-llm is not passed. Rule-based, not an LLM call."""
    if opp.knockout_flags:
        return f"Knockout flag present: {opp.knockout_flags[0]}"
    if opp.priority_tier <= 2:
        return f"{opp.function}-function match at {opp.seniority.lower()} level, posted {opp.posted_days_ago}d ago — {opp.company_tier}."
    if opp.priority_tier == 3:
        return f"{opp.function}-function match at {opp.seniority.lower()} level, posted {opp.posted_days_ago}d ago — P3 function band ({opp.company_tier})."
    if opp.priority_tier == 4 and opp.posted_days_ago > MIN_FRESHNESS_DAYS:
        return f"{opp.function}-function match but posting is {opp.posted_days_ago}d old — lower urgency tier."
    if opp.priority_tier == 4:
        return f"{opp.function}-function match, but seniority/function combination falls outside P1-P3 bands."
    return "Reach-level role — include only if brand value justifies applying above target seniority."


def maybe_llm_rationale(opp: Opportunity, use_llm: bool) -> str:
    if not use_llm:
        return deterministic_rationale(opp)
    try:
        import anthropic  # type: ignore
    except ImportError:
        print("anthropic package not installed; falling back to deterministic rationale.", file=sys.stderr)
        return deterministic_rationale(opp)

    system_prompt = Path(__file__).with_name("system_prompt.md").read_text(encoding="utf-8")
    # Extract just the fenced prompt block for the actual system message.
    match = re.search(r"```\n(.*?)```", system_prompt, re.DOTALL)
    system_message = match.group(1).strip() if match else system_prompt

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
    payload = opp.to_schema_dict()
    payload["rationale"] = ""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        system=system_message,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    text = response.content[0].text.strip()
    try:
        parsed = json.loads(text)
        return parsed.get("rationale", deterministic_rationale(opp))
    except json.JSONDecodeError:
        print("Model did not return valid JSON; falling back to deterministic rationale.", file=sys.stderr)
        return deterministic_rationale(opp)


def build_digest(opportunities: list[Opportunity], top_n: int) -> str:
    ranked = sorted(opportunities, key=lambda o: (o.priority_tier, -o.fit_score, o.posted_days_ago))
    selected = [o for o in ranked if not o.knockout_flags][:top_n]
    skipped = [o for o in ranked if o not in selected][:5]

    lines = [f"# Targeting Digest — top {len(selected)} of {len(opportunities)} scanned\n"]
    for i, o in enumerate(selected, 1):
        flags = "; ".join(o.knockout_flags) if o.knockout_flags else "None"
        lines.append(
            f"**{i}. {o.company} — {o.role_title}**\n"
            f"   Function: {o.function} | Seniority: {o.seniority} | Tier: P{o.priority_tier} / {o.company_tier}\n"
            f"   Baseline: {o.baseline_recommendation} | Fit score: {o.fit_score:.2f}\n"
            f"   Knockout flags: {flags}\n"
            f"   Rationale: {o.rationale}\n"
        )
    if skipped:
        lines.append("\n## Notable skips\n")
        for o in skipped:
            lines.append(f"- {o.company} — {o.role_title}: {o.rationale}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Score and route job opportunities from a CSV.")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--use-llm", action="store_true", help="Call the Anthropic API for rationale text.")
    parser.add_argument("--json-out", type=Path, default=None, help="Write full structured results here.")
    args = parser.parse_args()

    raw_records = load_records(args.csv_path)
    opportunities = [score_and_route(r) for r in raw_records]
    for opp in opportunities:
        opp.rationale = maybe_llm_rationale(opp, args.use_llm)

    digest = build_digest(opportunities, args.top)
    print(digest)

    if args.json_out:
        args.json_out.write_text(
            json.dumps([o.to_schema_dict() for o in opportunities], indent=2),
            encoding="utf-8",
        )
        print(f"\n[wrote structured output to {args.json_out}]", file=sys.stderr)


if __name__ == "__main__":
    main()
