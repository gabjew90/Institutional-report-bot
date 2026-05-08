# Pulse Adjudication — Design

**Date:** 2026-05-07
**Status:** Design (Stage 1 only)
**Scope:** Add an explicit per-theme adjudication step inside the existing scheduled-pulse routine. Stage 1 ships the adjudication artifact + lint + audit/diff bridge file. Stage 2 (rewriting the prose prompt to consume adjudications as primary input) is a separate spec.

---

## Why

Current pipeline (after recent schema upgrades):

- **Per-PDF deep analysis** (Gemini) extracts structured `theme_stances` (with stance, conviction, vs_consensus, verbatim ≤15-word `evidence` quote), `tension_points` (bull/bear/what_invalidates), and atomic `key_data_points`. Anti-hallucination rules enforce empty-over-invented and verbatim-only evidence.
- **Cross-PDF aggregation** (Python, deterministic, in `report.synthesizer._classify_themes`) normalizes theme labels, fuzzy-merges duplicates, counts banks per theme + per-stance breakdown. Demotes ungrounded directional stances to neutral. Emits `THEME COVERAGE` block.
- **Synthesis** (Opus routine) receives the structured per-PDF inputs + `THEME COVERAGE` + market/news/calendar context, picks 3–8 themes, writes prose.

**The gap:** there is no enforced commitment step between "structured inputs" and "prose." Opus is doing two cognitive jobs in one call — adjudicating cross-bank consensus AND writing newsletter-voice prose. Narrative pull tends to dominate evidence weighting (well-documented failure mode). Specific consequences observed in pulses:

- Confabulated bank attributions ("GS sees X, JPM disagrees") not anchored in the actual per-PDF stances.
- Falsifiable predictions (specific levels and dates from `tension_points.what_invalidates` and `key_data_points`) smoothed into mush ("oil higher" instead of "GS Brent $98 by end-Q1").
- Diff-framing operating on prose markdown instead of structured consensus state — false positives (rephrased themes look new) and false negatives (today's prose accidentally rhymes with yesterday's).

**The fix:** force the routine to emit per-theme adjudication JSON *before* prose, derived strictly from existing structured inputs, mechanically lint-able against those inputs, and persist it for tomorrow's diff-framing.

The reasoning was already done at extraction + aggregation. This step renders that reasoning into a committed structure.

## Architecture

Single pulse routine fire (no new routine, stays inside the 15/day cap):

```
1. Fetch pulse-context/latest.json from bridge (existing)
2. Pick selected themes from THEME COVERAGE
   (top N, conviction-weighted threshold — same logic the prose prompt
    already uses implicitly; surface it as an explicit selection)
3. PARALLEL sub-agent dispatch — one Agent call per theme:
   each sub-agent receives ONLY that theme's evidence
   (theme name + per-PDF theme_stances entries matching this theme
    + relevant tension_points + relevant key_data_points)
   and returns structured adjudication JSON
4. Lint each adjudication (deterministic, in main routine context):
   reject themes whose adjudication doesn't pass
5. Write adjudications to bridge:
   pulse-output/pending-adjudications/<timestamp>.json
6. Synthesize prose using adjudicated themes as input (Stage 2 will
   rewrite this prompt; Stage 1 keeps the existing prose prompt and
   passes the adjudication block in as context)
7. Write pulse markdown + frontmatter to pulse-output/pending/
   (worker poll picks it up, posts to Discord, archives — existing flow)
```

### Why agentic-sub-agents-in-one-routine beats alternatives

| Approach | Cost | Contamination control | Fits routine budget |
|---|---|---|---|
| Inline structured-thinking in synthesis prompt | Smallest | Weak — same context sees all themes | Yes |
| Separate routine for adjudication | Small | Strong — partitioned context | **No** (15/day cap) |
| **Sub-agents per theme inside synthesis routine** | **Small** | **Strong — each sub-agent sees only its theme** | **Yes** |

Partitioned contexts solve the cross-theme contamination problem without burning a routine slot.

## Data Contract

### Adjudication JSON (per theme)

