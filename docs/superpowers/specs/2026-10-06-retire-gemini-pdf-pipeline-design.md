# Retire the Gemini PDF pipeline: design

Status: draft for owner review, 2026-10-06. Nothing here is built.

## 1. What the owner asked

"When can we retire the expensive Gemini calls?" (2026-10-06). The owner
raised the Claude usage limit the same day, so Claude reader load is a
cost to measure, not a blocker.

## 2. What the Gemini PDF pipeline costs (measured)

`pdf_analyses`, last 30 days to 2026-10-06, latest analysis per PDF, at
the list prices in `ai_analysis/usage_ledger.PRICES_PER_M`
(gemini-3.1-flash-lite, $0.25 in / $1.50 out per million tokens):

| Priority | Documents | Gemini work per document | Input tokens | Output tokens | About $/month |
|---|---|---|---|---|---|
| HIGH | 817 | triage, deep analysis, page images for top banks | 18.0M | 1.84M | $7 |
| MEDIUM | 1,076 | triage, deep analysis | 29.1M | 2.32M | $11 |
| LOW | 1,471 | triage only | 24.5M | 0.29M | $6.5 |
| Total | 3,364 | | 71.7M | 4.45M | $25 |

The per-call ledger (`gemini_calls`, 2026-09-30 to 10-06) splits the same
work about 35% deep analysis and 19% triage of all Gemini spend in a light
week. The rest of the ~$50 monthly Gemini bill is the profile refresh
(about a quarter), /ask, screenshot and chat OCR, the trade parsers and the
race tagger, none of which this spec touches.

Triage reads the file name, folder and the first 32,000 characters of every
PDF (`ai_analysis/analyzer.py:184-188`) to return one word.

## 3. What reads the Gemini output today

All from `pdf_analyses.analysis_json` (field lists in the 2026-10-06
research note in the session; file references here).

| Consumer | Priorities | Fields it needs |
|---|---|---|
| Omnipulse readers choose documents (`github_bridge/pilot_publish.py:60-74`) | HIGH only | the triage priority |
| Morning routine context dump (`github_bridge/jobs.py:162`, `report/synthesizer.py:1684`): RECAP, WHAT TO WATCH, `_LEANS`, the classic pulse on miss days, footer stats | HIGH, MEDIUM | nearly every field |
| Pulse volume gate anchor stats (`jobs.py:203-224`) | all | `anchor_check` |
| /ask desk research `research_for_ticker` (`db_parts/pdf.py:1171`) and the stock outline's DESK slots | HIGH, MEDIUM | market_movers, trade_ideas, earnings_insights, key_insights, risk_factors, key_data_points, entities |
| Print takeaway `research_for_release` (`report/print_takeaway.py:49`) | HIGH, MEDIUM | macro_indicators, key_insights |
| Calendar bold names `recently_covered_tickers` (`pdf.py:1266`) | all | earnings_insights, entities |
| Calendar industry events `conference_sessions_for_date` (`pdf.py:1121`) | all | conference_sessions |
| Ingestion feed (`discord_bot/ingestion_feed.py`) | HIGH, MEDIUM | title, key_insights[0], entities, trade_ideas[0] |
| /ask `query_data` SQL tool (`latest_pdf_analyses` view) | all | any |

## 4. What the Claude readers produce

Files on the `pilot-data` branch, not database rows:
`pilot/cards/<date>/<pdf_file_id>.json` = `{"brief": ..., "cards": [...]}`.
A card has bank, document, claim, a verbatim anchor (machine-verified),
topic, status (released/forecast/target/level), instruments, macro_key,
direction, conviction, timeframe (`docs/superpowers/routines/pilot/reader.md:45-58`).

Gaps against section 3: no ratings or price targets as fields (a target is a
card with status "target"), no trade_ideas block, no conference_sessions, no
charts_described, no entities list beyond instruments, no anchor_check in the
pipeline's form. Readers read HIGH only, cards land about an hour after
publish on weekday mornings and up to 12 hours on weekends (worker dispatch
slots, `github_bridge/workflow_dispatch.py:34-36`), and runs are serialized at
6-7 minutes per document. Gemini's deep analysis lands within minutes.

## 5. Constraints

1. The classic pulse stays the fallback until the Omnipulse passes its
   retirement gate (10 consecutive market days with `body_source:
   omnipulse`, `scripts/omnipulse_body.py`). Earliest pass: 2026-10-16.
