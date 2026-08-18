# Daily calendar graphic — design

**Date:** 2026-08-15
**Status:** approved in brainstorm, awaiting spec review
**Owner decision trail:** comprehensive coverage → pure calendar, no editorial
layer → top-N by market cap → direction A2 (gold accent, two columns) →
Omnibeta palette → post at 00:00 UTC Sun–Thu into the pulse channels →
Pillow renderer → "markets closed" card on holidays.

## 1. What it is

A branded PNG posted to Discord every evening, showing the next US trading
day's economic releases and the companies reporting earnings before the
open and after the close. Same visual family as the Omnibeta logo. Pure
reference sheet: no commentary, no framing, no trade calls. The pulse
remains the place for interpretation.

Reference the user supplied: a dark-ground sheet with an ECONOMIC EVENTS
table, a COMPANY EVENTS block, and BMO / AMC earnings tables, ~40 tickers.
This design keeps the economic and earnings tables and drops the COMPANY
EVENTS block, because its content ("semicap read-through to WFE spend") is
analyst framing no feed supplies and the owner chose pure calendar.

## 2. Schedule and delivery

| | |
|---|---|
| Fires | 00:00 UTC, Sunday through Thursday nights (cron `0 0 * * 1-5` UTC — Monday 00:00 UTC *is* Sunday night ET) |
| Covers | the calendar date at fire time (UTC), which is the next US session |
| Posts to | every channel in `DISCORD_CHANNEL_ID`, same as the pulse |
| Skips | never silently. See §6 for holidays |
| Runs on | the Railway worker, as an APScheduler cron job in `scheduler/jobs.py` alongside the existing jobs |

00:00 UTC is 20:00 ET (19:00 during standard time). The sheet lands the
evening before the day it covers, which is when someone plans the next
session.

## 3. Data

Two sources, both already wired in `report/news_data.py`. No new API keys.

**Earnings — Finnhub `/calendar/earnings`.** Verified working on this
account (2026-08-13: 206 rows for one day, with `hour` = bmo/amc/dmh and
`epsEstimate`). The existing `fetch_earnings_calendar()` filters to
`_MAJOR_TICKERS`; the graphic needs the UNFILTERED list, so a new
`fetch_earnings_calendar_all(date) -> list[dict]` returns every row for the
date with `symbol`, `hour`, `epsEstimate`, `revenueEstimate`. `dmh` (during
market hours) and blank-hour rows go into the AMC column, flagged, since
that is where a reader would look for them last.

**Economic events — ForexFactory fallback (`_fetch_ff_economic_events`).**
Finnhub's economic calendar 403s on this tier (paid since 2026-06-11); FF is
what the pulse already uses. Filter: `country == US`, `impact in
{high, medium}`. Existing rows carry `event`, `time` (UTC ISO), `impact`,
`estimate`, `prev`. Rendered in ET.

**Market-cap ranking — Finnhub `/stock/profile2` `marketCapitalization`.**
Needed to cut 206 → top 20 per session. Fetched per symbol and cached in a
new SQLite table `symbol_market_cap(symbol, market_cap_musd, name,
fetched_at)` with a 7-day TTL. Steady state is ~20-40 fresh lookups a
night, not 206; the first night is a full pass and stays under Finnhub's
60/min free limit with a 1.1s pacing sleep. A symbol with no profile (fresh
IPO, delisting) sorts to the bottom rather than being dropped.

**Company names** come from the same profile call (`name`), so the cache
holds `name` too. Truncated to fit the column with an ellipsis, never
wrapped.

**Holidays** — `world_context.is_us_market_holiday(date_iso)`, the same
calendar the pulse routine and the missing-pulse watchdog use.

## 4. Rendering

**Engine: Pillow (already installed, 12.3, FreeType on).** Chosen over
headless Chromium (300MB install, 150-250MB resident, against ~250MB
container headroom on a service that already fought RSS with
`MALLOC_ARENA_MAX`) and over SVG+resvg (adds a binary; deferred as the
migration path if the design starts changing often).

**Canvas.** 1080 x 1350 px (4:5), the tallest ratio Discord shows
un-cropped in a feed. Rendered at 2x internally and downsampled for crisp
type.

**Fonts.** The container has no system fonts (`fc-list` empty), so two
TTFs are committed to `assets/fonts/`:
- a geometric grotesque for headings, wordmark and body (Inter or Manrope,
  OFL-licensed)
- a monospace for tickers and times (JetBrains Mono, OFL)

The wordmark is set in the grotesque, uppercase, ~0.4em tracking, matching
the logo's own lettering.

**Palette** (sampled from the logo):

| token | hex | use |
|---|---|---|
| ground | `#22352C` | canvas |
| gold | `#E5A93F` | section heads, times, rules — the single accent |
| sage | `#A8CBA0` | header hairline gradient midpoint only |
| teal | `#6CC9BE` | header hairline gradient endpoint only |
| text | `#E8F0EA` | body |
| text-dim | text at 55% | company names |
| wordmark | `#FFFFFF` | |

Gold is the only working accent (A2). Sage and teal appear only in the
one-pixel gradient hairline under the wordmark, so the logo's gradient is
present without competing with the content.

**Layout, top to bottom, fixed:**

1. Logo mark, centered, ~96px tall, composited from
   `assets/brand/omnibeta-logo.png` (RGBA). If the file is missing, this
   row is omitted and a warning is logged; the sheet still renders.
2. `OMNIBETA` wordmark, centered.
3. Gradient hairline, full width, transparent → gold → sage → teal →
   transparent.
4. Day and date, e.g. `THURSDAY 8/13`, with `MARKET CALENDAR` beneath in
   small tracked caps.
