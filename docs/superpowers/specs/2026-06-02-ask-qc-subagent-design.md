# /ask QC Sub-Agent — Design

**Status:** approved, ready for plan
**Date:** 2026-06-02
**Gap addressed:** /ask QC gap #1 (post-hoc QC sub-agent on /ask logs)
**Related shipped work:** /ask gap #4 + #5 — freshness stamps + status taxonomy (commit `8bda2c9`)

---

## Goal

Daily post-hoc grader that reads the previous day's `/ask` interaction log, scores each interaction against a fixed 6-dimension rubric using a separate Gemini call, and publishes a per-day markdown report to `pulse-data:ask-qc/`. Forward-only: starts grading the day after merge, no historical backfill on first deploy.

## Why

`/ask` is the highest-traffic surface where Gemini drives end-user-facing prose. Recent QC sweeps (this session: 2026-05-30 → 2026-06-01) surfaced patterns I had to spot by hand — architecture leaks, fabrications on `status=empty` responses, voice/format drift, depth mismatches. The shipped fixes for gaps #4 + #5 add the *information* the model needs to behave correctly; this gap adds the *observability* to verify it's actually using that information.

The goal is not to silently auto-correct answers — it's to give me a daily ~3-minute read on whether `/ask` is drifting, so prompt/rubric fixes can land before drift becomes pattern.

## Architecture

```
00:00 UTC  ─ day boundary; today's /ask interactions stop appending to {today}.md
   ↓
03:00 ET   ─ APScheduler fires _ask_qc_job  (after midnight ET, before 09:00 pulse)
   ↓
ask_qc/parser.py
   ↓ reads /data/ask-logs/{yesterday-UTC}.md
   ↓ splits on `^## 20\d\d-` headers → list[AskInteraction]
   ↓
ask_qc/grader.py
   ↓ for each interaction: one Gemini Flash Lite call, parallel via asyncio semaphore
   ↓ → list[GradedInteraction]
   ↓
ask_qc/aggregator.py
   ↓ renders markdown: header summary + per-interaction blocks
   ↓
write /data/ask-qc/{date}.md  +  github_bridge.put_file("ask-qc/{date}.md", ...)
```

**Key design choices:**

- **Idempotent + re-runnable.** Markdown file IS the state; no DB writes. Crashing mid-grade → next run overwrites cleanly.
- **Single APScheduler job** at 03:00 America/New_York, registered next to `_ask_log_publish_job` in `scheduler/jobs.py`.
- **Reuses existing GitHub bridge** (`gh_client.put_file()`) — already auth'd and idempotent (handles the existing-sha update path).
- **File-based state, not DB.** Mirrors the existing `/ask-logs` pattern. DB persistence can layer on later if/when trend dashboards justify the schema.
- **Yesterday-only per run.** Each cron fire grades exactly one date (yesterday UTC). Manual backfill is a separate one-shot script — only built if needed; not part of v1.

## Components

```
ask_qc/
├── __init__.py
├── models.py          # AskInteraction, DimensionVerdict, GradedInteraction dataclasses
├── parser.py          # parse_ask_log(text) → list[AskInteraction]
├── judge_prompt.py    # system prompt + rubric definitions + response schema
├── grader.py          # grade_day(interactions) → list[GradedInteraction]
└── aggregator.py      # render_report(date, graded) → markdown str

scheduler/jobs.py
└── _ask_qc_job        # new APScheduler hook, 03:00 ET cron