```json
{
  "theme": "hormuz oil shock",
  "selected": true,
  "stance_counts": { "supportive": 5, "skeptical": 1, "neutral": 3 },
  "consensus_view": "Supply scarcity priced; cuts coming Q2.",
  "facts_agreed": [
    {
      "claim": "OPEC+ has held output flat through April despite Hormuz disruption",
      "banks_for": ["Goldman Sachs", "Citi", "BofA"],
      "evidence_quotes": [
        "OPEC+ holding output flat through April",
        "no signal of incremental barrels into May"
      ]
    }
  ],
  "facts_contested": [
    {
      "claim": "Brent year-end target",
      "banks_for": ["Goldman Sachs"],
      "for_evidence": "we see Brent $98 by end-Q1",
      "banks_against": ["JPMorgan"],
      "against_evidence": "we expect Brent to settle near $88 once ceasefire prices in"
    }
  ],
  "falsifiable_predictions": [
    {
      "bank": "Goldman Sachs",
      "claim": "Brent $98 by end-Q1",
      "deadline": "2026-03-31"
    },
    {
      "bank": "JPMorgan",
      "claim": "Brent settles near $88 post-ceasefire",
      "deadline": "post-ceasefire"
    }
  ]
}
```

Per-pulse top-level adjudication file (`pulse-output/pending-adjudications/<timestamp>.json`):

```json
{
  "pulse_date": "2026-05-07",
  "window_label": "since-last-daily (2026-05-06 13:00)",
  "themes": [ <theme adjudications above> ],
  "discarded_themes": [
    { "theme": "...", "reason": "lint_failed: evidence_quote not found in inputs" }
  ]
}
```

### Lint rules (deterministic)

For each theme adjudication:

1. **Every `evidence_quotes[*]` (and `for_evidence`/`against_evidence`) must exact-match some input `theme_stances.evidence` string.** The Gemini extractor already enforces verbatim ≤15-word quotes; the sub-agent must echo one of those strings unchanged. No exact match → reject the entire theme adjudication, log to `discarded_themes`.
2. **Every bank in `banks_for` / `banks_against` must appear in the input PDFs' `source` field for this theme.** Eliminates fabricated bank attributions.
3. **Every `falsifiable_predictions[*].claim` must appear as a substring in some input `tension_points.what_invalidates` field OR `key_data_points.figure`/`metric` field.** Predictions are typically paraphrased so substring (not exact) is the right discipline here. `deadline` accepts either an ISO date or a non-date conditional string ("post-ceasefire", "next FOMC") that must also appear verbatim in the inputs.
4. **`stance_counts` must match the per-theme aggregated counts from `_classify_themes`** for this theme. The sub-agent is *counting* pre-extracted per-PDF stances, not reassigning them — any mismatch is a fabrication signal. Reject.

Failures are per-theme, not per-pulse. A pulse can ship with N–1 themes if one theme's adjudication fails the lint. Discarded themes are written to the adjudication file's `discarded_themes` array for inspection.

## Bridge File Layout

New on the bridge branch (alongside existing `pulse-context/latest.json` and `pulse-output/{pending,archive}/*.md`):

```
pulse-output/pending-adjudications/<timestamp>.json   # routine writes
pulse-output/archive-adjudications/<date>.json        # worker moves here on post
```

Naming mirrors pulses 1:1 — for pulse `pulse-output/pending/2026-05-07T13-00.md`, the adjudication is `pulse-output/pending-adjudications/2026-05-07T13-00.json`. Worker archives both atomically when posting to Discord.

## Diff-Framing Implications

Today, scheduled-pulse diff-framing (in `synthesizer.build_pulse_context`) extracts theme headers from yesterday's prose markdown via regex and passes them to the prompt as a "skip these" list. That's brittle — a rephrased theme looks new.

After Stage 1 lands, the next time the routine builds context it can additionally fetch `pulse-output/archive-adjudications/<yesterday>.json` and surface a structured prev-themes block:

```
PREVIOUS PULSE ADJUDICATIONS:
- hormuz oil shock — consensus_view: "Supply scarcity priced; cuts coming Q2." (supportive 5/skeptical 1/neutral 3)
- ai hyperscaler capex — consensus_view: "Capex revisions higher; concentration risk rising." (supportive 6/skeptical 2/neutral 1)
```

