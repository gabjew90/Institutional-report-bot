# CLAUDE.md

## Project Overview

Institutional Research PDF Analyzer + Discord Market Pulse Bot. Processes 50-100 institutional financial research PDFs daily from Dropbox and delivers synthesized trading intelligence via Discord.

**Target audience**: Options and crypto traders who need actionable intelligence from institutional research (Goldman Sachs, JPMorgan, Morgan Stanley, etc.)

## Architecture

```
Dropbox (PDFs) → poll every 15 min (cursor-based)
  → dropbox_client/watcher.py: download new PDFs
  → pdf_processing/extractor.py: PyMuPDF text extraction
  → pdf_processing/page_selector.py: smart page scoring (pick top 8 pages)
  → ai_analysis/analyzer.py: Gemini 3.1 Lite tiered analysis
  → report/synthesizer.py: cross-PDF synthesis into Market Pulse
  → report/formatter.py → discord_bot/sender.py: Discord embeds
  → Delivered at 9:00 AM PST / 12:00 PM ET daily
```

## AI Model

Uses **Google Gemini 3.1 Lite** (`google-genai` SDK) for all AI analysis. NOT Anthropic/Claude.

## Key Design Decisions

### Tiered Analysis (token efficiency)
- **Tier 1 - Triage**: Text-only Gemini call classifies each PDF as high/medium/low priority
- **Tier 2 - Deep Analysis**: HIGH priority → multimodal (text + page images). MEDIUM → text-only. LOW → skip, use triage summary
- **Tier 3 - Synthesis**: One Gemini call merges all per-PDF analyses into the Market Pulse

### Smart Page Selection (`pdf_processing/page_selector.py`)
Scores every page across 5 signals to pick the ~8 most valuable:
- Keyword density (0.25): financial terms — upgrades, price targets, CPI, BTC
- Structural importance (0.25): page 1, "Key Takeaways" headers boosted, disclaimers excluded
- Visual content (0.30): charts/tables/images ranked highest (multimodal value)
- Information density (0.10): numbers, data richness
- Novelty (0.10): penalizes repetitive content

### Burst Handling
- PDFs arrive unpredictably (could be 50 at once)
- Processing queue with `asyncio.Semaphore(5)` for concurrency control
- Daily report has hard 12:00 PM ET deadline — generates with whatever is ready, notes gaps

### Daily Market Pulse (9am PST / 12pm ET)
Single daily report. Sections: What Happened, What to Watch Today, What Smart Money Is Doing, Crypto, Coming Up. Written in plain English (no Wall Street jargon). Under 1000 words. Primary sources: Goldman Sachs, Citi, Bank of America.

## Module Guide

| Module | Purpose |
|--------|---------|
| `config.py` | All settings from env vars via pydantic-settings |
| `db.py` | SQLite schema + query helpers (WAL mode) |
| `dropbox_client/watcher.py` | Cursor-based Dropbox polling + download |
| `pdf_processing/extractor.py` | PyMuPDF text extraction + page image rendering |
| `pdf_processing/page_selector.py` | Multi-signal page scoring algorithm |
| `ai_analysis/prompts.py` | All Gemini prompt templates (triage, analysis, synthesis) |
| `ai_analysis/analyzer.py` | Gemini API orchestrator (triage + deep analysis) |
| `ai_analysis/rate_limiter.py` | Concurrency + RPM management |
| `report/synthesizer.py` | Cross-PDF synthesis via Gemini |
| `report/formatter.py` | Discord embed formatting with color-coded sections |
| `discord_bot/bot.py` | Discord bot with /pulse, /status, /reprocess commands |
| `discord_bot/sender.py` | Chunked embed delivery |
| `pipeline/orchestrator.py` | End-to-end pipeline coordination |
| `scheduler/jobs.py` | APScheduler cron jobs |
| `test_pulse.py` | CLI tool for manual testing |
| `main.py` | Entry point |

## Dropbox Structure (Live)

Root folder: `/Current` (not `/InstitutionalResearch`)