5. `ECONOMIC` band, then events in TWO columns, each row `HH:MM  Event
   name`. Sorted by time; column-major fill.
6. `BEFORE OPEN` and `AFTER CLOSE` bands side by side, each column up to
   20 rows of `SYM  Company name`, sorted by market cap descending.
7. Footer: `ALL TIMES ET` in small dim caps.

Every text row is measured with `ImageDraw.textlength`; a company name
longer than its column is cut on a word boundary and gets `…`. Nothing
wraps and nothing overflows the canvas by construction — the row counts
are fixed at 20, and 20 rows at the chosen size fit with margin.

**Two-column always.** The layout is fixed-size; there is no responsive
collapse. (The brainstorm mockup carried a mobile breakpoint that hid the
second column on the owner's phone for three rounds. Never again.)

## 5. Code layout

Follows the existing module split. No new top-level directories beyond
`assets/`.

| file | responsibility |
|---|---|
| `report/calendar_data.py` (new) | `build_calendar_day(date) -> CalendarDay` — fetch, filter, rank, truncate. Pure data; no drawing. |
| `report/calendar_render.py` (new) | `render_calendar_png(day: CalendarDay) -> bytes` — Pillow drawing, palette, fonts, logo. Pure rendering; no I/O beyond reading assets. |
| `db.py` | `symbol_market_cap` table + `get_market_caps(symbols)` / `upsert_market_caps(rows)`. |
| `report/news_data.py` | `fetch_earnings_calendar_all(date)` (unfiltered) and `fetch_symbol_profiles(symbols)`; the existing filtered fetcher is untouched. |
| `scheduler/jobs.py` | `_daily_calendar_job(bot)`, cron `0 0 * * 1-5` UTC, registered unconditionally (unlike the pulse jobs it does not depend on the bridge). |
| `discord_bot/sender.py` | reuse `_send_with_retry`; one new `send_file_to_channels(bot, png_bytes, filename, caption)` for the attachment path. |
| `assets/brand/omnibeta-logo.png` | owner-supplied. `assets/brand/README.md` already documents the expected form. |
| `assets/fonts/*.ttf` | two OFL fonts, committed. |

`CalendarDay` is a small dataclass: `date`, `weekday_label`,
`is_holiday: str | bool`, `econ: list[EconRow]`, `bmo: list[EarnRow]`,
`amc: list[EarnRow]`, `dropped_bmo: int`, `dropped_amc: int`. The
`dropped_*` counts render as a tiny `+N more` under a column when the cut
fired, so the sheet is honest that it is a top-20, not the whole list.

## 6. Failure and edge behaviour

The graphic must never fail to post over a partial data problem, and must
never post something misleading over one either.

| condition | behaviour |
|---|---|
| Market holiday | Post a **closed card**: same header, then `MARKETS CLOSED` with the holiday name from `world_context`, and — if FF returns any US high-impact release that day (rare, e.g. a jobs report on a Good Friday closure) — the economic block still renders under it. No earnings columns. |
| Earnings fetch fails | Render with the earnings columns replaced by one line `EARNINGS: unavailable`. Post. Log at ERROR. |
| Econ fetch fails | Render without the economic block. Post. Log at ERROR. |
| Both fail | Do NOT post an empty sheet. Log at ERROR, record `calendar_watchdog` in `processing_log`, and post a one-line plain message: `Tonight's calendar could not be built (data feeds unavailable).` |
| Market-cap lookups partially fail | Symbols without a cap sort last, still shown. |
| Logo file missing | Render without the mark; wordmark only. Log WARNING once per boot. |
| Font file missing | Hard fail at import with a clear message — this is a deploy defect, not a runtime one, and must not be papered over. |
| Discord send fails | Existing `_send_with_retry` handles rate limits; a hard failure logs ERROR. No re-post on the next boot: idempotency via `processing_log` event `calendar_posted` keyed by date, checked before rendering. |
| Job misfires (worker down at 00:00) | `misfire_grace_time=3600` — posts up to an hour late, otherwise skips that night. |

## 7. Testing

Smoke scripts in `scripts/`, matching the repo's convention:

- `smoke_calendar_data.py` — feeds a fixture of 206 Finnhub rows plus a
  cap table and asserts: exactly 20 per session, sorted by cap desc,
  `dmh`/blank hour lands in AMC, missing-cap symbols sort last,
  `dropped_*` counts are right, holiday flag set from `world_context`.
- `smoke_calendar_render.py` — renders a fixture `CalendarDay` to bytes and
  asserts: PNG magic bytes, 1080x1350, non-trivial size (> 30KB), the
  logo-missing path renders, a 40-char company name is truncated with `…`
  and never exceeds its column width (measured), the closed-card path
  renders. Also renders one PNG to the scratchpad for eyeball review.
- `smoke_calendar_job.py` — with `send_file_to_channels` mocked, asserts:
  posts once per date (idempotency), both-feeds-down posts the plain
  fallback and not an image, holiday posts the closed card, cron is
  registered `mon-fri` at 00:00 UTC.

## 8. Out of scope (deliberately)

- Analyst framing / COMPANY EVENTS block — owner chose pure calendar.
- Weekend sheets — Friday and Saturday nights post nothing.
- Options-liquidity ranking — cap ranking chosen; revisit if microcaps
  the room trades keep getting cut.
- Web/dashboard publication of the image — Discord only for now. The PNG
  bytes are returned by a pure function, so publishing elsewhere later is
  one more consumer, not a redesign.
- Timeline / day-clock treatment (brainstorm direction C) — rejected as
  incompatible with 20-name sessions.
