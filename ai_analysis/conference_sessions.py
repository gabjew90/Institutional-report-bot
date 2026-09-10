"""Conference sessions extracted from the corpus, verified against the
text they came from.

Spec: docs/superpowers/specs/2026-09-09-conference-sessions-from-corpus.md.
The host bank's morning note already prints the day's schedule ("Day 2
Schedule - Wednesday, September 9th (all times in PT): 12:30 PM: MSFT").
The deep-analysis schema gains one list, and this module is the whole
foolproofing: a session survives only when

1. its `anchor` is a normalized substring of the extracted text
   (ai_analysis.anchor_check.normalize, the same matcher the pulse's
   figure check uses), ENFORCING, not warn-only;
2. the month and day of `date_iso` are PRINTED in the text. "Day 2" is
   a label, not a date. The year is the reference date's year, rolled
   forward when the resolved date is more than 30 days in the past;
3. it names at least one US-listed ticker.

`tz` and `time_local` are stored exactly as printed; the calendar
converts to ET only for the zones it knows and renders day-level for
the rest. Nothing here reads pixels: an image-form schedule extracts
as a blank and yields nothing.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ai_analysis.anchor_check import MIN_ANCHOR_CHARS, normalize

log = logging.getLogger(__name__)


@dataclass
class ConferenceSession:
    conference: str              # as the document names it
    date_iso: str                # resolved per the module docstring, never inferred
    time_local: str = ""         # "12:30 PM" exactly as printed, "" when absent
    tz: str = ""                 # "PT" / "ET" / "" exactly as printed
    tickers: list[str] = field(default_factory=list)   # US-listed, uppercase, no $
    anchor: str = ""             # the verbatim schedule line, machine-verified


_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"]
_TICKER_RE = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\s*$")

# Zones the calendar converts; anything else renders day-level.
_ZONES = {
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver", "MDT": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago", "CDT": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
}
_ET = ZoneInfo("America/New_York")


def _printed_date_re(month: int, day: int) -> re.Pattern:
    name = _MONTHS[month - 1]
    abbr = name[:3]
    return re.compile(
        rf"(?:\b(?:{name}|{abbr})\.?\s+{day}(?:st|nd|rd|th)?\b"
        rf"|\b{day}(?:st|nd|rd|th)?\s+(?:{name}|{abbr})\b"
        rf"|\b{month}/{day}(?:/\d{{2,4}})?\b)",
        re.I)


def resolve_date(date_iso: str, text_lower: str, reference: date) -> str | None:
    """The printed-date rule. Returns the resolved ISO date or None."""
    try:
        m, d = int(str(date_iso)[5:7]), int(str(date_iso)[8:10])
        date(2000, m, d)
    except Exception:
        return None
    if not _printed_date_re(m, d).search(text_lower):
        return None
    try:
        resolved = date(reference.year, m, d)
    except ValueError:
        return None
    if (reference - resolved).days > 30:
        resolved = date(reference.year + 1, m, d)
    return resolved.isoformat()


def clean_tickers(raw) -> list[str]:
    out: list[str] = []
    for t in raw or []:
        s = str(t).strip().upper().lstrip("$")
        if _TICKER_RE.match(s) and s not in out:
            out.append(s)
    return out


def local_to_et_hhmm(time_local: str, tz: str, date_iso: str) -> str | None:
    """'12:30 PM' + 'PT' on 2026-09-09 -> '15:30' (24-hour, the form the
    econ rows use). None when the time or zone is not recognised."""
    m = _TIME_RE.match(time_local or "")
    zone = _ZONES.get((tz or "").strip().upper())
    if not m or not zone:
        return None
    hh, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).upper()
    if not (1 <= hh <= 12 and 0 <= mm < 60):
        return None
    hh = hh % 12 + (12 if ap == "P" else 0)
    try:
        y, mo, d = (int(x) for x in date_iso.split("-"))
        src = datetime(y, mo, d, hh, mm, tzinfo=ZoneInfo(zone))
    except Exception:
        return None
    et = src.astimezone(_ET)
    return f"{et.hour}:{et.minute:02d}"


def resolve_sessions(raw: list, source_text: str, reference: date | None = None,
                     *, file_name: str = "") -> tuple[list[ConferenceSession], dict]:
    """Every session the model proposed, filtered to the ones the text
    supports. Returns (sessions, stats); never raises."""
    stats = {"proposed": 0, "kept": 0, "anchor_missed": 0, "anchor_short": 0,
             "no_date": 0, "no_ticker": 0, "malformed": 0}
    out: list[ConferenceSession] = []
    try:
        ref = reference or datetime.now(_ET).date()
        hay = normalize(source_text or "")
        text_lower = (source_text or "").lower()
        seen: set[tuple] = set()
        for item in raw or []:
            stats["proposed"] += 1
            if not isinstance(item, dict):
                stats["malformed"] += 1
                continue
            anchor = str(item.get("anchor") or "").strip()
            if len(anchor) < MIN_ANCHOR_CHARS:
                stats["anchor_short"] += 1
                continue
            if normalize(anchor) not in hay:
                stats["anchor_missed"] += 1
                continue
            date_iso = resolve_date(str(item.get("date_iso") or ""), text_lower, ref)
            if not date_iso:
                stats["no_date"] += 1
                continue
            tickers = clean_tickers(item.get("tickers"))
            if not tickers:
                stats["no_ticker"] += 1
                continue
            conference = " ".join(str(item.get("conference") or "").split())[:120]
            if not conference:
                stats["malformed"] += 1
                continue
            s = ConferenceSession(
                conference=conference,
                date_iso=date_iso,
                time_local=" ".join(str(item.get("time_local") or "").split())[:20],
                tz=str(item.get("tz") or "").strip().upper()[:6],
                tickers=tickers,
                anchor=anchor[:200],
            )
            key = (s.conference.lower(), s.date_iso, s.time_local, tuple(s.tickers))
            if key in seen:
                continue
            seen.add(key)
            out.append(s)
        stats["kept"] = len(out)
        dropped = stats["proposed"] - stats["kept"]
        if dropped:
            log.warning(
                f"conference sessions {file_name}: kept {stats['kept']}/{stats['proposed']} "
                f"(anchor missed {stats['anchor_missed']}, short {stats['anchor_short']}, "
                f"no printed date {stats['no_date']}, no US ticker {stats['no_ticker']}, "
                f"malformed {stats['malformed']})")
        elif out:
            log.info(f"conference sessions {file_name}: {len(out)} verified")
    except Exception as e:
        log.warning(f"conference sessions {file_name}: resolver failed: {e}")
        stats["error"] = f"{type(e).__name__}: {e}"
    return out, stats
