"""Reminder calendar — pure load / validate / due-date logic.

No Discord, no DB, no scheduler here — just file parsing and date math
so the whole thing is unit-testable. The scheduled job (reminders/job.py)
calls these, dedups, renders embeds, and posts.

Calendar file: reminders/calendar.json — a JSON list of entries:
  {
    "id": "spacex-unlock-2026-06-25",   # stable slug (dedup + /reminders ref)
    "date": "2026-06-25",                # event date, YYYY-MM-DD
    "lead_days": [7, 0],                 # days-before offsets to fire; 0 = day-of
    "event": "SpaceX employee share unlock",
    "note": "First post-IPO lockup expiry."   # optional one-liner
  }

Claude is the editor (parses the user's natural language / screenshots
into entries + commits). The default lead when unspecified is [1] (day
before).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime

log = logging.getLogger(__name__)

# reminders/calendar.json sits next to this module.
CALENDAR_PATH = os.path.join(os.path.dirname(__file__), "calendar.json")

DEFAULT_LEAD_DAYS = [1]   # day-before, per the 2026-06-16 design decision
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _slugify(event: str, date_str: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (event or "").lower()).strip("-")
    return f"{base or 'event'}-{date_str}"


def _coerce_lead_days(raw) -> list[int]:
    """Non-negative ints, deduped + sorted ascending. Falls back to the
    default when the value is missing or yields nothing valid."""
    if not isinstance(raw, list):
        return list(DEFAULT_LEAD_DAYS)
    out: set[int] = set()
    for v in raw:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n >= 0:
            out.add(n)
    return sorted(out) if out else list(DEFAULT_LEAD_DAYS)


def _validate_entry(raw: dict) -> dict | None:
    """Normalize one raw entry to a clean dict, or None if unusable.

    Required: a parseable `date` (YYYY-MM-DD) and a non-empty `event`.
    Everything else is defaulted. Returns a NEW dict (doesn't mutate
    input) with keys: id, date, event, lead_days, note, _date (date obj).
    """
    if not isinstance(raw, dict):
        return None
    date_str = str(raw.get("date") or "").strip()
    event = str(raw.get("event") or "").strip()
    if not date_str or not event:
        return None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return None
    entry_id = str(raw.get("id") or "").strip() or _slugify(event, date_str)
    note = str(raw.get("note") or "").strip()
    return {
        "id": entry_id,
        "date": date_str,
        "event": event,
        "lead_days": _coerce_lead_days(raw.get("lead_days")),
        "note": note,
        "_date": d,
    }


def load_calendar(path: str | None = None) -> list[dict]:
    """Load + validate the calendar. Returns the list of valid entries.

    Missing file → []. Malformed file (not a JSON list) → [] + warning.
    Individual malformed entries are skipped + logged; the rest load.
    Never raises — the scheduler must not crash on a bad calendar.
    """
    path = path or CALENDAR_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"reminder calendar: could not read {path}: {e}")
        return []
    if not isinstance(data, list):
        log.warning(f"reminder calendar: {path} is not a JSON list — ignoring")
        return []
    out: list[dict] = []
    for raw in data:
        entry = _validate_entry(raw)
        if entry is None:
            log.warning(f"reminder calendar: skipping malformed entry: {raw!r}")
            continue
        out.append(entry)
    return out


def due_reminders(entries: list[dict], today: date) -> list[tuple[dict, int]]:
    """(entry, lead) pairs whose fire date == today.

    An entry fires for lead L on (event_date - L days). So lead_days
    [7,1,0] fires 7 days out, the day before, and the day of.
    """
    due: list[tuple[dict, int]] = []
    for e in entries:
        d = e.get("_date")
        if not isinstance(d, date):
            continue
        for lead in e.get("lead_days", []):
            if (d - today).days == lead:
                due.append((e, lead))
    return due


def upcoming(entries: list[dict], today: date) -> list[dict]:
    """Entries on/after today, sorted by date — for the /reminders view."""
    fut = [e for e in entries if isinstance(e.get("_date"), date)
           and e["_date"] >= today]
    return sorted(fut, key=lambda e: e["_date"])


def _fmt_date(d: date) -> str:
    """'Jun 25' — no leading zero, Windows-safe (avoids %-d)."""
    return f"{_MONTHS[d.month - 1]} {d.day}"


def lead_phrase(lead: int) -> str:
    """Human lead wording for the reminder title."""
    if lead <= 0:
        return "Today"
    if lead == 1:
        return "Tomorrow"
    return f"In {lead} days"


def reminder_title(entry: dict, lead: int) -> str:
    """e.g. '📅 Tomorrow — SpaceX employee share unlock (Jun 25)'."""
    d = entry.get("_date")
    date_part = f" ({_fmt_date(d)})" if isinstance(d, date) else ""
    return f"📅 {lead_phrase(lead)} — {entry['event']}{date_part}"