2. The reader prompt and model strings are frozen during the pilot; a change
   restarts the pilot clock (`scripts/pilot_config.py`).
3. The existing Opus ingestion bridge (`github_bridge/ingestion.py`,
   `HIGH_INGESTION_BACKEND`) was built 2026-05-07 and never turned on. Turning
   it on as built would starve the pilot: its fork returns before the pilot
   publish hook (`pipeline/orchestrator.py:127-141`).
4. Every consumer in section 3 must keep working on the day Gemini stops.

## 6. Options

**A. Convert cards into analysis rows.** After the readers finish a document,
a converter writes a `pdf_analyses` row from its cards (claims to
key_insights, status "target" cards to market_movers, instruments to
entities, macro_key cards to macro_indicators), marked
`model_used='claude-cards'`. No second read. Loses the fields cards lack
(conference sessions, trade ideas as a block, charts) and inherits the
reader latency.

**B. Readers write both.** After the gate, extend the reader output with the
analysis fields the consumers need, so one Claude read per document fills
cards and the analysis row. Complete coverage, one read, but it changes the
frozen reader prompt, so it can only start after the gate.

**C. Turn on the Opus bridge.** A separate Claude routine runs the existing
deep-analysis prompt and writes analysis rows directly. Same schema, no
converter, but every HIGH document is read twice by Claude, and the bridge
must first stop starving the pilot.

**Triage.** Three replacements, cheapest first: (T1) keep Gemini triage but
send 8,000 characters instead of 32,000, about a quarter of the cost, if a
sample shows the same priorities; (T2) a rule from source folder, report
type and title, free but blunter; (T3) let readers read everything, which
needs parallel reader runs (1,500+ more documents a month at 6-7 minutes each
does not fit one serial lane).

## 7. Recommendation

**T1 result (dry run 2026-10-06, 200 pilot documents, 100 HIGH and 100
MEDIUM, production prompt, plain client):** rejected. Agreement with a
full-length re-run: 16,000 characters 92%, 8,000 characters 84% (a
full-length re-run agrees with the stored priority 88% of the time, so
triage is noisy to begin with). Input tokens per call: 8,000 at full
length, 6,000 at 16k, 4,500 at 8k, because the fixed prompt is about
half of every call, so the saving is $2-3.50 a month at best. 8,000
characters moved 14 of 100 MEDIUM documents to LOW against 7 at full
length. The code comment in `ai_analysis/analyzer.py` records that 8,000
was tried before and raised to 32,000 for the same reason. Not shipped.

Phase 0, now, during the gate (no production change):
- T1 sample: re-triage the last 300 PDFs with 8,000 characters in a dry run
  and compare priorities with the stored ones. If at least 95% agree, ship it:
  about $5-6 a month saved without touching the Omnipulse.
- Converter in shadow (option A): write card-derived rows to a separate table
  for a week and diff what /ask research, the print takeaway and the calendar
  would have shown against the Gemini rows.
- Measure reader latency and Claude usage per document.

Phase 1, after the gate passes: turn Gemini deep analysis off for HIGH,
keep the converter (or option B once the reader prompt can change), keep
Gemini for MEDIUM. Saves about $5-6 a month more.

Phase 2, only if wanted: MEDIUM through the readers (option B plus parallel
runs). Saves about $11 a month and is the largest piece, but doubles reader
volume.

Honest summary: the PDF pipeline is about $25 a month. Phases 0 and 1 save
roughly $10-12 of it with low risk. Retiring all of it needs Phase 2.

## 8. Decisions for the owner

1. Run the Phase 0 triage sample (T1) now?
2. Option A (converter, starts now) or B (readers write both, starts after the
   gate) for HIGH?
3. Is Phase 2 (MEDIUM through Claude) worth about $11 a month, given that it
   doubles reader volume and adds hours of latency to /ask desk research on
   fresh notes?

## 9. Status (2026-10-06)

- The lane runs in shadow: `.github/workflows/pilot-analysis.yml` writes a
  record for every HIGH document to `pilot/analyses/`. First comparison, 24
  documents: Claude 363 insights to Gemini's 179, 293 data points to 105,
  anchors found in the source 99.7% to 99.0%; Gemini found more desk calls
  (17 to 12) and described more charts (64 to 34).
- The cutover switch is built and off (`github_bridge/claude_lane.py`,
  `HIGH_INGESTION_BACKEND=claude_lane`). Owner call: compare for a couple
  more days, then flip it.
