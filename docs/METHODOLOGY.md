# Institutional Research PDF Analyzer + Discord Market Pulse Bot — Methodology

A complete walkthrough of how the bot ingests 100-200 institutional research PDFs daily and synthesizes them into a Discord-delivered Market Pulse for self-directed options and crypto traders.

---

## 1. Audience and design constraint

The reader is a self-directed US options/crypto trader. Smart, reads the WSJ, trades actively. **Not** a finance professional. They don't know what "convexity," "term structure," "NII," "bps," or "duration" mean as standalone words.

Every output prioritizes:
- **Plain English first.** Jargon gets translated inline or rewritten.
- **Trade implication closes every theme.** A specific instrument lean (long $TLT, $BNO, etc.) — not generic "watch closely."
- **Honest attribution.** Single-bank claims attributed; multi-bank consensus shown explicitly with stance counts.
- **No fabricated dates, prices, or attributions.** If research didn't say it, the pulse doesn't write it.

The bot is hosted on Railway (project `marvelous-dream`, service `worker`) and posts daily to Discord. Source: https://github.com/gabjew90/Institutional-report-bot

---

## 2. End-to-end pipeline

```
Dropbox /Current
   │   (every 15 min — cursor-based delta polling)
   ▼
Railway worker downloads PDFs to /data volume
   │
   ▼
pdf_files row created (status=DOWNLOADED)
   │   (every 5 min — async processing queue)
   ▼
PyMuPDF extracts full text
   │
   ▼
Triage (Gemini, ~2K tokens) → priority + source + tickers
   │
   ▼
Deep analysis (Gemini, full document text-only, ~15K tokens)
   │   → structured JSON: theme_stances, contextual_mentions,
   │     key_data_points, market_movers, macro_indicators, etc.
   ▼
pdf_analyses row (append-only)
   │
   │   (every 15 min)
   ▼
build_pulse_context() runs on Railway
   │   - Theme clustering (Phase A: embeddings on theme_stances)
   │   - Discovery clustering (Phase B: embeddings on contextual_mentions)
   │   - Live market data (Binance.US + Finnhub /quote)
   │   - Live news + earnings + economic calendars (Finnhub)
   │   - Theme coverage block + ticker map
   ▼
pulse-context/latest.json on the pulse-data branch
   │
   ▼
Claude.ai scheduled routine fires (13:08 UTC weekdays)
   │   - Fetches synthesis-routine.md from the working branch
   │   - Fetches latest.json
   │   - Runs the 8-step routine (DRAFT → STITCH → EDIT → LINT → SCRUB → commit → QC → confirm)
   │   - Commits artifacts to pulse-output/* on the pulse-data branch
   ▼
Bridge worker on Railway polls pulse-output/pending/ every 60s
   │   - Posts to all configured Discord channels (or filtered to target_channels)
   │   - Archives on success → pulse-output/archive/
   │   - On delivery failure: retries up to 15 min, then moves to pulse-output/delivery-failed/
   ▼
Discord post lands in #market-pulse
```

---

## 3. Stage 1 — PDF ingestion

[`dropbox_client/watcher.py`](../dropbox_client/watcher.py) — polls Dropbox `/Current` every 15 min using cursor-based delta API. Only newly-uploaded files are downloaded; the cursor persists in `dropbox_state` table.

- Dropbox structure: `/Current/2026/<Month>/<MonDay>/<BankName>/...`
- Volume: ~100-200 PDFs/day across all sources
- Download path: `/data/pdfs/<filename>` on the Railway volume
- Tracked in `pdf_files` table with status: `DOWNLOADED → PROCESSING → PROCESSED / FAILED`

---

## 4. Stage 2 — Per-PDF analysis (Gemini)

Two-pass extraction, both text-only (no multimodal — see decisions below).

### 4.1 Triage (~2K tokens)

Quick classification. Prompt: [`ai_analysis/prompts.py:TRIAGE_*`](../ai_analysis/prompts.py).

Outputs (`TriageResult`):
- `priority`: high | medium | low
- `report_type`: equity_research | macro | crypto | morning_briefing | etc.
- `key_tickers`: list of mentioned tickers
- `summary`: 1-2 sentences
- `source`: bank/publisher name

LOW-priority criteria: peripheral EM, minor FX pairs, niche commodities, single-stock regional research, credit without spread calls, technical-only analysis, historical wrap-ups. Filtered out before synthesis.

### 4.2 Deep analysis (~15K tokens)

Full document text fed to Gemini with a strict structured-JSON schema. Prompt: `DAILY_SYNTHESIS_*` and `PdfAnalysis` schema.