```
/Current/2026/April/Apr 10/
  Goldman/           # ~80+ PDFs/day — equity research, macro, sector
    S&T/             # Sales & Trading notes (Morning Mail, Chart of Day, Digital Assets)
  JPM/               # First to Market, equity research, FX, credit
  Citi/              # The Point (Europe/Global), equity research
  BofA/              # Hartnett Flow Show, Economic Weekly, sector notes
  UBS/               # Contextual Diary, sector research (large PDFs, 8-90 pages)
  RBC/               # Research at a Glance (compact summary format)
  Barclays/          # Morning Research Summary, Before the Bell
  Deutsche Bank/     # Early Morning Reid, FX Blog, sector previews
  TME/               # The Market Ear — short punchy vol/positioning commentary (2-3 pages)
  Bernstein/         # Thematic research (e.g., Bitcoin & Quantum computing)
  Mizuho/            # Japan macro, JGB strategy
  MUFG/              # FX, inflation, Asia
  ANZ/               # Australia/NZ/Asia macro
  ING/               # ING Think pieces — rates, FX, commodities, EM
  Rabobank/          # Global daily
  TS Lombard/        # EM guides
  Other/             # Misc: ABN AMRO, SEB, BNZ, SYZ, etc.
/Archives/2026/      # Organized by month/date — same structure as Current
/Archives/Hedge Fund Letters/  # Historical quarterly letters (2021-2024)
```

### Volume: ~100-150 PDFs per day across all sources
### Naming patterns:
- Goldman: descriptive titles directly (e.g., `US Morning Call_ April 10.pdf`)
- BofA: `BofA_` prefix with `_YYYYMMDD` suffix
- JPM: `JPM_` prefix
- Deutsche Bank: `DB-` prefix
- ING: `ING-Think-` prefix with kebab-case
- TME: snake_case lowercase (e.g., `vol_reset_risk_didn_t.pdf`)
- UBS: alphanumeric codes or descriptive titles

### Key report types found:
1. **Morning briefings** (HIGH priority): GS Morning Call, JPM First to Market, Citi The Point, BofA Morning Tidbits, Barclays Before the Bell
2. **S&T color** (HIGH): GS Sales Trading (Good Morning Mail, Chart of Day, Digital Assets, TMT Spec Sales)
3. **Vol/macro commentary** (HIGH): TME (The Market Ear) — positioning, squeeze alerts, vol surface, hedging ideas
4. **Single-stock research** (MEDIUM-HIGH): Upgrades/downgrades/catalyst watches with DCF/SOTP valuation
5. **Macro/economics** (MEDIUM-HIGH): BofA Economic Weekly, GS Economics Analyst, inflation/CPI/Fed notes
6. **Sector overviews** (MEDIUM): GS/UBS Weekly Kickstart, sector earnings previews
7. **FX/commodities** (MEDIUM): MUFG FX Daily, GS Oil Tracker, commodity strategy
8. **Regional** (LOW-MEDIUM): Individual country macro (Argentina, Egypt, Poland, etc.)
9. **UBS Contextual Diary** (MEDIUM): Large 60-90 page event previews with crowding scores

### Current geopolitical context (as of April 2026):
- Middle East conflict / Iran situation driving oil prices and market volatility
- Ceasefire negotiations in progress
- Strait of Hormuz transit risk affecting energy markets
- European markets particularly oil-sensitive

## Environment Variables

See `.env.example` for the full list. Key ones:
- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` — Dropbox OAuth2
- `GOOGLE_API_KEY` — Gemini API key
- `GEMINI_MODEL` — defaults to `gemini-3.1-lite`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` — Discord bot config
- `TIMEZONE` — defaults to `America/New_York`

## Testing

```bash
# Quick test: process 3 PDFs from Dropbox, print report to terminal
python test_pulse.py --limit 3 --dry-run

# Full test with Discord delivery
python test_pulse.py --limit 5 --send

# Test specific Dropbox subfolder
python test_pulse.py --folder "/Research/Macro" --dry-run
```

## Deployment

Railway (always-on worker). See `Procfile`, `railway.toml`, `nixpacks.toml`.
Needs `poppler-utils` system package for PDF image rendering.

## Database

SQLite with WAL mode. Tables:
- `dropbox_state` — cursor for delta polling
- `pdf_files` — status tracking (DOWNLOADED → PROCESSING → PROCESSED / FAILED)
- `pdf_analyses` — per-PDF structured JSON results + token usage
- `daily_reports` — synthesized Market Pulse reports
- `processing_log` — audit trail

## Next Steps / TODO

- [x] Connect to live Dropbox and examine actual PDF content to tune prompts
- [x] Fine-tune page_selector.py keywords based on real report formats
- [x] Adjust triage prompt based on actual report types received
- [x] Generate Dropbox refresh token for production use
- [ ] Set up Discord bot (create at discord.com/developers, enable Message Content intent)
- [ ] Get Google Gemini API key and test AI analysis pipeline with real PDFs
- [ ] Test end-to-end pipeline with real PDFs (use test_pulse.py --limit 5 --dry-run)
- [ ] Tune Dropbox watcher to handle `/Current/2026/{Month}/{MonthAbbr} {Day}/` folder structure
- [ ] Deploy to Railway
