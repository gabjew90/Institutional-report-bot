# Pulse regression test corpus

Pinned historical pulse fixtures. Run synthesizer + validator against them
to confirm fixes work BEFORE shipping to production. Replaces the "ship
and wait for tomorrow's QC" feedback loop.

## What's here

Each fixture is a directory `fixtures/<date-tag>/`:

- `ctx.json` — pulled from `pulse-context/` on pulse-data branch at the
  time of the pulse. The full theme_map + discovery_audit + per-PDF
  analyses_json the synthesizer produced.
- `final.md` — the actual pulse markdown that shipped, pulled from
  `pulse-output/archive/<ts>.md`.
- `qc_review.md` — the QC review that ran on this pulse, pulled from
  `pulse-output/qc-reviews/<ts>.md`. Lists the failures we want the
  regression test to catch.
- `expected.json` — engineering-side annotations of what the validator
  SHOULD flag on this fixture (see schema below).

## Fixture schema for `expected.json`

```json
{
    "fixture_id": "2026-05-29-ai-dup",
    "summary": "AI capex shipped as two separate INSIGHTS sections — sibling-canonical fold violated",
    "expected_hard_violations": [
        {
            "kind": "duplicate-sibling-sections",
            "theme_substring": "ai infrastructure",
            "sibling_substring": "ai infrastructure pivot"
        }
    ],
    "expected_soft_violations": [
        {"kind": "stance-split-no-named-debate"}
    ],
    "should_pass_validator": false
}
```

## Running the regression suite

```bash
python3 tests/pulse_regression/run.py
```

Reports per-fixture: `PASS`, `FAIL: missed expected violations`, or
`FAIL: unexpected violations`. Non-zero exit code on any FAIL.

Add to CI before shipping a fix that touches:
- `report/synthesizer.py`
- `scripts/pulse_draft_validate.py`
- `ai_analysis/voice_rules.py` (compose_lint_patterns affecting validator)

## Adding new fixtures

When a pulse exhibits a failure pattern worth regression-testing:

1. Pull `ctx.json` from `pulse-context/latest.json` on pulse-data
2. Pull `final.md` from `pulse-output/archive/<ts>.md`
3. Pull `qc_review.md` from `pulse-output/qc-reviews/<ts>.md`
4. Write `expected.json` with what the validator SHOULD flag
5. Drop all 4 files into `fixtures/<date-tag>/`
6. Run `python3 tests/pulse_regression/run.py` to confirm
