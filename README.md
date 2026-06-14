# Institutional Research PDF Analyzer + Discord Market Pulse Bot

Turns 100-200 institutional research PDFs a day into a single Discord-delivered Market Pulse for self-directed US options and crypto traders.

The bot watches a Dropbox folder where sell-side research lands (Goldman, JPM, Citi, BofA, UBS, RBC, Barclays, Deutsche Bank, The Market Ear, and ~15 other shops), runs each PDF through Gemini for structured extraction, then synthesizes a cross-bank pulse delivered to Discord at 9:00 AM ET on weekdays. The pulse is also runnable on demand via `/pulse` slash commands.

**Live deployment:** Railway project `marvelous-dream`, service `worker`.

---

## What it does

The product output is a three-section Discord post (gold + blue + orange embeds):

1. **RECAP** — narrative lede + bulleted drivers. Live market prices ($SPY/$QQQ/$BTC/etc.) grounded against the day's news and any `[RELEASED]` economic prints.
2. **INSIGHTS & ALPHA** — 3-6 themes synthesized from cross-bank coverage. Each theme: italicized punchline, 3-5 data-point bullets, mechanism prose, bull-vs-pushback paragraph, and a direct trade-idea close (e.g., *"Long $TLT into NFP."*).
3. **WHAT TO WATCH** — `### Today` and `### This Week` subsections. Calendar is hard-filtered at the data layer (only FOMC/Powell/CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI plus MAG7 / big-bank earnings reach the synthesis prompt).

Voice is trader-newsletter, not bank-analyst. Every jargon term is translated inline. No em-dashes, no semicolons, no "it's worth noting" / "notably" / "moreover" AI-tells. Cashtags ($AAPL, $BTC, $SPY) for US stocks/ETFs/crypto/indexes only; FX pairs and commodity spot reference by name.

---

## Architecture

```
Dropbox /Current
   │   every 15 min — cursor-based delta polling
   ▼
Railway worker downloads PDFs to /data volume
   │
   ▼
pdf_files row created (status=DOWNLOADED)
   │   every 5 min — async processing queue
   ▼
PyMuPDF extracts full text (text-only — image rendering removed)
   │
   ▼
Triage (Gemini Flash Lite, ~2K tokens) → priority + source + tickers
   │
   ▼
Deep analysis (Gemini Flash Lite, full doc text, ~15K tokens)
   │   → structured JSON: theme_stances, contextual_mentions,
   │     key_data_points, market_movers, macro_indicators,
   │     trade_ideas, tension_points, entities_mentioned, etc.
   ▼
pdf_analyses row (append-only — reanalyses preserved as history)
   │
   │   every 15 min — pulse-context dump
   ▼
build_pulse_context() on Railway worker
   │   - Phase A theme clustering (embeddings on theme_stances)
   │   - Phase B discovery clustering (embeddings on contextual_mentions)
   │   - Two-tier merge: collapse Phase A fragments via Phase B topic centroids
   │   - Live market snapshot (Binance.US for crypto, Finnhub /quote for stocks)
   │   - Live news + earnings + economic calendars (Finnhub, hard-filtered)
   ▼
pulse-context/latest.json on the pulse-data branch (GitHub-as-message-bus)
   │
   ▼
Claude.ai scheduled routine fires
   │   STEP 1: read prompts from repo (ai_analysis/prompts.py)
   │   STEP 2: fetch latest.json (with retry + structured failure markers)
   │   STEP 3.5: adjudicate top themes via parallel sub-agents
   │             (one Agent per theme — fabrication-resistant lint)
   │   STEP 4: DRAFT prose from adjudicated themes + per-PDF JSON
   │   STEP 5: STITCH (deterministic) + EDIT (fresh-eyes sub-agent)
   │   STEP 5.5: LINT (regex scan against voice_rules.py)
   │   STEP 5.7: SCRUB (lint-driven sub-agent, max 2 iterations)
   │   STEP 6: commit pulse + adjudication + intermediate artifacts
   │   STEP 7: QC self-review sub-agent (post-run critique, day-over-day diff)
   ▼
pulse-output/pending/<ts>.md on the pulse-data branch
   │
   ▼
Bridge worker on Railway sees new pending file
   │   - Posts to every channel in DISCORD_CHANNEL_ID
   │   - Archives to pulse-output/archive/ on success
   │   - On Discord 5xx: retries for 15 min, then writes a structured
   │     delivery-failure marker to pulse-output/qc-reviews/<ts>.delivery.md
   ▼
Discord
```