Output fields (most important):
- **`key_insights`** — bulleted findings
- **`theme_stances`** — bank's directional view on 1-3 cross-bank themes with stance + conviction + evidence quote (anti-hallucination anchor)
- **`contextual_mentions`** — topical mentions throughout the report that weren't promoted to a theme_stance (powers Phase B discovery)
- **`market_movers`** — rating changes / top calls
- **`sector_views`** — overweight/neutral/underweight stances
- **`macro_indicators`** — print readings + interpretation
- **`crypto_views`** — crypto-specific commentary
- **`trade_ideas`** — explicit trade structures with rationale + risk + conviction + time horizon
- **`risk_factors`** — what could break the call
- **`vol_and_positioning`** — vol levels, positioning percentiles
- **`geopolitical`** — Mideast, Russia/Ukraine, China, etc.
- **`cross_bank_references`** — explicit references to other banks' calls
- **`entities_mentioned`** — every company / crypto / index for cashtag formatting (`$NVDA`, `$BTC`, etc.)
- **`key_data_points`** — every specific numeric figure (capex, yields, percentiles, dissent counts)
- **`tension_points`** — explicit bull-vs-bear framing where the report presents both sides

Anti-hallucination rules baked into the prompt:
- `theme_stances.evidence` must be a verbatim ≤15-word phrase from the report.
- Empty list is correct and common — don't fabricate.
- `vs_consensus` only fills with explicit consensus language.
- `conviction=high` only with explicit markers.

### 4.3 Why text-only

Multimodal was tried and dropped. Text in research adequately summarizes chart takeaways for synthesis purposes; image rendering added cost/latency without measurable improvement. Code in [`pdf_processing/page_selector.py`](../pdf_processing/page_selector.py) and image rendering in [`pdf_processing/extractor.py`](../pdf_processing/extractor.py) exists but isn't invoked.

### 4.4 AI model

**Google Gemini 3.1 Flash Lite** (`google-genai` SDK). Same model for triage, deep analysis, and synthesis. Cost: ~$0.10/M input, $0.40/M output. **Not** Anthropic/Claude — that's the routine layer (next stage).

---

## 5. Stage 3 — Cross-PDF aggregation

[`report/synthesizer.py:build_pulse_context`](../report/synthesizer.py) runs every 15 min on Railway. Pulls the last 24h of analyses (or wider if last scheduled pulse was further back, capped at 96h), filters out LOW priority, and constructs a synthesis-ready context.

### 5.1 Phase A — theme_stance clustering (embeddings)

[`report/theme_clusterer.py:cluster_themes`](../report/theme_clusterer.py).

For each theme tag extracted across all PDFs:
1. **Embed** the tag + key_argument (for disambiguation context) via `gemini-embedding-001`.
2. **Greedy agglomerative cluster** on cosine similarity (threshold 0.78). Picked over HDBSCAN — stable on small samples (~30-90 strings/pulse), one tunable parameter.
3. **LLM canonical label** per cluster via `gemini-2.5-flash-lite`. Picks the best name from cluster members (parallelized via thread pool).

Why embeddings: the prior anchor-list + substring-match merge had over-merge problems (e.g., `iran nuclear deal` + `iran oil exports` shared the "iran" anchor and would collapse into one theme despite being distinct stories). Semantic clustering keeps them apart while merging genuine variants like `ai hyperscaler capex super cycle` ↔ `hyperscaler capex boom`.

### 5.2 Phase B — corpus-level discovery (embeddings)

[`report/theme_clusterer.py:discover_uncovered_clusters`](../report/theme_clusterer.py).

The structural fix for distributed-mention failures (e.g., 7 banks all reference "US strikes on Iranian targets" inside `risk_factors` / `geopolitical` / `macro_indicators` but no single PDF promotes it to a `theme_stance`).

