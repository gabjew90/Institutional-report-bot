"""A week of earnings, one line per session, built in code.

2026-10-26 audit A4: "all the big earnings this week" listed Monday to
Wednesday and stopped, leaving out IBM, TXN, PG, UNP, LMT, WDC and AXP.
The week slate carries an outline of each session's biggest names, and an
answer that skips a session still to come gets that session's line added.

Checked per session, not per day (2026-10-27 harness run): the answer
named Wednesday's morning reports and left out Alphabet and Microsoft
after the close, and a per-day check counted Wednesday as covered.
"""
from __future__ import annotations

import re
from datetime import date

PER_SESSION = 4
_SESSIONS = (("before_open", "before the open"), ("after_close", "after the close"))
# Legal suffixes the calendar feed carries ("Merck & Company, Inc.",
# "ExxonMobil Holdings Corporation"); a reader knows the company without them.
_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:and|&)\s+(?:Company|Co\.?)\b|,?\s+(?:Inc\.?|Incorporated|Corporation|Corp\.?|Company"
    r"|Co\.?|Ltd\.?|Limited|plc|PLC|LLC|L\.P\.|N\.V\.|S\.A\.|SA|AG|SE|Holdings?|Group|Class [A-C]"
    r"|Common Stock|Ordinary Shares|American Depositary Shares))+\s*$")


def short_name(name: str) -> str:
    """'Merck & Company, Inc.' -> 'Merck', 'Eli Lilly and Company' -> 'Eli Lilly'."""
    n = (name or "").strip()
    for _ in range(3):
        cut = _SUFFIX_RE.sub("", n).strip(" ,")
        if cut == n or not cut:
            break
        n = cut
    return n


def week_outline(days: list[dict], per_session: int = PER_SESSION) -> list[dict]:
    """[{date, session, symbols, names, line}] for each session of a week
    slate that has names, in date order."""
    out: list[dict] = []
    for d in days or []:
        if d.get("status") != "ok":
            continue
        try:
            dt = date.fromisoformat(str(d.get("date"))[:10])
        except ValueError:
            continue
        for key, label in _SESSIONS:
            seen, syms, names = set(), [], []
            for r in d.get(key) or []:
                nm = short_name(r.get("name") or "") or r["symbol"]
                if nm.lower() in seen:
                    continue            # GOOG and GOOGL are one company
                seen.add(nm.lower())
                syms.append(r["symbol"])
                names.append(nm)
                if len(syms) == per_session:
                    break
            if syms:
                out.append({
                    "date": dt.isoformat(), "session": key, "symbols": syms, "names": names,
                    "line": f"→ **{dt:%a %b} {dt.day}, {label}:** "
                            + ", ".join(f"{n} ({s})" if n != s else s for n, s in zip(names, syms))})
    return out


def _mentions(answer: str, symbol: str, name: str) -> bool:
    """The answer names this company: a ticker of two letters or more, or
    the company's first name word. A one-letter ticker (F, T, C) matches
    ordinary text, so it counts only through the name (code review
    2026-10-26)."""
    if len(symbol) >= 2 and re.search(r"(?<![A-Za-z])\$?" + re.escape(symbol) + r"(?![A-Za-z])",
                                      answer):
        return True
    # a one-letter ticker written as one: "$V", "**V**"
    if len(symbol) == 1 and re.search(r"\$" + re.escape(symbol) + r"\b|\*\*" + re.escape(symbol)
                                      + r"\*\*", answer):
        return True
    words = [w.strip(",.()") for w in (name or "").split()]
    # the first word, or the second when the first is short ("Eli Lilly")
    word = next((w for w in words[:2] if len(w) >= 4), "")
    return bool(word) and bool(re.search(r"\b" + re.escape(word) + r"\b", answer, re.I))


def fill_missing_days(answer: str, outline: list[dict], today: date,
                      morning_reported: bool = False) -> tuple[str, list[str]]:
    """Add the line of every session from `today` on whose names the answer
    never mentions. `morning_reported` (after 9:30 ET) skips today's
    before-open session, which has happened. Returns (answer, sessions
    added as 'date:session')."""
    if not answer or not outline:
        return answer, []
    added, lines = [], []
    for s in outline:
        if s["date"] < today.isoformat():
            continue
        if morning_reported and s["date"] == today.isoformat() and s["session"] == "before_open":
            continue
        if any(_mentions(answer, sym, nm) for sym, nm in zip(s["symbols"], s["names"])):
            continue
        added.append(f"{s['date']}:{s['session']}")
        lines.append(s["line"])
    if not lines:
        return answer, []
    return answer.rstrip() + "\n\n" + "\n\n".join(lines), added