scripts/
├── smoke_ask_qc_parser.py
├── smoke_ask_qc_judge_prompt.py
├── smoke_ask_qc_grader.py
├── smoke_ask_qc_aggregator.py
└── smoke_ask_qc_end_to_end.py
```

### Module responsibilities

| Module | Responsibility | What it knows | What it doesn't touch |
|---|---|---|---|
| `models.py` | Dataclass shapes — keeps modules decoupled | dataclass fields only | DB, Gemini, files |
| `parser.py` | Pure: `parse_ask_log(text: str) -> list[AskInteraction]`. Handles both `<details>`-block and legacy shape | Markdown format | Gemini, DB, filesystem |
| `judge_prompt.py` | System prompt + the 6 rubric dimension definitions + response schema | Rubric semantics | Gemini SDK |
| `grader.py` | `grade_day(interactions, sem_size=3) -> list[GradedInteraction]`. Wraps each in a Gemini call, parses JSON, retries once | Gemini SDK | Markdown rendering |
| `aggregator.py` | `render_report(date, graded) -> str`. Markdown — header summary + per-interaction blocks | Markdown layout | Gemini, parsing |
| `_ask_qc_job` | Orchestrator. Reads file, calls parser → grader → aggregator, writes locally, pushes to GitHub | All the above | Rubric internals |

### Judge response schema

The judge returns this exact JSON per interaction:

```json
{
  "overall": "CLEAN | CONCERN | FAIL",
  "dimensions": {
    "fabrication":            {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "status_handling":        {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."},
    "voice":                  {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "format_adherence":       {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "depth_match":            {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "decline_when_uncertain": {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."}
  },
  "notable_pattern": "single-line one-shot description, or null"
}
```

`N/A` exists for dimensions that can't be evaluated without the `<details>` prompt block (legacy logs) — `status_handling` and `decline_when_uncertain` depend on knowing what tools returned.

### Reused scaffolding

- `github_bridge.client.put_file()` — already auth'd, idempotent (no retry layer; one-shot per call)
- `config.settings` — `pdf_download_dir.parent / "ask-qc"` mirrors the existing /ask-logs path resolution
- `db.log_event()` / `pipeline_events` — observability row per run (issue #7 pattern)
- Gemini SDK init pattern from `ai_analysis/analyzer.py` — copy model selection + timeout config

## Grading Rubric

Six dimensions, each scored `PASS | CONCERN | FAIL | N/A`. The judge gets these definitions verbatim in its system prompt — calibration is transparent and editable.

### 1. `fabrication`
Did the answer claim specific facts not supported by tool output, search results, or clear general knowledge?

- **PASS** — every specific claim (price, rank, date, %, attributed quote) is either grounded in the tool/search payload visible in `<details>`, or is general knowledge that doesn't change with time
- **CONCERN** — borderline grounding (approximate number without an "around" hedge, paraphrased rank that drifts from the verbatim rationale)
- **FAIL** — a specific factual claim contradicts or extrapolates beyond the tool/search output. Includes: confident answer when `status=empty`/`error`; quoting a price that wasn't in the response; inventing a rank position
- **N/A** — pure Type 2 banter / Type 3 personality answer with no factual assertion

**Examples in the judge prompt:**
- FAIL: *"Abe is 54-0 this month"* when `lookup_trade_log` returned `status=empty`
- FAIL: *"BK closed PLTR at $24 yesterday"* when no tool was called and search didn't include that
- PASS: *"Don't have clean data on that — search didn't return a current price"*

### 2. `status_handling` (legacy logs → N/A)
Did the answer correctly interpret the new `status` + freshness fields?

- **PASS** — `status=empty` → "no data"; `status=error` → "lookup failed"; `status=not_found` → "don't see that user/rank"; `status=ok` → uses the data
- **CONCERN** — hedges where data exists, or under-hedges a stale `updated_at` (>5d old)
- **FAIL** — treats `empty` as data; fabricates on `error`; ignores `not_found` and invents the user
- **N/A** — log entry has no `<details>` block (legacy)

### 3. `voice`
Trader-newsletter voice — conversational, opinionated, no AI tells.

- **PASS** — direct, opinionated, sharp. No "it's worth noting", "notably", "moreover", "furthermore", "in conclusion", em-dash chains, hedging cascades, or summary wrap-ups
- **CONCERN** — 1-2 AI tells slip through
- **FAIL** — paragraph-mode prose, multiple AI tells, lecture cadence

### 4. `format_adherence`
Type 1 answers = literal `→` arrow bullets within the tier's word ceiling, no closing prose.

- **PASS** — arrow bullets, within word cap, no closing wrap-up paragraph
- **CONCERN** — one stray closing line or modest word-cap overshoot (≤15%)
- **FAIL** — prose-only where arrows were required, or `In short, …` summary tacked on
- **N/A** — Type 2 (banter) / Type 3 (personality)

### 5. `depth_match`
Answer tier matches question shape — concision is the default, depth is on-request.

- **PASS** — Quick → Quick (≤60w, 2-3 arrows); Standard → Standard (≤130w, 3-5 arrows); "DD on X" → Full DD (≤250w, 5-7 arrows)
- **CONCERN** — one tier off (Standard answer to a Quick question)
- **FAIL** — two tiers off (Full DD to "what's TSLA at"), or sub-2-arrow answer to a clear DD request

### 6. `decline_when_uncertain` (legacy logs → N/A)
When tools came back empty/error or search whiffed, did the answer cleanly decline?

- **PASS** — `status=empty`/`error` surfaced as "no data" / "lookup failed"; search-miss → "can't verify cleanly"
- **CONCERN** — slight overconfidence given the data quality
- **FAIL** — confident fabricated answer on top of failed lookup
- **N/A** — every tool returned `status=ok` AND search returned grounded results

### Overall verdict rollup
- **CLEAN** — all six PASS or N/A
- **CONCERN** — 1+ CONCERN, no FAIL
- **FAIL** — 1+ FAIL on any dimension

### Notable pattern
Optional one-line cross-day pattern description. Aggregator surfaces the top 1-3 most-repeated patterns in the daily report header.

### What the judge is explicitly told NOT to penalize
- Brevity when the question was terse (that's the `depth_match` PASS condition)
- Declining when tools failed (goal, not failure)
- Answer differing from what the judge itself would have written if grounded + voice-correct + format-correct
- Insults / charged language in Type 2 / Type 3 answers — the prompt allows them

## Error Handling

| Failure | Behavior |
|---|---|
| Yesterday's log file doesn't exist | Log `ask-qc: no log for {date}, nothing to grade` and exit cleanly |
| Empty log file (0 interactions) | Write `*No /ask interactions on {date}.*` stub locally; **skip the GitHub push** |
| Gemini API outage | Per-interaction retry once with 5s backoff. On second failure, tag `UNGRADED` (rationale = exception class). Other interactions continue |
| Judge returns malformed JSON | Same retry-once policy → `UNGRADED` on second failure |
| Single interaction >200k chars | Tail-truncate `<details>` block, preserve Q + A + first 100k of prompt. Report tagged `prompt_truncated_for_grader: true` |
| Malformed log file | Parser silently skips any block where the timestamp regex doesn't pin. Report header notes `{N} entries unparseable` |
| GitHub push fails | Local write happens first and succeeds. Push failure logged at `WARNING` but not raised — the local file is the source of truth. Manual recovery if push has been failing for multiple days (rare; pulse-data publishing for `/ask-logs` uses the same bridge and has been stable). Automatic republish-on-next-run is **not** wired in v1 to keep the job small; can be added later if pushes prove flaky |
| Concurrent backfill + scheduled run on same date | Both write idempotently; last-writer-wins. Acceptable |

## Testing

Five smoke tests in `scripts/` (project convention):

| Smoke | Covers |
|---|---|
| `smoke_ask_qc_parser.py` | Real `.tmp_ask_log_today.md` (63 interactions) + a legacy file without `<details>`. Assert N, timestamp parse, `<details>` extraction when present and `None` when not, malformed-skip behavior |
| `smoke_ask_qc_judge_prompt.py` | Build prompt for a canned interaction. Assert all 6 rubric defs present, JSON schema example present, "do NOT penalize" list present |
| `smoke_ask_qc_grader.py` | Mock Gemini SDK. Three cases: clean JSON → `GradedInteraction`; malformed JSON → retry → `UNGRADED`; API exception → retry → `UNGRADED` |
| `smoke_ask_qc_aggregator.py` | Canned `list[GradedInteraction]` covering CLEAN/CONCERN/FAIL/UNGRADED mixes. Assert header percentages, top-pattern surfacing, per-interaction block rendering |
| `smoke_ask_qc_end_to_end.py` | Real parser + real aggregator + mocked judge. Reads `.tmp_ask_log_today.md`, mock-grades to PASS, verifies the markdown lands at right path + `put_file` called with right args |

Regression: all existing /ask smokes re-run at commit time. New code adds modules without touching executors, schema, or scheduler signatures.

## Observability

- `pipeline_events` row per job run: `{run_id, date, interactions_total, interactions_graded, interactions_ungraded, top_pattern, github_pushed_at, duration_ms}` — matches the existing issue-#7 pattern. Lets `/status` surface "last QC run: 12 interactions, 11 CLEAN, 1 CONCERN" without parsing markdown
- Log lines: `INFO ask-qc: grading {date} ({N} interactions)` at start; `INFO ask-qc: done — {clean}/{concern}/{fail}/{ungraded}, pushed to GitHub` at end
- Failure path: `ERROR ask-qc: {phase} failed: {exc}` with `exc_info=True`

## Cost

Judge model: **Gemini 3.1 Flash Lite** (same model as production /ask).

- ~50k input tokens per interaction (Q + A + `<details>` block + rubric ~5k)
- ~2k output tokens per interaction
- Per interaction: ~50k × $0.10/M + 2k × $0.40/M ≈ **$0.006**
- At ~20 /asks/day: **~$0.12/day**, **~$3.60/month**

Acknowledged risk: shares blindspots with production `/ask`. Acceptable for the first iteration because most surfaced gaps so far (voice drift, format adherence, fabrication-on-empty) are detectable by the same model when given the rubric + the prompt-block forensics. If grades feel toothless after ~2 weeks, swap the model constant in `judge_prompt.py` to Gemini 3.x Pro (one-line change, ~10× cost — still trivial).

## Backfill

**Not in v1.** Forward-only — first report lands the day after merge.

A `scripts/backfill_ask_qc.py` script can be built later if I want to grade prior days. Pattern: loop a date range, call the same `_ask_qc_job` core logic per date, resume via `processing_log` checkpoints (same pattern as `backfill_text_extracted_trades.py`). Deferred until there's a concrete need (e.g., a specific past day pattern I want to audit).

## What's NOT in scope (v1)

- **No inline / per-interaction grading.** Daily batch only. Tighter feedback loops can come later if pattern volume justifies it.
- **No automatic remediation.** The QC sub-agent grades; humans (me) read and act. No automatic prompt-edit suggestions, no auto-rerun of flagged interactions.
- **No /qc_ask Discord command.** Adding a manual trigger is a one-line change once the orchestrator exists; deferred until the cron-based flow proves the rubric is useful.
- **No DB persistence of grades.** File-only artifact. DB schema can layer on later if trend dashboards become valuable.
- **No backfill on first deploy.** Forward-only per the design call above.
