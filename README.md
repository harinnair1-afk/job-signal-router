# job-signal-router

A small, real, runnable pipeline for scoring and routing job opportunities against a
target profile — deterministic filtering and tiering logic, a JSON-schema-constrained
output contract, and one narrowly-scoped LLM step for natural-language rationale.

## Context

I run an active, high-volume job search and track roughly 100 target companies across
gaming, tech, and energy. For months the targeting logic — freshness filters, function
matching, seniority bands, company-tier weighting, knockout checks for hard blockers —
lived as a detailed prompt I ran manually inside Claude sessions against a scraped CSV of
postings. It worked well, but it wasn't a system: it was a set of rules I re-explained to
a model every time, with no versioning, no tests, and nothing I could point to as evidence
of the underlying skill.

This project pulls that logic out into something concrete: a standalone Python package
with a defined schema, a documented system prompt, and deterministic code doing everything
that doesn't actually need a language model.

## What it does

1. **Reads** a CSV of raw opportunity records (company, role, function, seniority,
   location, posting age, salary).
2. **Filters and scores** each record deterministically — no LLM involved — against a
   5-tier priority framework (function match × seniority band × posting freshness ×
   company-brand tier), plus a knockout-flag pass for hard blockers (function mismatch,
   seniority above target band).
3. **Validates** every output record against `schema.json` — a hard contract for what a
   "scored opportunity" object has to contain.
4. **Optionally** calls an LLM for exactly one thing: writing a single, rule-constrained
   sentence of fit rationale per record (`system_prompt.md`). Everything upstream of that
   is pure code, on purpose — the model isn't asked to make judgment calls that a
   deterministic rule already makes better and more reproducibly.
5. **Outputs** a ranked digest (Markdown) and the full structured result set (JSON).

## Why the LLM is scoped so narrowly

The instinct with a lot of "AI pipeline" work is to hand the model the whole problem.
This one doesn't, deliberately. Scoring, tiering, and knockout logic are business rules —
they should be auditable, testable, and consistent every time, which means they belong in
code, not in a prompt. The one place natural-language judgment actually earns its keep is
turning a structured record into a one-line, specific, non-generic explanation of *why* —
so that's the only place the LLM is in the loop, and its output is schema-validated before
it's trusted.

## Run it

```bash
pip install -r requirements.txt        # anthropic + jsonschema (only needed for --use-llm / validation)

# Deterministic mode — no API key required
python router.py sample_input.csv --top 5

# With LLM-generated rationale (requires ANTHROPIC_API_KEY)
python router.py sample_input.csv --top 5 --use-llm --json-out output.json
```

`sample_input.csv` is a small synthetic dataset (15 rows) modeled on the shape of real
scraper output, not real scraped data. `sample_output.md` / `sample_output.json` in this
repo are a real run of the deterministic engine against that sample — not hand-written
example output.

## Files

| File | What it is |
|---|---|
| `router.py` | The pipeline: CSV load → deterministic scoring/routing → schema validation → optional LLM rationale → digest output |
| `schema.json` | JSON Schema contract for a scored/routed opportunity record |
| `system_prompt.md` | The system prompt for the one LLM step, with explicit output constraints |
| `sample_input.csv` | Synthetic sample data |
| `sample_output.md` / `sample_output.json` | A real run of the pipeline against the sample data (deterministic mode) |

## Honest scope note

This is an MVP extraction of a real, ongoing personal workflow — not a claim of
production infrastructure at any employer. The underlying targeting logic (priority
tiers, knockout checks, company-tier weighting) is the same rule set I use for my own
search; this repo is that rule set made portable, testable, and demonstrable.
