"""A week of earnings, one line per day, built in code.

2026-10-26 audit A4: "all the big earnings this week" listed Monday to
Wednesday and stopped, leaving out IBM, TXN, PG, UNP, LMT, WDC and AXP.
The week slate carries a per-day outline of its biggest names, and an
answer that skips a day that has not passed gets that day's line added.
"""
from __future__ import annotations

import re
from datetime import date

PER_SESSION = 4


def week_outline(days: list[dict], per_session: int = PER_SESSION) -> list[dict]:
    """[{date, symbols, line}] for each day of a week slate, in date order."""
    out: list[dict] = []
    for d in days or []:
        try:
            dt = date.fromisoformat(str(d.get("date"))[:10])
        except ValueError:
            continue
        label = f"{dt:%a %b} {dt.day}"
        if d.get("status") != "ok":
            continue
        parts, syms = [], []
        for key, name in (("before_open", "before the open"), ("after_close", "after the close")):
            rows = (d.get(key) or [])[:per_session]
            if rows:
                parts.append(f"{name} " + ", ".join(f"{r.get('name') or r['symbol']} {r['symbol']}"
                                                    for r in rows))
                syms += [r["symbol"] for r in rows]
        if syms:
            out.append({"date": dt.isoformat(), "symbols": syms,
                        "line": f"→ **{label}:** " + "; ".join(parts)})
    return out


def fill_missing_days(answer: str, outline: list[dict], today: date) -> tuple[str, list[str]]:
    """Add the line of every day from `today` on whose names the answer
    never mentions. Returns (answer, dates added)."""
    if not answer or not outline:
        return answer, []
    added, lines = [], []
    for day in outline:
        if day["date"] < today.isoformat():
            continue
        if any(re.search(r"(?<![A-Za-z])\$?" + re.escape(s) + r"(?![A-Za-z])", answer)
               for s in day["symbols"]):
            continue
        added.append(day["date"])
        lines.append(day["line"])
    if not lines:
        return answer, []
    return answer.rstrip() + "\n\n" + "\n\n".join(lines), added
