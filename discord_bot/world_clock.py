"""Local time and session status of foreign exchanges, computed in code.

2026-10-26 audit A7/A8: asked whether Korea's market was open at 8:15 PM
ET (9:15 AM KST, 15 minutes into the session), the bot said "9:15 PM in
Seoul, markets are closed", and a later search retry replaced a correct
"KRX is live and open" with "closed". The prompt gives only ET and UTC, so
the model did the time-zone arithmetic itself, wrongly. This module does it,
and /ask injects the result whenever a question names a foreign market.

Regular hours only, weekdays; exchange holidays are not modelled, and the
note says so.
"""
from __future__ import annotations

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

# name, zone, sessions [(start, end, label)], words that name it
EXCHANGES = [
    ("Korea Exchange (KRX)", "Asia/Seoul",
     [(time(9, 0), time(15, 30), "regular session"), (time(16, 0), time(20, 0), "after-hours session")],
     r"korea|korean|kospi|kosdaq|krx|seoul|kst"),
    ("Tokyo Stock Exchange", "Asia/Tokyo",
     [(time(9, 0), time(11, 30), "morning session"), (time(12, 30), time(15, 30), "afternoon session")],
     r"japan|japanese|tokyo|nikkei|topix|tse|jst"),
    ("Hong Kong Exchange", "Asia/Hong_Kong",
     [(time(9, 30), time(12, 0), "morning session"), (time(13, 0), time(16, 0), "afternoon session")],
     r"hong\s*kong|hang\s*seng|hkex|hkt"),
    ("Shanghai/Shenzhen exchanges", "Asia/Shanghai",
     [(time(9, 30), time(11, 30), "morning session"), (time(13, 0), time(15, 0), "afternoon session")],
     r"china|chinese|shanghai|shenzhen|csi\s*300"),
    ("Taiwan Stock Exchange", "Asia/Taipei",
     [(time(9, 0), time(13, 30), "regular session")], r"taiwan|taiex|twse"),
    ("India NSE/BSE", "Asia/Kolkata",
     [(time(9, 15), time(15, 30), "regular session")], r"india|indian|nifty|sensex|nse|bse"),
    ("Australian Securities Exchange", "Australia/Sydney",
     [(time(10, 0), time(16, 0), "regular session")], r"australia|asx|sydney"),
    ("London Stock Exchange", "Europe/London",
     [(time(8, 0), time(16, 30), "regular session")], r"london|lse|ftse|\buk\s+market"),
    ("Frankfurt (Xetra)", "Europe/Berlin",
     [(time(9, 0), time(17, 30), "regular session")], r"germany|german|frankfurt|xetra|dax"),
]
_NAMES = [(name, zone, sessions, re.compile(r"\b(?:" + words + r")\b", re.I))
          for name, zone, sessions, words in EXCHANGES]


def _status(local: datetime, sessions) -> str:
    if local.weekday() >= 5:
        return "CLOSED (weekend)"
    t = local.time()
    for start, end, label in sessions:
        if start <= t < end:
            return f"OPEN, {label} ({start:%H:%M}-{end:%H:%M} local)"
    nxt = next((s for s in sessions if t < s[0]), None)
    if nxt:
        return f"CLOSED now; next: {nxt[2]} opens {nxt[0]:%H:%M} local today"
    return "CLOSED for the day"


_TIME_WORDS = re.compile(
    r"\b(?:time|o'?clock|open|opens|opened|close|closes|closed|hours|session|right now|live|"
    r"trading now|market(?:s)? (?:is|are) (?:open|closed))\b", re.I)


def clock_note(question: str, now: datetime | None = None) -> str:
    """The WORLD CLOCK block for the exchanges a question names, or ''.
    Only when the question is about time or market hours."""
    if not _TIME_WORDS.search(question or ""):
        return ""
    now = now or datetime.now(ZoneInfo("UTC"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    lines = []
    for name, zone, sessions, rx in _NAMES:
        if not rx.search(question or ""):
            continue
        local = now.astimezone(ZoneInfo(zone))
        lines.append(f"- {name}: local time {local:%I:%M %p %a %b} {local.day} "
                     f"({local.tzname()}). {_status(local, sessions)}.")
    if not lines:
        return ""
    et = now.astimezone(ZoneInfo("America/New_York"))
    return ("WORLD CLOCK, computed by the system just now (authoritative; do not convert time "
            f"zones yourself). New York: {et:%I:%M %p %a %b} {et.day} ET.\n" + "\n".join(lines)
            + "\nExchange holidays are not modelled; say so if it could matter.")