For each PDF's `contextual_mentions`:
1. **Cluster** mentions across the entire corpus via embeddings + greedy clustering.
2. For each cluster, compute max cosine similarity to any **canonical Phase-A label**.
3. If max similarity < threshold (covered by existing theme), AND cluster spans **≥3 distinct banks** (min promotion floor), promote as a "discovered" theme.
4. Stance is `neutral` (contextual mentions don't carry a directional view).

Output `discovery_audit` (always populated):
- `phase_b_ran: bool`
- `pdfs_in_window`, `pdfs_with_contextual_mentions`, `total_mentions`
- `phase_a_theme_count`
- `promoted: list[dict]` — promoted clusters with banks, pdf_ids, max_covered_sim
- `near_miss: list[dict]` — clusters that almost surfaced (covered or thin)

### 5.3 Bank-deduplicated stance counts

A bank with 5 supportive PDFs on the same theme contributes **1** to `supportive`, not 5. Prevents one bank's house view (re-articulated across desk notes) from inflating cross-bank consensus signals.

### 5.4 Non-bank-only theme flagging

Themes whose only sources are non-bank publications (TME, Bloomberg news wire, Reuters, "Unknown") get `non_bank_only: True`. The synthesis prompt sees these in a separate section of the theme coverage block with explicit framing: "DO NOT promote these to primary INSIGHTS themes without multi-bank corroboration — they're color, not underwritten analysis."

### 5.5 Output: `pulse-context/latest.json` on the `pulse-data` branch

Top-level keys:
- `today`, `today_label`, `now_label`, `is_weekend`, `session_status`
- `pdf_count`
- `analyses_json` — full per-PDF structured JSON
- `market_snapshot` — live Binance.US + Finnhub /quote prices
- `news_snapshot` — Finnhub general market news (last 48h)
- `earnings_calendar` — hard-filtered to MAG7 + top banks + bellwethers (already-released vs upcoming)
- `economic_calendar` — hard-filtered to FOMC/Powell/CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI + ECB/BOJ/BOE rate decisions (already-released vs upcoming)
- `ticker_block` — dedup ticker lookup with cashtag rules
- `theme_coverage` — formatted block: primary themes / discovered themes / non-bank-only themes
- `theme_map` — structured form (used by routine adjudication)
- `discovery_audit` — Phase B audit (promoted + near_miss + structured fields)

---

## 6. Stage 4 — Synthesis routine (Claude.ai)

The synthesis runs on Claude.ai infrastructure (the model is Claude Opus 4.7), not on Railway. Reason: Anthropic's API is egress-blocked from Railway, and the routine session has rich tooling (parallel sub-agent dispatch, longer context).

### 6.1 Bootstrap pattern

The Claude.ai scheduled-routine config is **minimal** — just a wrapper that:
1. Writes `/tmp/gh_token.txt` and `/tmp/target_channels.txt` (env vars don't persist across sandbox Bash calls)
2. `curl`s the latest [`synthesis-routine.md`](superpowers/routines/synthesis-routine.md) from GitHub
3. Tells the agent: "execute every step exactly as written"

The actual logic lives in version-controlled markdown. Editing the routine = edit the markdown + push. No Claude.ai config dance needed.

### 6.2 The 8 steps

#### STEP 1 — Read prompts
Reads `ai_analysis/prompts.py` to load the prompt strings (`ADJUDICATION_*`, `DRAFT_*`, `AUDIT_*`, `SCRUB_*`, `QC_*`).

#### STEP 2 — Fetch context (retry + failure markers)
- Retry-with-backoff (5s/15s/30s) on 404/502/503/504
- Token-via-file fallback for env-non-persistence
- Pins pulse_ts to `/tmp/pulse_ts.txt` for downstream artifact pairing
- Writes `/tmp/progress.py` helper script
- Tee logging to `/tmp/routine.log`
- On terminal failure: commits structured marker to `pulse-output/qc-reviews/<ts>.md` with stage + reason + log tail + /tmp listing + progress events + env summary

#### STEP 3 — Inspect theme coverage
Echo the formatted `theme_coverage` block. Used as a forcing function: top-bank-count themes MUST appear in INSIGHTS unless conviction-disqualified.

#### STEP 3.5 — Adjudicate top themes (parallel sub-agents)
Picks top 8 themes with banks ≥ 2. For each:
- Builds per-theme inputs (theme_stances + tension_points + key_data_points)
- Dispatches a `general-purpose` Agent in parallel
- Sub-agent emits structured JSON (consensus_view + facts_agreed + facts_contested + falsifiable_predictions)

Lint validates each sub-agent output:
- Rule 0: empty inputs → output must be empty (anti-fabrication)
- Rule 1: evidence_quotes must verbatim-match a `theme_stances.evidence` from inputs
- Rule 2: banks must appear in input sources
- Rule 3: falsifiable_predictions claims must trace to inputs
- Rule 4: stance_counts must match pre-aggregated counts exactly
- Rule 5: facts_agreed entries must have ≥2 banks_for (drop single-bank claims)
- Rule 6: deadlines must be ISO date or short conditional substring (not noun-phrase descriptions)

Validated JSON is concatenated and saved to `/tmp/adjudication.json`.

#### STEP 4 — DRAFT
Long-form synthesis from per-PDF inputs + adjudicated themes block. System prompt: `DRAFT_SYSTEM`. The DRAFT focuses on accuracy/density: every sentence carries a number, named bank, level, or specific call. Voice polish is delegated to EDIT.

Output structure (5-movement INSIGHTS):
- Italicized one-line punchline
- 3-5 bullet data points
- Mechanism prose paragraph (2-3 sentences arguing from bullets)
- Bull/pushback/defense/positioning paragraph
- Trade implication close (long $TICKER, etc.)

Saved to `/tmp/draft.md`.

#### STEP 5a — STITCH (mechanical)
[`scripts/pulse_stitch.py`](../scripts/pulse_stitch.py). Foreign cashtag scrub (`$TSCO` → "Tesco", etc.) and ETF normalization (`$SPX` → `$SPY`, `$NDX` → `$QQQ`). No LLM. Deterministic. Logs each fix or "no mechanical fixes needed."

Saved to `/tmp/stitched.md`.

#### STEP 5b — EDIT (sub-agent)
Fresh-context `general-purpose` Agent. Prompt: `AUDIT_SYSTEM` + `AUDIT_USER` with stitched.md as `{draft_markdown}`. Saves prompt to `/tmp/agent_io/edit-prompt.txt` for forensic review.

EDIT applies the full editorial pipeline: RECAP rebuild from live data, Pass A cull, Pass A.5 density, Pass B close, voice scrub. Returns the revised markdown.

Saved to `/tmp/final.md`.

#### STEP 5.5 — LINT
[`scripts/pulse_lint.py`](../scripts/pulse_lint.py). Deterministic regex scan against `voice_rules.py` patterns. Categories:
- em-dash, semicolon (banned punctuation)
- AI-tells ("the cleanest read", "the mechanism is straightforward", "Today's price action", etc.)
- AI-cliche-verbs ("delve", "navigate" as verb)
- AI-cliche-adjectives ("robust")
- meta-narration ("cross-bank consensus", "research suggests")
- source-prefix ("Goldman says", "JPM notes that")
- banned-publication (TME, Market Ear, FX Daily)
- jargon (every entry in `JARGON_WITH_TRANSLATIONS`)
- top-3-theme-missing (soft structural check)

Output: `/tmp/lint_report.json`.

#### STEP 5.7 — SCRUB (lint-driven sub-agent)
Conditional. If lint has any HARD issues (kind != `top-3-theme-missing`), dispatch SCRUB. Fresh-context Agent. Prompt: `SCRUB_SYSTEM` + `SCRUB_USER` with lint report + final.md.

SCRUB rewrites ONLY the flagged sentences. Doesn't add/remove themes, doesn't change facts. Pre-SCRUB final.md saved as `/tmp/pre_scrub_final.md` for QC visibility.

Re-lint. Up to 2 SCRUB iterations max. Even if residuals remain, ship.

#### STEP 6 — Commit pulse + artifacts
Composes frontmatter (pdf_count, tokens, dumped_at_utc, optional target_channels). Commits BOTH:
- `pulse-output/pending/<ts>.md` — the pulse markdown (what the bridge picks up)
- `pulse-output/pending-adjudications/<ts>.json` — adjudication audit

Plus forensics chain:
- `pulse-output/drafts/<ts>.md` — pre-stitch DRAFT
- `pulse-output/stitched/<ts>.md` — post-STITCH pre-EDIT
- `pulse-output/scrubbed/<ts>.md` — post-EDIT post-SCRUB (if SCRUB ran)
- `pulse-output/pre-scrub/<ts>.md` — post-EDIT pre-SCRUB (forensic diff target)
- `pulse-output/lint/<ts>.json` — final lint report
- `pulse-output/agent-io/<ts>/edit-prompt.txt` — EDIT sub-agent prompt
- `pulse-output/agent-io/<ts>/scrub-prompt.txt` — SCRUB sub-agent prompt (if SCRUB ran)

#### STEP 7 — QC self-review (sub-agent)
Fresh-context `general-purpose` Agent reviews the run end-to-end. Prompt: `QC_SYSTEM` + `QC_USER`. Input includes:
- `theme_coverage` block
- `discovery_audit_json` (Phase B promoted + near-miss)
- Adjudication summary + discard reasons
- `lint_summary_json` (post-SCRUB)
- `handoffs_summary` — sub-agent dispatch counts + I/O sizes per stage + EDIT fact-provenance check (numeric tokens in final not in stitched/live-data)
- `draft_md` / `stitched_md` / `pre_scrub_md` / `final_md` — full markdown at each stage

Output: structured QC review markdown with sections:
- TL;DR (one sentence)
- Coverage audit
- Workflow stage critique
- Voice + structure
- Accuracy + sourcing
- Suggested changes for next run (file path + specific change)
- Signals worth tracking (forward-looking flags for next 1-3 runs)

Committed to `pulse-output/qc-reviews/<ts>.md` (unified quality artifact). Failure path: if QC sub-agent produced no output, commits a structured failure marker with the same filename — the QC slot is always filled.

#### STEP 8 — Confirm
Reports back to the routine session: pulse filename + commit shas + pdf_count + word count + top-3 INSIGHT theme headers + adjudication summary + QC review filename.

### 6.3 Progress events

Each major step commits a progress event to `pulse-output/progress/<ts>.json` (running list). The watcher polls this and emits notifications:
- `STEP_2_DONE`
- `STEP_3_5_DONE`
- (and so on)

Lets a human or automated observer see live status during the ~15-25 min routine.

---

## 7. Stage 5 — Discord delivery (bridge worker)

[`github_bridge/jobs.py`](../github_bridge/jobs.py). Runs on Railway as part of the main worker process.

### 7.1 Polling

Two scheduled jobs:
- **`dump_context_job`** every 15 min — runs `build_pulse_context()` and commits `pulse-context/latest.json` to the `pulse-data` branch.
- **`post_pending_pulses_job`** every 60s — polls `pulse-output/pending/` for new pulse markdown.

### 7.2 Delivery flow

For each pending pulse:
1. **Fetch and parse frontmatter** — extract pdf_count, tokens, target_channels.
2. **Fetch matching adjudication** — `pulse-output/pending-adjudications/<base>.json` (paired by base name).
3. **Apply target_channels filter** — substring match against configured Discord channel names. No filter = all configured channels.
4. **Build embeds** — [`report/formatter.py`](../report/formatter.py) splits pulse markdown into Discord embeds: header (gold, with title + date in markdown), section embeds (RECAP gold, INSIGHTS blue, WHAT TO WATCH orange), footer with stats (top sources, priority mix, research date range, next pulse time).
5. **Post to each target channel** — collect per-channel errors.
6. **Decide outcome:**
   - **All success** → archive markdown to `pulse-output/archive/<ts>.md`, persist to `daily_reports` table, archive adjudication.
   - **Partial success** → archive markdown anyway, log partial.
   - **Zero successes** + age ≤ 15 min → leave in pending/, retry next poll.
   - **Zero successes** + age > 15 min → commit `pulse-output/qc-reviews/<ts>.delivery.md` (structured marker), move pulse to `pulse-output/delivery-failed/<ts>.md`, remove from pending.
   - **No matched channels** (config error, target_channels filter matches nothing) → immediate move to delivery-failed/.

### 7.3 Discord login resilience

[`main.py`](../main.py) wraps `bot.login()` with retry-with-exponential-backoff (8 attempts, 30s → 30min cap). Reason: Railway redeploys can trigger Discord 429 rate limits on bot login, which previously took the worker into a 5-retry crashloop and then permanent failure. The retry rides out transient 429s instead.

---

## 8. Voice and quality layer

[`ai_analysis/voice_rules.py`](../ai_analysis/voice_rules.py) is the **single source of truth** for banned patterns. Both the AUDIT/SCRUB prompts and the LINT regex scanner import from it — updates propagate without drift.

### 8.1 Banned pattern lists

- `BANNED_PUNCTUATION` — em-dash, semicolon (use commas, periods, parentheses)
- `BANNED_FILLER_PHRASES` — "it's worth noting," "notably," "Furthermore," "Meanwhile," etc.
- `BANNED_AI_CLICHE_VERBS` — "delve," "leverage" (verb), "navigate"
- `BANNED_AI_CLICHE_ADJECTIVES` — "robust"
- `BANNED_HEDGING_WEASELS` — "could potentially," "may or may not"
- `BANNED_WRAPUP_SENTENCES` — "Overall," "In summary," "All told"
- `BANNED_AI_TELLS` — template-default phrases the model latches onto and overuses ("the cleanest read," "the pushback we would anticipate," "the mechanism is straightforward," etc.)
- `BANNED_META_NARRATION` — "cross-bank consensus is firming," "8+ notes flag," "research suggests"
- `BANNED_PUBLICATION_NAMES` — TME, Market Ear, FX Daily (never attributed in prose)
- `SOURCE_PREFIX_BANKS` × `SOURCE_PREFIX_VERBS` — composite pattern catching "Goldman says," "JPM notes that," "Mizuho keeps hammering" patterns

### 8.2 Tier-1 weighting + non-bank flagging

- `TIER_1_BANKS = ["JPMorgan", "Bank of America", "Goldman Sachs"]` — content guidance for synthesis. When Tier-1 disagrees with Tier-2, Tier-1 leads.
- `NON_BANK_SOURCES = {"TME", "Market Ear", "Bloomberg", "Reuters", "Unknown", ...}` — themes sourced ONLY from these get flagged `non_bank_only=True` and surfaced in a separate theme-coverage bucket: useful for color, not for INSIGHTS lead.

### 8.3 Jargon translation

`JARGON_WITH_TRANSLATIONS` — 40+ terms (duration, breakevens, convexity, NII, bps, gamma, basis, CTAs, "got hit," "caught a bid") with plain-English equivalents. The audit pass walks every paragraph; if a key appears without translation context, AUDIT either substitutes the value verbatim or restructures the sentence to drop the jargon. Recent example: `"CTAs" → "systematic trend-followers (CTAs)"` (4-word self-defining version replaced an earlier 13-word translation that broke prose rhythm).

### 8.4 The lint pipeline

`scripts/pulse_lint.py` runs on the final markdown post-EDIT (and again post-SCRUB):
1. Strip fenced code blocks
2. Iterate `compose_lint_patterns()` — each (pattern, kind) becomes a finditer scan
3. Iterate `compose_jargon_lint_patterns()` — soft hits, surfaced for review
4. Soft top-3-theme-missing structural check (any significant word from each top theme appears anywhere in INSIGHTS section)

Output: `/tmp/lint_report.json` with line + kind + 80-char snippet per issue.

Pattern count: 65+ as of this writing.

---

## 9. Observability — unified QC layout

The user-facing model: **everything that affects pulse quality goes into `pulse-output/qc-reviews/<ts>.md`.** One place for any human reviewer to look.

```
pulse-output/
├── pending/<ts>.md                    ← pulse markdown awaiting bridge pickup
├── pending-adjudications/<ts>.json    ← adjudication audit awaiting pickup
├── archive/<ts>.md                    ← successfully delivered pulse
├── archive-adjudications/<ts>.json    ← archived adjudication
├── delivery-failed/<ts>.md            ← pulse markdown after retry exhaustion (operational, manual recovery)
├── drafts/<ts>.md                     ← pre-STITCH DRAFT
├── stitched/<ts>.md                   ← post-STITCH pre-EDIT
├── pre-scrub/<ts>.md                  ← post-EDIT pre-SCRUB
├── scrubbed/<ts>.md                   ← post-EDIT post-SCRUB (= final, when SCRUB ran)
├── lint/<ts>.json                     ← final lint report
├── agent-io/<ts>/                     ← sub-agent prompts (forensics)
│   ├── edit-prompt.txt
│   └── scrub-prompt.txt
├── progress/<ts>.json                 ← live step-by-step events
└── qc-reviews/                        ← UNIFIED QUALITY ARTIFACTS
    ├── <ts>.md                        ← QC sub-agent's review (success) OR routine-failure marker
    └── <ts>.delivery.md               ← bridge delivery sidecar (only when delivery failed)
```

### 9.1 What the QC review surfaces

Beyond content quality, the QC sub-agent assesses workflow:
- **Coverage audit** — were all heavyweight themes from `theme_coverage` actually surfaced? Were discovered themes correctly promoted? Are near-miss clusters worth surfacing despite filter?
- **Workflow stage critique** — per-stage assessment, but only for stages that did something noteworthy. Skip clean stages.
- **Voice + structure** — tells that slipped through, theme-coherence breaks, misframings (theme presented as bull-consensus when corpus is split).
- **Accuracy + sourcing** — numbers in final that don't trace to corpus or live data, single-bank claims presented as consensus, forecasts dressed up as actuals. Powered by the `handoffs_summary` block which automatically computes EDIT-introduced unverified numbers.
- **Suggested changes for next run** — concrete file path + rule change (e.g., "ai_analysis/voice_rules.py: add `the mechanism is straightforward` to BANNED_AI_TELLS").
- **Signals worth tracking** — forward-looking flags for next 1-3 runs.

### 9.2 Failure markers

When STEP 2/6/7 fails (or the QC sub-agent produces no output), a structured marker lands at `pulse-output/qc-reviews/<ts>.md` formatted as a QC review with `## Status: FAILED at <stage>` plus:
- Exception traceback
- Last 120 lines of `/tmp/routine.log` (tee'd from each step)
- `/tmp` artifact listing with sizes + mtimes (which steps got far enough to write)
- Progress events committed before the abort
- Environment summary (token presence, target_channels, etc.)

When the bridge fails delivery: `pulse-output/qc-reviews/<ts>.delivery.md` sidecar with `## Status: DELIVERY FAILED (bridge)`, target_channels filter, channel match counts, per-channel errors (Discord 503s, channel-not-found, etc.), and recovery instructions.

### 9.3 Watcher

[`scripts/routine_watcher.py`](../scripts/routine_watcher.py) — polls `pulse-output/qc-reviews/`, `pulse-output/archive/`, and the latest `pulse-output/progress/<ts>.json` every 45s. Emits one stdout line per new event:
- `PROGRESS: step=<name> (file=<ts>.json)`
- `QC_REVIEW: <filename>`
- `POSTED: <filename>`

Designed to be invoked under a Monitor tool or any process that converts stdout lines into notifications.

---

## 10. Discord commands

Password gate: `COMMAND_PASSWORD=<set-in-railway-env>` env var. Gated commands take a `password` arg.

### Open commands

- **`/pulse [hours:N]`** — manual pulse synthesis. Default 24h, max 168h. Fully standalone (no prev-pulse diff context). Uses Gemini directly (not the routine pipeline) — the routine pulse is only the scheduled one.
- **`/status`** — health dashboard: today's ingestion + total DB state + priority mix (always shows high/medium/low) + upload range + all-time tokens + last pulse times + Dropbox cursor state + upload volume (24h + since last scheduled) + last 5 ingested filenames.

### Gated commands

- **`/load hours:N password:<your-command-password>`** — ingest Dropbox PDFs from last N hours (max 48). Live progress.
- **`/reanalyze hours:N password:<your-command-password>`** — re-analyze PDFs already in DB with the current prompt (appends new pdf_analyses rows; old preserved).
- **`/clearqueue password:<your-command-password> [confirm:true]`** — delete pending rows + local files. Refuses >500 without `confirm:true`.
- **`/seedcursor password:<your-command-password>`** — set Dropbox cursor to "now" so next poll skips backfill.
- **`/reprocess filename:X`** — retry a failed PDF.

---

## 11. Database schema

SQLite with WAL mode at `/data/reports.db` (persists on Railway volume).

| Table | Purpose | Notes |
|---|---|---|
| `dropbox_state` | Cursor for delta polling | One row |
| `pdf_files` | Status tracking | DOWNLOADED → PROCESSING → PROCESSED / FAILED |
| `pdf_analyses` | Append-only structured analysis JSON + token usage | UNIQUE constraint dropped — re-analyses create new rows; queries use `MAX(id) GROUP BY pdf_file_id` to get latest |
| `daily_reports` | Append-only synthesized pulses | UNIQUE(report_date, report_type) dropped. `report_type='daily'` for scheduled, `'manual'` for /pulse |
| `processing_log` | Audit trail | Grows fastest; eventual pruning candidate |

Migration on boot: `_migrate_drop_unique_constraints` rebuilds tables without UNIQUEs if old schema is detected. Append-only model preserves history (re-analysis under new prompts doesn't destroy old extractions).

Timestamp format normalization: SQLite's `datetime('now')` uses space separator; Python's `isoformat()` uses T. Lexical TEXT comparison treats T > space, so mixed-format comparisons silently break. `db._normalize_ts()` is applied at every cutoff comparison site.

---

## 12. Deployment

**Railway** — project `marvelous-dream`, service `worker`, environment `production`. Volume mounted at `/data` (SQLite + temp PDFs). Every `git push` to the working branch auto-redeploys.

Key env vars:
- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`, `DROPBOX_FOLDER_PATH=/Current`
- `GOOGLE_API_KEY`, `GEMINI_MODEL=gemini-3.1-flash-lite`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` (comma-separated channel IDs)
- `FINNHUB_APi_KEY` (case-insensitive due to pydantic-settings)
- `COMMAND_PASSWORD=<set-in-railway-env>`
- `TIMEZONE=America/New_York`
- `DAILY_PULSE_HOUR=9`, `DAILY_PULSE_MINUTE=0` (legacy — actual pulse fires via Claude.ai cron, not Railway scheduler)
- `DB_PATH=/data/reports.db`, `PDF_DOWNLOAD_DIR=/data/pdfs`
- `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRIDGE_BRANCH=pulse-data`
- `PULSE_API_TOKEN` (gates the optional `/api/pulse/*` HTTP endpoints)

Claude.ai routine config:
- Cron: `0 13 * * 1-5` (13:00 UTC = 9:00 AM ET, weekdays only)
- Body: writes `/tmp/gh_token.txt`, fetches `synthesis-routine.md` from the working branch, executes verbatim
- Test fires: body sets `TARGET_CHANNELS='test-channel'` to filter delivery

---

## 13. Cost

Based on actual token usage (Gemini Flash Lite at ~$0.10/M input, $0.40/M output):
- Per pulse synthesis (Gemini path, /pulse command): ~$0.02
- Per day ingestion (~150-200 PDFs, deep analysis only): ~$0.30-0.50
- Routine pulse (Claude Opus 4.7 + sub-agents): ~$0.10-0.20 per fire (5 sub-agents per pulse: ~3 adjudication + EDIT + SCRUB + QC)
- Embeddings (Phase A + Phase B): ~$0.01 per pulse
- LLM canonical-labels: ~$0.01 per pulse (15-20 cluster labels)
- **Monthly total: ~$15-25** (Gemini + routine + Railway $5)

Spend cap at ai.studio/spend.

---

## 14. Key design decisions

### 14.1 Why text-only deep analysis
Multimodal was tried and dropped. Text in research adequately summarizes chart takeaways. Saves cost + latency; multimodal didn't measurably improve synthesis quality.

### 14.2 Why Gemini-only priority (no override rules)
Earlier versions had a Tier-1 floor (GS/JPM/BofA/MS = min MEDIUM) and HIGH topic boost (macro/crypto/vol_commentary = force HIGH). Removed in favor of Gemini's call verbatim, with expanded LOW criteria in the triage prompt. Cleaner; trusts the model.

### 14.3 Why the GitHub-as-message-bus pattern
Anthropic API is egress-blocked from Railway. The routine runs on Claude.ai infrastructure but needs to read pre-prepared context AND write back the synthesized pulse. GitHub branches act as the bidirectional message bus: Railway dumps context to `pulse-context/latest.json`, routine fetches via `raw.githubusercontent.com`, routine commits pulse to `pulse-output/pending/`, Railway bridge worker polls and posts to Discord.

### 14.4 Why embeddings instead of anchor lists
The prior anchor-list merge had two problems: (a) over-merge from generic anchors ("iran" merged "iran nuclear deal" + "iran oil exports"), (b) silent under-merge when banks used different vocabulary. Embeddings + LLM canonical labels generalize without per-topic maintenance and produce informative cluster labels (e.g., "fed rate cut expectations" combining "rate cut repricing" + "fed dovish surprise").

### 14.5 Why single-source-of-truth voice rules
[`voice_rules.py`](../ai_analysis/voice_rules.py) is imported by both the AUDIT/SCRUB prompts (via composer functions) and the LINT regex scanner. Updating a banned phrase propagates to both. Without this, prompt rules and lint rules drifted apart.

### 14.6 Why STITCH + EDIT split
STITCH handles mechanical/deterministic fixes (foreign cashtag scrub, ETF normalization). EDIT is the judgment-based editorial pass dispatched as a fresh-context sub-agent. Each does one thing well: STITCH can't accidentally drop a theme; EDIT can't accidentally miss a foreign cashtag.

### 14.7 Why SCRUB is lint-driven sub-agent (not in-prompt rules)
EDIT's prompt has voice rules but model attention dilutes across many concerns. SCRUB receives ONLY the lint report + the markdown — no other concerns competing for attention. This catches voice tells the editorial pass missed.

### 14.8 Why QC is fresh-context sub-agent
The routine spent 5+ stages making editorial decisions, so its judgment is biased toward defending them. A fresh-context sub-agent reads the artifacts (theme_coverage, discovery_audit, draft, stitched, pre-SCRUB, final, lint, handoffs_summary) without that bias. The QC review becomes the unified observability artifact.

---

## 15. Glossary

| Term | What it means here |
|---|---|
| **Pulse** | The synthesized daily Discord post |
| **Routine** | The Claude.ai scheduled-job session that runs the synthesis pipeline |
| **Bridge worker** | The Railway process that polls GitHub for pending pulses and posts to Discord |
| **Phase A** | Theme clustering using `theme_stances` (primary themes the analyst put a stance on) |
| **Phase B** | Discovery clustering using `contextual_mentions` (topics mentioned but not promoted to a theme) |
| **DRAFT / STITCH / EDIT / LINT / SCRUB / QC** | The 6 routine sub-stages that transform the context into the final pulse |
| **theme_coverage block** | Formatted list of themes with bank counts, stance breakdown, and bucketing (primary / discovered / non-bank-only) |
| **handoffs_summary** | Section in QC inputs showing what each sub-agent received, returned, and whether it engaged with its inputs |
| **Tier-1 banks** | JPMorgan, Bank of America, Goldman Sachs — get content lead when consensus is split |
| **Non-bank source** | TME, Bloomberg news wire, Reuters, "Unknown" — useful for color but not for INSIGHTS lead |
| **Pulse_ts** | The timestamp pinned at routine fire-start; used to pair all artifacts for the same fire |

---

*This document reflects the system as of 2026-05-08. The pipeline is under active development; check git log for changes since.*
