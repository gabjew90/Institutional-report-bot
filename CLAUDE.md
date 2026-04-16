# CLAUDE.md

## Project Overview

Institutional Research PDF Analyzer + Discord Market Pulse Bot. Processes 100-200 institutional financial research PDFs daily from Dropbox and delivers synthesized trading intelligence to Discord channels.

**Target audience:** self-directed options and crypto traders. Smart but not finance professionals — they don't know terms like "convexity," "term structure," or "NII." Every technical term must be translated.

**Live deployment:** Railway, project `marvelous-dream`, service `worker`. Always-on. Connected to Dropbox + Discord + Gemini + Finnhub.

## Architecture

```
Dropbox (/Current)
  ↓ every 15 min (cursor-based delta polling)
  ↓ dropbox_client/watcher.py — poll_and_download
  ↓ (file lands on /data volume as PDF)
pdf_files row created with status=DOWNLOADED
  ↓ every 5 min (asyncio processing)
  ↓ pdf_processing/extractor.py — PyMuPDF text extraction
  ↓ ai_analysis/analyzer.py — triage (Gemini text-only, ~2K tokens)
  ↓                          — deep analysis (Gemini text-only, full document, ~15K tokens)
  ↓ pdf_analyses row (append; old analyses preserved as history)
  ↓
Scheduled 9:00 AM ET (via APScheduler)
  OR user runs /pulse, /pulse hours:N
  ↓
report/synthesizer.py — builds context (live market data + news + calendar + prev pulse if scheduled)
  ↓ calls Gemini with aggregated per-PDF JSON
  ↓ daily_reports row
  ↓
report/formatter.py — Discord embeds (3 sections)
discord_bot/sender.py — posts to every channel in DISCORD_CHANNEL_ID
```

## AI Model

**Google Gemini 3.1 Flash Lite Preview** (`google-genai` SDK). Env var `GEMINI_MODEL=gemini-3.1-flash-lite-preview`. Same model for triage, deep analysis, synthesis. NOT Anthropic/Claude.

## Key Design Decisions

### Text-only ingestion (multimodal was removed)
Deep analysis sends the full document as text to Gemini. **No image rendering, no page selection, no truncation.** Multimodal was tried and dropped — text in research adequately summarizes chart takeaways. Code still has `page_selector.py` and image rendering in `extractor.py` but they're not invoked in the deep analysis path. Don't re-enable multimodal without explicit user sign-off.

### Gemini-only priority (no source/topic overrides)
`_apply_priority_rules` returns Gemini's call verbatim. Tier-1 floor (GS/JPM/BofA/MS = min MEDIUM) and HIGH topic boost (macro/crypto/vol_commentary/morning_briefing/sales_trading/strategy/derivatives = force HIGH) were removed. Triage prompt has expanded LOW criteria to filter out peripheral EM macro, minor FX pairs, niche commodities, single-stock regional research, credit without spread calls, technical-only analysis, historical wrap-ups.

### Scheduled vs manual pulse behavior
| | Window | Prev-pulse context |
|---|---|---|
| Scheduled 9 AM ET (`run_daily_pulse`) | Since last scheduled pulse | ✅ Diff-framing — skip themes unchanged from yesterday |
| `/pulse` (no args) | **Last 24h always** | ❌ None — fully standalone |
| `/pulse hours:N` | Last N hours (max 168) | ❌ None |

