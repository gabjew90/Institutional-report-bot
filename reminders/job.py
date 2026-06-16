"""Reminder scheduled job + Discord delivery.

Registered as a daily 3:45 PM ET cron (scheduler/jobs.py). Asks
reminders.calendar what's due today, dedups against the reminder_sent
table, renders one embed per due reminder, and posts to the configured
reminder channel. Never raises into the scheduler — a bad calendar or a
send failure logs and is skipped.
"""

from __future__ import annotations

import logging
from datetime import datetime

import discord
import pytz

import db
from config import settings
from reminders import calendar as cal

log = logging.getLogger(__name__)

_REMINDER_COLOR = 0xF1C40F  # amber — distinct from pulse sections


def _build_embed(entry: dict, lead: int) -> discord.Embed:
    embed = discord.Embed(
        title=cal.reminder_title(entry, lead),
        description=entry.get("note") or None,
        color=_REMINDER_COLOR,
    )
    return embed


async def reminder_check_job(bot) -> None:
    """Post any reminders due today to the reminder channel.

    Idempotent within a day via the reminder_sent dedup table.
    """
    raw_cid = (settings.reminder_channel_id or "").strip()
    if not raw_cid:
        return  # feature disabled — no channel configured
    try:
        cid = int(raw_cid)
    except ValueError:
        log.warning(f"reminder job: REMINDER_CHANNEL_ID not an int: {raw_cid!r}")
        return

    tz = pytz.timezone(settings.timezone)
    today = datetime.now(tz).date()
    today_iso = today.isoformat()

    entries = cal.load_calendar()
    due = cal.due_reminders(entries, today)
    if not due:
        return

    channel = bot.get_channel(cid)
    if channel is None:
        log.warning(f"reminder job: channel {cid} not in bot cache — skipping")
        return

    from discord_bot.sender import send_embeds

    posted = 0
    for entry, lead in due:
        eid = entry.get("id") or ""
        if db.reminder_already_sent(today_iso, eid, lead):
            continue
        try:
            ok = await send_embeds(channel, [_build_embed(entry, lead)])
        except Exception as e:
            log.warning(f"reminder job: send failed for {eid} lead={lead}: {e}")
            continue
        if ok:
            # Only mark sent on a confirmed post, so a failed send retries
            # on the next fire if the lead window hasn't passed.
            db.mark_reminder_sent(today_iso, eid, lead)
            posted += 1
        else:
            log.warning(
                f"reminder job: send_embeds returned False for {eid} "
                f"lead={lead} — not marking sent"
            )

    if posted:
        log.info(f"reminder job: posted {posted} reminder(s) for {today_iso}")
