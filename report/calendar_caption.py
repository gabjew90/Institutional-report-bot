"""The text that rides with the omni-calendar image on X.

The image carries the detail; the caption is the scannable summary and
must fit X's 280 characters. It is built greedily and trimmed from the
least important end: unimportant econ rows go first, then tickers past
the first few, then the industry line. Cashtag rule follows CLAUDE.md
(`$AAPL` for stocks and ETFs; the calendar only carries those).
"""
from __future__ import annotations

X_LIMIT = 280


def _econ_line(day, important_only: bool) -> str:
    rows = [r for r in day.econ if (r.important or not important_only)]
    if not rows:
        return ""
    by_time: dict[str, list[str]] = {}
    for r in rows:
        by_time.setdefault(r.time_et, []).append(r.event)
    parts = [f"{t} {', '.join(evs)}" for t, evs in by_time.items()]
    return "Economic (ET): " + " · ".join(parts)


def _earn_line(day, max_each: int) -> str:
    bits = []
    if day.bmo:
        bits.append("before open " + " ".join(f"${r.symbol}" for r in day.bmo[:max_each]))
    if day.amc:
        bits.append("after close " + " ".join(f"${r.symbol}" for r in day.amc[:max_each]))
    return ("Earnings: " + " · ".join(bits)) if bits else ""


def _industry_line(day, max_tickers: int) -> str:
    rows = getattr(day, "conferences", None) or []
    if not rows:
        return ""
    bits = []
    for c in rows:
        t = " ".join(f"${x}" for x in c.tickers[:max_tickers])
        bits.append(f"{c.conference}" + (f" ({t})" if t else ""))
    return "Industry: " + " · ".join(bits)


MAX_HASHTAGS = 8


def important_tickers(day) -> list[str]:
    """The names the sheet renders BOLD, in sheet order: important
    earnings rows (major-ticker list, mega-cap, or bank-covered this
    week) and the conference names on the major-ticker list. Hashtagged
    on X (owner call 2026-09-11)."""
    from report.news_data import _MAJOR_TICKERS
    out: list[str] = []
    for r in list(day.bmo) + list(day.amc):
        if getattr(r, "important", False) and r.symbol not in out:
            out.append(r.symbol)
    for c in getattr(day, "conferences", None) or []:
        for t in c.tickers:
            if t in _MAJOR_TICKERS and t not in out:
                out.append(t)
    return out


def _hashtag_line(day, max_tags: int) -> str:
    tags = important_tickers(day)[:max_tags]
    return " ".join(f"#{t}" for t in tags)


def calendar_caption(day) -> str:
    title = f"Market calendar · {day.weekday_label.title()}"
    if day.is_holiday:
        text = title + "\nMarkets closed" + (f" · {day.is_holiday}" if isinstance(day.is_holiday, str) else "")
        return text[:X_LIMIT]
    # candidate builds, most complete first; the first that fits ships.
    # The hashtag line is trimmed LAST: it is the part the owner asked
    # for, so econ detail, extra tickers and the industry line give way
    # before a single hashtag does.
    variants = [
        (False, 6, 6, MAX_HASHTAGS), (True, 6, 6, MAX_HASHTAGS), (True, 5, 4, MAX_HASHTAGS),
        (True, 4, 3, MAX_HASHTAGS), (True, 3, 0, MAX_HASHTAGS), (True, 2, 0, MAX_HASHTAGS),
        (True, 2, 0, 5), (True, 0, 0, 5), (True, 0, 0, 3),
    ]
    for important_only, max_each, max_ind, max_tags in variants:
        lines = [title, _econ_line(day, important_only),
                 _earn_line(day, max_each) if max_each else ""]
        if max_ind:
            lines.append(_industry_line(day, max_ind))
        lines.append(_hashtag_line(day, max_tags))
        text = "\n".join(l for l in lines if l)
        if len(text) <= X_LIMIT:
            return text
    # last resort: title and whatever fits of the econ line
    text = title
    econ = _econ_line(day, True)
    if econ and len(title) + 1 + len(econ) <= X_LIMIT:
        text += "\n" + econ
    return text[:X_LIMIT]