Scheduled pulse updates `daily_reports` with `report_type='daily'` (this is the cutoff anchor for next day's window). Manual /pulse writes `report_type='manual'` so it does not affect the scheduled cadence.

### Timestamp format normalization
SQLite's `datetime('now')` uses space separator (`"2026-04-14 13:00:00"`); Python's `isoformat()` uses T (`"2026-04-14T13:00:00"`). Lexical TEXT comparison treats T > space, so mixed-format comparisons are broken. `db._normalize_ts()` is applied at every cutoff comparison site. `insert_daily_report` explicitly writes T-format going forward.

### Pulse output structure (3 sections)
1. **RECAP** (gold embed) — live market prices + news + this morning's data releases (with ACTUAL values when tagged `[RELEASED]` in Finnhub economic calendar). Only section where live prices/news are used.
2. **INSIGHTS & ALPHA** (blue embed) — flowing prose or bullets, 3-8 themes entirely from research. Prioritizes dedicated single-topic notes (FINRA PDT rule removal, M&A deals, etc.) over broad macro narratives. Calls out consensus vs divergence between banks. Each theme ends with trade implication.
3. **WHAT TO WATCH** (orange embed) — ### Today + ### This Week subsections. **Calendar is filtered hard at the data layer** — only FOMC/Powell/CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI + MAG7/big-bank earnings reach Gemini. Fed governor speeches (non-Powell), regional Fed surveys, minor data, foreign macro are dropped before synthesis.

Footer: dynamic stats (top sources, priority mix always shows high/medium/low, research date range, next pulse time).

### Cashtag formatting + structured entity extraction
Each deep analysis extracts `entities_mentioned: list[{name, ticker, asset_class}]`. Synthesizer aggregates across all PDFs into a dedup ticker lookup and injects into the synthesis prompt. Cashtag rule: `$AAPL`, `$NVDA`, `$BTC`, `$ETH`, `$SPX` etc. for stock/etf/crypto/index. Skip `$` for FX (DXY, EURUSD), commodity spot (Brent, Gold), currencies in prose.

### Writing voice — non-AI prose
User-provided reference sample (Circle/USDC + Anthropic Mythos pieces) lives in the system prompt as a few-shot example. Key traits: conversational, opinionated, story-driven; "optimistic read vs risk" framing; memorable phrasing; close every theme with trade implication. Hard rules against AI tells: em-dashes structural inside sentences, "it's worth noting", "notably", "Meanwhile,", "Furthermore,", hedging, summary wrap-up sentences.

Translation rules — every jargon term must be explained first use. System prompt has a table of 20+ common terms (CTAs, sigma, RSI, NII, bps, CMD, convexity, etc.) with plain-English equivalents.

### Data sources layered at synthesis
1. **Research PDFs = primary content driver** across all sections
2. **Live market data** (CoinGecko BTC/ETH/SOL + Yahoo Finance S&P/VIX/oil/gold/10Y/DXY) — used in RECAP only, ground prices in current reality
3. **Live news** (Finnhub market news, last 48h) — used in RECAP only, catches weekend/overnight events
4. **Finnhub earnings + economic calendars** — verification only, not a content source. Hard-filtered at the data layer: only MAG7/top banks/bellwethers earnings + macro whitelist (CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI/FOMC/Powell + ECB/BOJ/BOE rate decisions)
5. **Previous pulse markdown** (scheduled only) — diff baseline, not template

Per-PDF JSON passed to synthesis includes: source, title, type, priority, published date, key_insights, market_movers (with conviction), sector_views, earnings_insights, macro_indicators, crypto_views, vol_and_positioning, trade_ideas (with time_horizon), risk_factors, cross_bank_references, entities_mentioned, charts_described.

## Module Guide

| Module | Purpose |
|---|---|
| `config.py` | All settings from env vars via pydantic-settings |
| `db.py` | SQLite schema + query helpers (WAL mode) |
| `dropbox_client/watcher.py` | Cursor-based Dropbox polling + download |
| `pdf_processing/extractor.py` | PyMuPDF text extraction (image rendering exists but unused) |
| `pdf_processing/page_selector.py` | Multi-signal page scoring (exists but unused — multimodal removed) |
| `ai_analysis/prompts.py` | Gemini prompt templates (triage, deep analysis, synthesis) |
| `ai_analysis/analyzer.py` | Gemini orchestrator (triage + deep analysis, text-only) |
| `ai_analysis/rate_limiter.py` | Concurrency + RPM management |
| `ai_analysis/models.py` | Dataclasses: TriageResult, PdfAnalysis, MarketMover, SectorView, MacroIndicator, TradeIdea, EntityMention |
| `report/synthesizer.py` | Cross-PDF synthesis via Gemini; builds ticker map; handles prev_pulse context |
| `report/market_data.py` | CoinGecko + Yahoo Finance live price snapshot |
| `report/news_data.py` | Finnhub news + earnings calendar + economic calendar (all hard-filtered) |
| `report/formatter.py` | Discord embed formatting with color-coded sections + dynamic footer |
| `discord_bot/bot.py` | Discord bot with /pulse, /status, /load, /reanalyze, /clearqueue, /seedcursor, /reprocess |
| `discord_bot/sender.py` | Embed delivery (per-embed = separate message — batching was reverted) |
| `pipeline/orchestrator.py` | End-to-end pipeline coordination |
| `scheduler/jobs.py` | APScheduler cron jobs (15-min poll, 5-min process, 9 AM ET pulse) |
| `test_pulse.py` | CLI tool for manual testing |
| `inspect_db.py` | CLI for browsing DB state |
| `main.py` | Entry point |

## Discord Commands

Password gate: `COMMAND_PASSWORD=jamalbot` env var. Gated commands take `password` arg.

**Gated:**
- `/load hours:N password:jamalbot` — ingest Dropbox PDFs from last N hours (max 48). Shows live progress with current filename + last 5 completed.
- `/reanalyze hours:N password:jamalbot` — re-analyze PDFs already in DB with current prompt (appends new pdf_analyses rows; old preserved).
- `/clearqueue password:jamalbot [confirm:true]` — delete pending (DOWNLOADED/PROCESSING) rows + local files. Refuses >500 without `confirm:true`.
- `/seedcursor password:jamalbot` — set Dropbox cursor to "now" so next poll skips backfill.

**Open:**
- `/pulse [hours:N]` — manual pulse synthesis. Default 24h, max 168h. Fully standalone (no prev-pulse diff).
- `/status` — dashboard: today's ingestion + total DB state + priority mix (always shows high/medium/low even if 0) + upload range + all-time tokens + last pulse times + Dropbox cursor state + upload volume (24h + since last scheduled) + last 5 ingested filenames (in configured timezone).
- `/reprocess filename:X` — retry a failed PDF.

## Deployment

Railway project **`marvelous-dream`**, service **`worker`**, environment **`production`**. Volume mounted at **`/data`** (SQLite DB + temp PDFs). Every `git push` to the working branch auto-redeploys.

### Accessing production state

**If you (Claude) have shell access** (Claude Code desktop with terminal):
```bash
railway logs --deployment | tail -50                        # recent logs
railway logs --deployment 2>&1 | grep -iE "ERROR|failed"     # filter for issues
railway variables --service worker                            # list env vars (human-readable)
railway variables --service worker --kv                       # list env vars (KEY=value)
railway variable set --service worker "KEY=value"             # set env var (triggers redeploy)
railway ssh "python -c 'import sqlite3; ...'"                 # query prod DB directly
```

Railway CLI is authenticated via `railway login` (already cached on the user's machine). The project is already linked from this repo's directory.

**If you (Claude) don't have shell access** (mobile app, web UI, or any env without terminal):

You cannot run `railway` commands yourself. Ask the user to run them locally and paste output. Structured request pattern that works well:

> "I need to check production state. Could you run this in your terminal and paste the output?"
> ```
> cd c:/Users/gabje/Institutional-report-bot
> railway logs --deployment | tail -100
> ```

Common requests worth pre-writing for the user:

| What you need | Command for user to run |
|---|---|
| Recent logs | `railway logs --deployment \| tail -100` |
| Error patterns | `railway logs --deployment 2>&1 \| grep -iE "ERROR\|failed\|Traceback"` |
| List env vars | `railway variables --service worker` |
| Query prod DB | `railway ssh 'python -c "import sqlite3; ..."'` (write the Python carefully — no complex quoting) |
| Deploy status | Ask user to check Railway dashboard (deploy tab) |

Alternative for log inspection: user can run `/status` in Discord which surfaces most health signals without needing terminal access. That's usually faster for quick health checks than pulling raw logs.

**DB schema + contents** without shell access: read `db.py` for schema. For data inspection, ask user to run `inspect_db.py` locally (has pre-built CLI views for recent PDFs, analyses, reports, logs).

## Environment Variables (on Railway)

Key ones set on `worker` service:
- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` — Dropbox OAuth2
- `DROPBOX_FOLDER_PATH=/Current`
- `GOOGLE_API_KEY`, `GEMINI_MODEL=gemini-3.1-flash-lite-preview`, `GEMINI_TRIAGE_MODEL=gemini-3.1-flash-lite-preview`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` (comma-separated list of channel IDs)
- `FINNHUB_APi_KEY` (note lowercase 'i' typo — pydantic-settings is case-insensitive so it works; don't fix cosmetically without reason)
- `COMMAND_PASSWORD=jamalbot`
- `TIMEZONE=America/New_York`
- `DAILY_PULSE_HOUR=9`, `DAILY_PULSE_MINUTE=0`
- `DB_PATH=/data/reports.db`, `PDF_DOWNLOAD_DIR=/data/pdfs` (MUST use leading slash — relative paths write to ephemeral container storage and get wiped on redeploy)

## Database

SQLite with WAL mode at `/data/reports.db` (persists on Railway volume).

Tables:
- `dropbox_state` — cursor for delta polling
- `pdf_files` — status tracking (DOWNLOADED → PROCESSING → PROCESSED / FAILED)
- `pdf_analyses` — **append-only** per-PDF structured JSON results + token usage. UNIQUE constraint was dropped so reanalyses create new rows, old ones preserved as history. Queries use `MAX(id) GROUP BY pdf_file_id` CTE to get the latest analysis per PDF.
- `daily_reports` — **append-only** synthesized pulses. UNIQUE(report_date, report_type) also dropped. `report_type='daily'` for scheduled, `'manual'` for manual.
- `processing_log` — audit trail

Migration on boot: `_migrate_drop_unique_constraints` rebuilds tables without UNIQUEs if the old schema is detected.

## Dropbox Structure (Live)

Root: `/Current`
```
/Current/2026/April/Apr 15/
  Goldman/     # biggest volume, ~50-80 PDFs/day
    S&T/       # Sales & Trading — chart-of-day, flow notes
  JPM/
  Citi/
  BofA/        # Hartnett Flow Show, Morning Tidbits, Economic Weekly
  UBS/
  RBC/
  Barclays/
  Deutsche Bank/
  TME/         # The Market Ear — short vol/positioning pieces
  ANZ/, ING/, Mizuho/, MUFG/, Rabobank/, TS Lombard/, Other/
```

/Current volume: ~100-200 PDFs/day across all sources.

## Current Geopolitical Context (April 2026)

- Middle East / Iran / Strait of Hormuz blockade driving oil markets
- Ceasefire negotiations ongoing (deadlines referenced in research vary)
- Oil prices split: physical Brent markets pricing scarcity premium, futures pricing resolution
- CTA systematic squeeze dynamics — hedge fund positioning oscillating, re-risking in tech

## Cost Monitoring

Based on actual token usage (Gemini Flash Lite at ~$0.10/M input, $0.40/M output):
- Per pulse synthesis: ~$0.02
- Per day ingestion (~150-200 PDFs): ~$0.30-0.50
- Monthly total: ~$15-20 (Gemini + Railway $5)
- Spend cap at ai.studio/spend — user hit $10 cap once, raised; keep in mind if 429 errors return.

## Recent Session Context (2026-04-14 → 2026-04-16)

Major improvements made in the recent iteration sequence:

1. **Removed multimodal entirely** — text-only deep analysis with full document
2. **Timestamp format bug fix** — was inflating "since last pulse" counts
3. **Diff-framing for scheduled pulse** — skip themes unchanged vs yesterday
4. **Manual pulse fully standalone** — no prev-pulse context, last-24h default
5. **Hard-filtered Finnhub calendars** — only Tier 1 events reach Gemini
6. **Elevated single-topic dedicated notes** — don't let catalyst notes get drowned by broad macro themes
7. **Plain-English translation table** — 20+ jargon terms with plain equivalents
8. **Structured entity extraction** for reliable cashtag formatting
9. **Previous pulse freshness check** — skip diff if prior scheduled pulse >48h old
10. **LOW-source hardcode fix** — was tagging LOW PDFs as "Unknown"
11. **Expanded LOW criteria** — peripheral EM macro, minor FX, niche commodities now correctly LOW
12. **Multi-channel DISCORD_CHANNEL_ID** — comma-separated list support
13. **/status enrichments** — upload volume windows, last-5-ingested, priority shows 0 for empty buckets

## Next Steps / TODO

No unchecked infrastructure items. Deployed, stable, functioning.

Potential future work (not prioritized):
- Q&A RAG agent over pdf_analyses (SQL + FTS5, no embeddings needed given structured data)
- Dropbox DB backup job
- Log retention pruning (processing_log grows fastest)
- Ticker-specific news filtering for deeper RECAP
