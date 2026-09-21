"""The text that rides with the omni-calendar image on X.

This is the account's marketing post, so it is written to be read in a
second on a phone (owner, 2026-09-21): a heading that says which day
the sheet covers, one line per session, 12-hour ET times, and a small
icon per block so the eye finds the section it wants.

    📅 Tomorrow's market calendar · Tuesday 9/22

    🔔 Before the open: $JPM, WFC, ABT
    🌙 After the close: NFLX, UAL

    📊 Data (ET): 8:30 AM CPI, Core CPI · 2:00 PM FOMC Minutes
    🎤 Conferences: Morgan Stanley TMT (NVDA, AMD)

"TOMORROW" ONLY WHEN IT IS. The sheet posts at 3 PM ET for the next
trading day. On a Friday that is Monday, and before a holiday it skips
the closed day, so the heading says "Tomorrow's" only when the covered
date is the next calendar day and names the weekday otherwise
("Monday's market calendar · 9/28").

ONE CASHTAG, ON THE BIGGEST BOLD NAME; NO HASHTAGS (owner, 2026-09-21,
replacing an all-hashtags version that ran for one post). Cashtags go
on the names the sheet renders bold, but X refuses a self-serve API
post with more than one: the first live test came back HTTP 403,
"Posts are limited to a maximum of one cashtag ($SYMBOL)", with five in
the earnings line. So the single cashtag goes to the bold earnings name
with the largest market cap (a bold conference name when no earnings
row is bold), every other ticker is plain, and a day with nothing bold
carries none. `x_client.enforce_cashtag_limit` is the post-time
backstop.

Built greedily and trimmed from the least important end: unimportant
econ rows first, then tickers past the first few per session (the
sheet is cap-ranked, so the first names are the biggest), then the
conference line. Must fit X's 280 characters, which X counts with each
emoji as two.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

X_LIMIT = 280

ICON_TITLE = "\U0001F4C5"   # calendar
ICON_BMO = "\U0001F514"     # bell: before the open
ICON_AMC = "\U0001F319"     # crescent moon: after the close
ICON_DATA = "\U0001F4CA"    # bar chart: economic data
ICON_CONF = "\U0001F3A4"    # microphone: conferences


def x_length(text: str) -> int:
    """Length as X counts it: characters outside the Basic Multilingual
    Plane (every emoji used here) weigh two."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def cashtag_name(day) -> str | None:
    """The one name that gets a `$`: the bold earnings row with the
    largest market cap, else the first bold conference name (on the
    major-ticker list, which is what bolds it on the sheet), else None."""
    bold = [r for r in list(day.bmo) + list(day.amc) if getattr(r, "important", False)]
    if bold:
        return max(bold, key=lambda r: float(getattr(r, "cap_musd", 0) or 0)).symbol
    from report.news_data import _MAJOR_TICKERS
    for c in getattr(day, "conferences", None) or []:
        for t in c.tickers:
            if t in _MAJOR_TICKERS:
                return t
    return None


def _tags(symbols, lead: str | None = None) -> str:
    return ", ".join(f"${s}" if s == lead else s for s in symbols)


def _time_12h(t: str) -> str:
    """'14:00' -> '2:00 PM', '8:30' -> '8:30 AM'. Anything else as is."""
    try:
        h, m = (int(x) for x in t.split(":"))
    except (ValueError, AttributeError):
        return t
    if not (0 <= h < 24 and 0 <= m < 60):
        return t
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h < 12 else 'PM'}"


def _heading(day, today_iso: str | None) -> str:
    """Which day this sheet covers, in words a reader cannot misread."""
    label = (day.weekday_label or "").title()      # "Tuesday 9/22"
    weekday, _, md = label.partition(" ")
    if today_iso is None:
        import pytz
        from config import settings
        today_iso = datetime.now(pytz.timezone(settings.timezone)).strftime("%Y-%m-%d")
    try:
        covered = date.fromisoformat(day.date_iso)
        today = date.fromisoformat(today_iso)
    except (TypeError, ValueError):
        return f"{ICON_TITLE} {weekday}'s market calendar · {md}".strip()
    if covered - today == timedelta(days=1):
        return f"{ICON_TITLE} Tomorrow's market calendar · {label}"
    if covered == today:
        return f"{ICON_TITLE} Today's market calendar · {label}"
    return f"{ICON_TITLE} {weekday}'s market calendar · {md}"


def _earn_lines(day, max_each: int, lead: str | None) -> list[str]:
    out = []
    if day.bmo:
        out.append(f"{ICON_BMO} Before the open: "
                   + _tags((r.symbol for r in day.bmo[:max_each]), lead))
    if day.amc:
        out.append(f"{ICON_AMC} After the close: "
                   + _tags((r.symbol for r in day.amc[:max_each]), lead))
    return out


def _econ_line(day, important_only: bool) -> str:
    rows = [r for r in day.econ if (r.important or not important_only)]
    if not rows:
        return ""
    by_time: dict[str, list[str]] = {}
    for r in rows:
        by_time.setdefault(r.time_et, []).append(r.event)
    return f"{ICON_DATA} Data (ET): " + " · ".join(
        f"{_time_12h(t)} {', '.join(evs)}" for t, evs in by_time.items())


def _conf_line(day, max_tickers: int, lead: str | None = None) -> str:
    rows = getattr(day, "conferences", None) or []
    if not rows:
        return ""
    bits = []
    for c in rows:
        t = _tags(c.tickers[:max_tickers], lead)
        bits.append(f"{c.conference}" + (f" ({t})" if t else ""))
    return f"{ICON_CONF} Conferences: " + " · ".join(bits)


def _assemble(blocks: list[list[str]]) -> str:
    """Blank line between non-empty blocks."""
    return "\n\n".join("\n".join(b) for b in blocks if any(b))


def calendar_caption(day, today_iso: str | None = None) -> str:
    """`today_iso` is the posting date in ET; defaults to now. Passed in
    tests so the Tomorrow / weekday heading is deterministic."""
    title = _heading(day, today_iso)
    if day.is_holiday:
        text = title + "\n\nMarkets closed" + (
            f" · {day.is_holiday}" if isinstance(day.is_holiday, str) else "")
        return text
    # candidate builds, most complete first; the first that fits ships
    variants = [
        (False, 8, 6), (True, 8, 6), (True, 6, 4), (True, 5, 3),
        (True, 4, 0), (True, 3, 0), (True, 2, 0), (True, 0, 0),
    ]
    lead = cashtag_name(day)
    earn_syms = {r.symbol for r in list(day.bmo) + list(day.amc)}
    for important_only, max_each, max_conf in variants:
        shown = {r.symbol for r in list(day.bmo[:max_each]) + list(day.amc[:max_each])}
        # the `$` lands exactly once: in the earnings lines when the lead
        # is shown there, else in the conference line
        conf_lead = lead if lead not in shown else None
        if lead in earn_syms and lead not in shown:
            conf_lead = None  # trimmed off the earnings line; no `$` today
        blocks = [
            [title],
            _earn_lines(day, max_each, lead) if max_each else [],
            [l for l in (_econ_line(day, important_only),
                         _conf_line(day, max_conf, conf_lead) if max_conf else "") if l],
        ]
        text = _assemble(blocks)
        if x_length(text) <= X_LIMIT:
            return text
    text = title
    econ = _econ_line(day, True)
    if econ and x_length(title) + 2 + x_length(econ) <= X_LIMIT:
        text += "\n\n" + econ
    return text