A few things to call out about that diagram:

- **No multimodal.** Deep analysis sends the full document as text. Image rendering and page selection code exist but aren't invoked — the text in research adequately summarizes chart takeaways and removing multimodal cut latency + cost meaningfully.
- **GitHub-as-message-bus.** Railway's egress can't reach the Anthropic API directly, so the synthesis routine runs on Claude.ai scheduled routines (Opus 4.7, 1M context). The two sides communicate by committing JSON / markdown to the `pulse-data` branch.
- **Append-only DBs.** `pdf_analyses` and `daily_reports` have no `UNIQUE` constraints; reanalyses and pulse re-runs create new rows with the old ones preserved as history. Queries use `MAX(id) GROUP BY pdf_file_id` to pick the latest.

---

## Theme clustering (the engine that decides what leads the pulse)

The hardest editorial problem is "across 200 PDFs from 20 banks, which 5 topics actually matter today?" The answer is built in three stages by [report/synthesizer.py](report/synthesizer.py) + [report/theme_clusterer.py](report/theme_clusterer.py):

**Phase A — stance clustering.** Every PDF emits 1-3 `theme_stances` (theme label + supportive/skeptical/neutral + key_argument). Phase A embeds these labels via `gemini-embedding-001` and runs greedy agglomerative clustering at cosine 0.75. Output: cross-bank theme groups with bank-deduplicated stance counts. *"AI hyperscaler capex super-cycle"* and *"hyperscaler capex boom"* merge into one canonical via embeddings; *"China rate cuts"* and *"Fed rate cuts"* do not.

**Phase B — discovery clustering.** Every PDF also emits `contextual_mentions` (offhand references in risk_factors, geopolitical, macro_indicators). Phase B clusters these mentions across the whole corpus and promotes any cluster spanning ≥3 distinct banks that's NOT already covered by Phase A. This catches the *"Iran strikes on Qeshm port"* class — broadly mentioned in context, never tagged as a primary theme by any single bank.

**Two-tier merge.** Phase A frequently fragments a single subject when banks phrase their views differently. *"Hormuz peace deal"* + *"Iran risk premium"* + *"Middle East supply shock"* are three Phase A clusters, one bank each, that all sit just below the pairwise merge threshold. Phase B's broader topic clustering bridges them: when one Phase B cluster's centroid is within threshold of ≥2 Phase A labels, those labels collapse via union-find into one canonical theme. The Phase B cluster's mentioning banks (the 12-15 banks that referenced the topic without staking a primary stance) flow into the merged theme's neutral bucket. Without this, a 15-bank "Strait of Hormuz" topic looked like five thin 1-bank themes to the synthesis ranking and got under-prioritized.

The clustering result feeds two downstream signals:

1. **`theme_coverage` block** in the DRAFT prompt — anchors INSIGHTS ordering. The highest-bank-count themes MUST appear unless conviction-disqualified.
2. **`discovery_audit.promoted` / `near_miss`** — surfaces what Phase B saw. Promoted topics route to WHAT TO WATCH (broad engagement without a stance). Near-miss with `reason: "covered"` shows the Phase A themes that were merged.

---

## Voice + quality enforcement

The pulse goes through five layers of voice/quality enforcement on every run:

