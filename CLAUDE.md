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
  → Delivered at 8:30 AM ET (morning) and 3:00 PM ET (afternoon)
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
- Morning report has hard 8:30 AM deadline — generates with whatever is ready, notes gaps
- Afternoon report catches up on anything missed

### Morning vs Afternoon Market Pulse
- **Morning** (8:30 AM ET): Strategic session expectations — what to watch at open, institutional consensus, positioning ideas
- **Afternoon** (3:00 PM ET): Closing playbook — what to enter before close, overnight positioning, what the session revealed

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

- [ ] Connect to live Dropbox and examine actual PDF content to tune prompts
- [ ] Fine-tune page_selector.py keywords based on real report formats
- [ ] Adjust triage prompt based on actual report types received
- [ ] Set up Discord bot (create at discord.com/developers, enable Message Content intent)
- [ ] Generate Dropbox refresh token for production use
- [ ] Test end-to-end pipeline with real PDFs
- [ ] Deploy to Railway
