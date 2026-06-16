# Channel Reminder System — Design

**Date:** 2026-06-16
**Goal:** A calendar of dated events (SpaceX unlocks, earnings, macro prints, etc.) that the bot posts to the stonks-yapping channel on its own, on configurable lead times — the same hands-off, scheduled posting the bot already does for the daily pulse.

**Editor / runtime split:** the user (via a Claude Code session) is the calendar EDITOR — they hand events to Claude in natural language or by screenshot; Claude parses them into the calendar file and commits. The bot is the ALARM CLOCK — once an event is in the file, the bot reminds the channel autonomously with no session needed.

---

## Architecture

Three small units, each independently testable:

1. **Calendar store** — `reminders/calendar.json`, a checked-in JSON list. Claude edits it; a commit auto-redeploys and the new event is live. Version-controlled, dependency-free (stdlib `json`), no manual prod DB writes.
2. **Reminder logic** — `reminders/calendar.py`: pure functions that load+validate the calendar and compute which reminders are due for a given date. No I/O beyond reading the file; fully unit-testable.
3. **Scheduled job + delivery** — a daily APScheduler cron (3:45 PM ET) that asks the logic what's due today, dedups against a `reminder_sent` table, renders an embed, and posts via `discord_bot.sender.send_embeds` to the reminder channel.

Plus a read-only `/reminders` slash command that renders the upcoming calendar in Discord.

## Data model

`reminders/calendar.json`:
```json
[
  {
    "id": "spacex-unlock-2026-06-25",
    "date": "2026-06-25",
    "event": "SpaceX employee share unlock",
    "lead_days": [7, 0],
    "note": "First post-IPO lockup expiry; watch for supply pressure."
  }
]
```
- `id` (string): stable slug, used for dedup + `/reminders remove` reference. Claude generates it (`<kebab-event>-<date>`).
- `date` (YYYY-MM-DD): the event date.
- `lead_days` (list[int]): days-before offsets to fire. `[7,0]` = 7 days before + day-of. **Default `[1]`** (day before) when the user doesn't specify. `0` = day-of.
- `note` (string, optional): one-line context included in the reminder body.

`reminder_sent` DB table (dedup):
```
fire_date TEXT,   -- YYYY-MM-DD the reminder posted
event_id  TEXT,   -- calendar entry id
lead      INTEGER,
UNIQUE(fire_date, event_id, lead)
```

## Data flow

1. **Add/edit:** user tells Claude → Claude writes/updates `calendar.json` → commit + push → redeploy (~90s).
2. **Daily fire (3:45 PM ET cron):**
   - Load + validate calendar (skip malformed entries, log them).
   - `today = now(ET).date()`. For each event, for each `lead` in `lead_days`: if `event_date − lead == today`, it's due.
   - For each due (event, lead): skip if `(today, id, lead)` already in `reminder_sent` (dedup). Otherwise render embed, post to channel, insert the `reminder_sent` row on success.
3. **`/reminders`:** loads the calendar, lists upcoming events (date ≥ today) sorted by date with their lead_days, as a normal (non-ephemeral) channel reply so the whole room can see the schedule. Read-only; no password gate (non-mutating), but channel-allowlisted like other pulse/admin commands.

## Reminder embed

Lead-aware wording, one embed per due event:
- 7+ days: `📅 In {n} days — **{event}** ({Mon DD})`
- 1 day: `📅 Tomorrow — **{event}** ({Mon DD})`
- 0 days: `📅 Today — **{event}** ({Mon DD})`
- Body: the `note` if present. Distinct color (amber `0xF1C40F`).

## Config

- New env var `REMINDER_CHANNEL_ID = 1317587853282119745` (#💬-stonks-yapping-💬) on Railway.
- Post time `3:45 PM ET` (hardcoded `CronTrigger(hour=15, minute=45, timezone=tz)`; ET handles EST/EDT). No new config var — it's a fixed product decision.

## Error handling

- Missing or malformed `calendar.json` → log a warning, post nothing that day (never raise into the scheduler).
- A single malformed entry → skip just that entry, process the rest.
- Channel id unset or unresolvable → log, no-op.
- Send failure → log, do NOT write the `reminder_sent` row (so it retries next day's fire if still due — though for a one-shot lead that window has passed; acceptable, logged).
- Past events stay in the file harmlessly (never match `today`); Claude prunes them when editing.

## Testing (smoke, no live Discord)

- Due-date math: an event with `lead_days=[7,1,0]` fires on exactly those three dates and no others; `0` = day-of; default `[1]` applied when omitted.
- Validation: malformed entry skipped, rest processed; missing file → empty due-list, no raise.
- Dedup: second run on the same fire_date for the same (id, lead) does not re-post.
- Embed wording: in-N-days / tomorrow / today branches.
- `/reminders` rendering: upcoming-only, sorted, shows lead_days.

## Out of scope (YAGNI)

- No slash command to ADD/EDIT events (user opted for natural-language-via-Claude).
- No recurring events (every entry is a single dated event).
- No per-user reminders or DMs (channel-only).
- No timezone per event (all ET).