| Layer | What | Where |
|---|---|---|
| DRAFT | Initial prose synthesis from research + adjudication. Targets 800-1100 words, 140-180 per theme. | [DRAFT_SYSTEM / DRAFT_USER](ai_analysis/prompts.py) |
| STITCH | Deterministic Python — strips draft-notes section, normalizes foreign cashtags (`$TSCO` → "Tesco"), ETF aliases (`$SPX` → `$SPY`). | [scripts/pulse_stitch.py](scripts/pulse_stitch.py) |
| EDIT | Fresh-eyes sub-agent — rebuilds RECAP with live data, culls weak themes, enforces data-density floors, fixes session framing. | [AUDIT_SYSTEM / AUDIT_USER](ai_analysis/prompts.py) |
| LINT | Deterministic regex scan against [voice_rules.py](ai_analysis/voice_rules.py) banned-pattern lists + coverage checks (top-3 primary + discovered themes must appear). | [scripts/pulse_lint.py](scripts/pulse_lint.py) |
| SCRUB | Lint-driven sub-agent — rewrites exactly the flagged sentences, max 2 iterations. Headers in scope. Can add a WHAT TO WATCH bullet for `discovered-theme-missing` lints. | [SCRUB_SYSTEM / SCRUB_USER](ai_analysis/prompts.py) |

After the pulse posts, a sixth layer runs:

| Layer | What |
|---|---|
| QC self-review | Post-run sub-agent reads the final pulse + day-over-day comparison vs yesterday's pulse and yesterday's QC review. Writes structured critique to `pulse-output/qc-reviews/<ts>.md`. Non-blocking — pulse is already delivered by then. |

Voice rules are a single source of truth in [ai_analysis/voice_rules.py](ai_analysis/voice_rules.py). Updating a banned pattern there propagates to both the AUDIT prompt and the LINT pass.

---

## Adjudication (the fabrication-resistant pre-DRAFT step)

Before DRAFT writes prose, the routine runs **adjudication** — a parallel sub-agent dispatch where each Agent sees only one theme's evidence and emits structured JSON. The output is then lint-validated against the original inputs.

The lint rules:
- Every `evidence_quotes` entry must be a verbatim character-for-character match of one of the input `theme_stances.evidence` strings.
- Every bank in `banks_for` / `banks_against` must appear as a `source` in the input PDFs.
- Every `falsifiable_predictions.claim` must appear as a substring of an input `tension_points.what_invalidates` field or a `key_data_points` figure/metric/context.
- `stance_counts` must exactly equal the pre-aggregated bank-deduplicated counts.
- Single-bank facts are dropped UNLESS the theme has ≥2 banks of supportive OR neutral stance (`max(supportive, neutral)`). Discovery-promoted themes (supportive=0, neutral≥3) pass via the neutral branch.

The lint discards any sub-agent output that fabricates evidence quotes, bank attributions, or stance counts. DRAFT then sees only validated adjudications, which become its grounded factual spine.

---

## Observability

The routine runs ~15 minutes end-to-end and posts a stream of structured events to `pulse-output/progress/<ts>.json` after each step:

- `STEP_2_DONE` (context fetched)
- `STEP_3_DONE` (theme coverage inspected)
- `STEP_3_5_DONE` (adjudication complete)
- `STEP_4_DONE` (DRAFT generated)
- `STEP_5A_STITCH_DONE` / `STEP_5B_EDIT_DONE`
- `STEP_5_5_LINT_DONE`
- `STEP_5_7_SCRUB_DONE`
- `STEP_6_COMMIT_DONE`
- `STEP_7_QC_DONE`

A local watcher script ([scripts/routine_watcher.py](scripts/routine_watcher.py)) polls these every 45s and surfaces them as chat notifications under the `Monitor` tool, so a human can watch a 15-minute fire in near real-time.

If any step fails, the routine commits a structured failure marker to `pulse-output/qc-reviews/<ts>.md` containing the stage, exception, last 120 lines of routine log, `/tmp` artifacts, and committed progress events — diagnosable without spelunking the Claude.ai session log.

The "everything is QC" model: any event that affects a pulse's quality (content critique, routine abort, delivery failure) lands in `pulse-output/qc-reviews/` as either `<ts>.md` (routine view) or `<ts>.delivery.md` (bridge view).