Tomorrow's adjudication sub-agents can compare *consensus_view text* and *stance_counts shifts* — not prose. That's the unlock that makes the audit file load-bearing rather than archival.

This is a context-builder change in `report/synthesizer.py` — out of scope for Stage 1 implementation but the file format is designed to support it.

## Component Changes

### Routine prompt (lives on Claude.ai, not in this repo)

Substantial change. The routine's prompt becomes:

1. Fetch context (existing).
2. Theme selection step (new, explicit): rank `THEME COVERAGE` by bank count + conviction, pick top N (e.g., 3–8).
3. Per-theme parallel `Agent` dispatch with the strict-input adjudication prompt (new).
4. Lint pass via main-context Bash/jq or inline JSON checks (new). Discarded themes go to `discarded_themes`.
5. Write `pending-adjudications/<timestamp>.json` to bridge (new).
6. Existing prose synthesis step, with the adjudication block additionally injected as input (Stage 1 keeps prose prompt mostly unchanged; Stage 2 rewrites it).
7. Write `pending/<timestamp>.md` to bridge (existing).

The exact prompt text is part of the implementation plan, not this design doc.

### `github_bridge/jobs.py` (in this repo)

Modifications to `_process_one_pulse`:

- After identifying a pending pulse `<name>.md`, look for `pulse-output/pending-adjudications/<name>.json`.
- If present: fetch it, attach to `DailyReport.raw_json` (so it lands in `daily_reports.report_json` as well — useful for backfill and analysis), and archive it to `pulse-output/archive-adjudications/<date>.json` atomically with the pulse archive step.
- If absent: log a warning but continue posting (graceful degradation — bridge doesn't block a pulse on a missing adjudication file).

New constants:

```python
PENDING_ADJUDICATIONS_DIR = "pulse-output/pending-adjudications"
ARCHIVE_ADJUDICATIONS_DIR = "pulse-output/archive-adjudications"
```

### `db.py` / `daily_reports` schema

No schema change. Adjudication JSON rides inside `report_json` (existing TEXT column). If backfill / structured queries become useful later, that's a separate migration.

### `report/synthesizer.py`

No change in Stage 1. (Stage 2 will likely add a `fetch_prev_adjudications` helper to inject structured prev-themes into context.)

## Failure Handling

| Failure | Behavior |
|---|---|
| Sub-agent returns malformed JSON | Drop that theme, add to `discarded_themes` with reason, continue. |
| Sub-agent JSON fails lint (evidence quote not found, bank not in inputs, etc.) | Same — drop theme, log reason. |
| All themes fail | Routine still writes pulse markdown + an adjudication file with `themes: []` and full `discarded_themes`. Better to ship a pulse + audit than skip the day. |
| Routine writes pulse markdown but no adjudication file | Worker logs warning, posts pulse anyway, no adjudication is archived. Manual investigation. |
| Routine writes adjudication file but no pulse markdown | Worker does nothing (it polls `pending/`, not adjudications). Adjudication is orphaned until manual cleanup. |

## Out of Scope (Stage 2+)

- Rewriting the prose synthesis prompt to consume adjudicated themes as primary input (instead of raw per-PDF JSON). Stage 1 still passes raw JSON; the adjudication block is added context, not replacement.
- Structured prev-pulse diff-framing using yesterday's adjudication file.
- Track-record backtesting (verifying `falsifiable_predictions` against actual market prints over time).
- Promoting adjudication to a separate routine fire (only worth doing if Stage 1 reveals contamination patterns the partitioned sub-agents don't catch).

## Success Criteria

After Stage 1 ships:

1. Each scheduled pulse produces a companion `archive-adjudications/<date>.json` file with at least one validated theme.
2. Lint catches at least one fabricated evidence quote or bank attribution per week (validating the lint is doing useful work — if zero rejections for a month, the lint is too lenient or the sub-agents are perfect, both worth investigating).
3. The `daily_reports.report_json` column for scheduled pulses contains the adjudication block, queryable via `inspect_db.py`.
4. No regression in pulse Discord delivery (channel posts unchanged in cadence and structure for Stage 1).
