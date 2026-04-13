# Market Pulse Bot — User Guide

Daily market briefing synthesized from institutional research PDFs + live market data + news + economic calendar. Runs on Railway, posts to Discord.

## Automatic behavior

- **9:00 AM PT / 12:00 PM ET daily** — scheduled Market Pulse posts to the configured channel
- **Every 15 min** — polls Dropbox for new research PDFs
- **Every 5 min** — processes any pending PDFs through triage + deep analysis

You don't need to do anything for the daily pulse. It just arrives.

## Slash commands

### `/pulse`
Generate a Market Pulse right now from analyses already in the DB (since the last scheduled pulse). Use this to preview what the next scheduled pulse will look like, or to get an ad-hoc briefing.

Takes ~15-30 seconds. Shows live progress. Does not affect the scheduled pulse cutoff.

### `/load <hours>`
Pull in recent PDFs from Dropbox and analyze them immediately. Max 48 hours.

Useful when:
- You want fresh data before running `/pulse`
- You want to catch up after a gap
- The auto-poll missed something

Shows live progress with current filename + last 5 completed. Skips anything already in the DB.

### `/status`
Pipeline health dashboard:
- PDFs ingested/processed today
- Total DB state + priority mix
- Upload date range in DB
- All-time tokens used
- Last scheduled + last manual pulse times
- Dropbox cursor state (should be ✅ seeded)

### `/seedcursor`
Sets the Dropbox cursor to "now" — next 15-min poll will skip backfill and only pick up new uploads. Use after a fresh deploy or if the cursor gets reset.

### `/clearqueue`
Deletes all PDFs in DOWNLOADED/PROCESSING status. Useful to abort a large backfill. Already-analyzed PDFs are safe.

## Pulse format

Each Pulse has three sections:

1. **RECAP** — how stocks and crypto have moved since the last pulse, and why
2. **INSIGHTS & ALPHA** — top institutional takes + smart money positioning
3. **WHAT TO WATCH** — market-moving events today + this week, with "HOW TO REACT" guidance per event

Prices come from live data (CoinGecko + Yahoo). Dates and BMO/AMC come from Finnhub's calendars. Commentary comes from the research.

## Data sources

- **Research**: Dropbox `/Current` folder — institutional PDFs from GS, JPM, Citi, BofA, UBS, Barclays, DB, TME, etc.
- **Live prices**: CoinGecko (BTC/ETH/SOL), Yahoo Finance (S&P, VIX, oil, gold, 10Y, DXY)
- **News**: Finnhub market news (last 48h)
- **Calendars**: Finnhub earnings + economic calendars (ground truth for dates/BMO-AMC/forecasts)

## Troubleshooting

- **"Application did not respond"** — bot was briefly mid-task. Wait a few seconds and retry.
- **`/pulse` returns "No analyses available"** — run `/load 24` first.
- **Cursor shows ❌ unset in `/status`** — run `/seedcursor` to prevent next poll from backfilling the full Dropbox folder.
- **Wrong prices in pulse** — CoinGecko or Yahoo may have rate-limited. Wait and retry.
- **Stale calendar dates** — verify `FINNHUB_API_KEY` is set in Railway env vars.