---

## Discord commands

Password gate on state-changing commands: `COMMAND_PASSWORD=<set-in-railway-env>` (env var). Gated commands take `password` arg.

**Gated:**
- `/load hours:N password:<your-command-password>` — ingest Dropbox PDFs from last N hours (max 48). Shows live progress with current filename + last 5 completed.
- `/reanalyze hours:N password:<your-command-password>` — re-analyze PDFs already in DB with current prompt. Queues a persistent background job that survives worker restarts; progress shown in `/status`.
- `/clearqueue password:<your-command-password> [confirm:true]` — delete pending (DOWNLOADED/PROCESSING) rows + local files. Refuses >500 without `confirm:true`.
- `/seedcursor password:<your-command-password>` — set Dropbox cursor to "now" so the next poll skips backfill.

**Open:**
- `/pulse [hours:N]` — manual pulse synthesis. Default 24h, max 168h. Fully standalone (no prev-pulse diff).
- `/status` — dashboard: today's ingestion (in configured timezone), total DB state, priority mix (high/medium/low buckets always shown), upload range, all-time tokens, last pulse times, Dropbox cursor state, upload volume (24h + since last scheduled), last 5 ingested filenames, recent reanalyze jobs.
- `/reprocess filename:X` — retry a failed PDF.

---

## Web embed (cross-repo)

The pulse is also rendered as a daily page on a **separate site**:

**Production:** https://gabjew90.github.io/Stock-market-dashboard/pulse/

That dashboard lives in [gabjew90/Stock-market-dashboard](https://github.com/gabjew90/Stock-market-dashboard). It's a static HTML page (`web/pulse.html` there) that fetches per-pulse HTML fragments + an `archive.json` index from this repo's `pulse-data` branch at runtime. No build-time coupling — the two repos communicate purely via the public URL contract under `pulse-output/web/` (see [`github_bridge/jobs.py :: publish_web_fragment_job`](github_bridge/jobs.py)).

**Boundary:**

| Concern | Owned by |
|---|---|
| What goes INTO each pulse (voice, themes, sections, cashtags) | This repo |
| HTML class structure the fragment emits, `archive.json` schema | This repo |
| Page layout, pagination, # of pulses shown, nav, colors | **Stock-market-dashboard** |
| GitHub Pages hosting + workflow | **Stock-market-dashboard** |

If you want to change how the embed LOOKS (more pulses on a page, different colors, archive list, etc.), work in the Stock-market-dashboard repo. If you want to change what each pulse SAYS, work here. See [CLAUDE.md](CLAUDE.md) (Web embed integration section) for the full coordination rules.

**Backfill policy:** only the most recently archived pulse gets an HTML fragment. Older `archive.json` entries are stubs (no `fragment_url`) and don't render on the page. Going forward, every weekday's new pulse adds a fragment so the current-week view fills in over time.

---

## Deployment

**Railway project `marvelous-dream`, service `worker`, env `production`.** Volume mounted at `/data` for the SQLite database and temporary PDFs. Every push to the working branch auto-redeploys.

Required env vars:
- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN`, `DROPBOX_FOLDER_PATH=/Current`
- `GOOGLE_API_KEY`, `GEMINI_MODEL=gemini-3.1-flash-lite`, `GEMINI_TRIAGE_MODEL=gemini-3.1-flash-lite`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` (comma-separated list)
- `FINNHUB_API_KEY` (with lowercase `i` typo on Railway — pydantic-settings is case-insensitive, don't fix cosmetically)
- `COMMAND_PASSWORD=<set-in-railway-env>`
- `TIMEZONE=America/New_York`
- `DAILY_PULSE_HOUR=9`, `DAILY_PULSE_MINUTE=0`
- `DB_PATH=/data/reports.db`, `PDF_DOWNLOAD_DIR=/data/pdfs` (leading slashes required — relative paths write to ephemeral container storage and get wiped on redeploy)

Costs: ~$15-20/month total (Gemini Flash Lite ingestion + Railway $5 + Claude.ai routine for synthesis).

---

## Repository layout

| Path | Purpose |
|---|---|
| [config.py](config.py) | All settings from env vars via pydantic-settings |
| [db.py](db.py) | SQLite schema + query helpers (WAL mode, append-only tables, persistent reanalyze jobs) |
| [main.py](main.py) | Entry point — boots bot + scheduler with Discord 429 retry-with-backoff |
| [dropbox_client/watcher.py](dropbox_client/watcher.py) | Cursor-based Dropbox delta polling + download |
| [pdf_processing/extractor.py](pdf_processing/extractor.py) | PyMuPDF text extraction |
| [ai_analysis/prompts.py](ai_analysis/prompts.py) | Gemini prompt templates: TRIAGE, ANALYSIS, ADJUDICATION, DRAFT, AUDIT, SCRUB, QC |
| [ai_analysis/voice_rules.py](ai_analysis/voice_rules.py) | Single source of truth for banned voice patterns (regex + jargon lists) |
| [ai_analysis/analyzer.py](ai_analysis/analyzer.py) | Gemini orchestrator (triage + deep analysis) |
| [ai_analysis/models.py](ai_analysis/models.py) | Dataclasses: TriageResult, PdfAnalysis, ThemeStance, TensionPoint, EntityMention, etc. |
| [report/synthesizer.py](report/synthesizer.py) | Cross-PDF aggregation, Phase A/B clustering, two-tier merge, pulse context build |
| [report/theme_clusterer.py](report/theme_clusterer.py) | Embedding-based clustering (Phase A + Phase B with nearby_phase_a tracking) |
| [report/market_data.py](report/market_data.py) | Binance.US + Finnhub `/quote` for live price snapshot |
| [report/news_data.py](report/news_data.py) | Finnhub news + earnings + economic calendar (hard-filtered) |
| [report/formatter.py](report/formatter.py) | Discord embed formatting (3 color-coded sections + dynamic footer) |
| [discord_bot/bot.py](discord_bot/bot.py) | Discord bot with all slash commands |
| [discord_bot/sender.py](discord_bot/sender.py) | Per-embed delivery |
| [github_bridge/jobs.py](github_bridge/jobs.py) | Bridge worker — pulls pulse-output/pending/, posts to Discord, archives or marks delivery failures |
| [pipeline/orchestrator.py](pipeline/orchestrator.py) | End-to-end pipeline coordination + resumable reanalyze jobs |
| [scheduler/jobs.py](scheduler/jobs.py) | APScheduler cron jobs (15-min poll, 5-min process, 60s reanalyze processor, 9 AM ET pulse) |
| [scripts/pulse_stitch.py](scripts/pulse_stitch.py) | Mechanical post-DRAFT normalization |
| [scripts/pulse_lint.py](scripts/pulse_lint.py) | Deterministic markdown linter |
| [scripts/routine_watcher.py](scripts/routine_watcher.py) | Watcher for live routine observability |
| [docs/superpowers/routines/synthesis-routine.md](docs/superpowers/routines/synthesis-routine.md) | Version-controlled source for the Claude.ai routine prompt |
| [docs/METHODOLOGY.md](docs/METHODOLOGY.md) | Long-form walkthrough of the full method |
| [CLAUDE.md](CLAUDE.md) | Project conventions, recent design decisions, ops notes for Claude Code sessions |

---

## Further reading

- [docs/METHODOLOGY.md](docs/METHODOLOGY.md) — the long-form walkthrough. Covers everything in this README in more depth plus the rationale behind each design decision.
- [CLAUDE.md](CLAUDE.md) — codebase conventions, current architecture decisions, recent session context, deployment + Railway access notes.
- [docs/superpowers/routines/synthesis-routine.md](docs/superpowers/routines/synthesis-routine.md) — the canonical version of the synthesis routine prompt. The live Claude.ai routine config is a copy of this; updates need to be propagated manually.
