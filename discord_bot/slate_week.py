"""A week of earnings, one line per session, built in code.

2026-10-26 audit A4: "all the big earnings this week" listed Monday to
Wednesday and stopped, leaving out IBM, TXN, PG, UNP, LMT, WDC and AXP.
The 2026-10-27 harness run then showed the fill-in check this module had
(add the line of every session the answer skipped) leaving two writers in
one post: the model's own lines out of order, the added lines after them,
and bare small names (FUBO). Since 2026-10-27 the week's lines are the
answer body, rendered here in date order, and the model writes only the
opening (discord_bot/answer_block.py).
"""
from __future__ import annotations

import re
from datetime import date

PER_SESSION = 4
# A name below this market cap is not a "big" report; a session with none
# above it says so rather than listing small caps (FUBO, 2026-10-27).
CAP_FLOOR_MUSD = 10_000
_SESSIONS = (("before_open", "before the open"), ("after_close", "after the close"))
# Legal suffixes the calendar feed carries ("Merck & Company, Inc.",
# "ExxonMobil Holdings Corporation"); a reader knows the company without them.
_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:and|&)\s+(?:Company|Co\.?)\b|,?\s+(?:Inc\.?|Incorporated|Corporation|Corp\.?|Company"
    r"|Co\.?|Ltd\.?|Limited|plc|PLC|LLC|L\.P\.|N\.V\.|S\.A\.|SA|AG|SE|Holdings?|Group|Class [A-C]"
    r"|Common Stock|Ordinary Shares|American Depositary Shares))+\s*$")


def short_name(name: str) -> str:
    """'Merck & Company, Inc.' -> 'Merck', 'Eli Lilly and Company' -> 'Eli Lilly'."""
    n = re.sub(r"\s*\(The\)\s*$", "", (name or "").strip())   # "Coca-Cola Company (The)"
    for _ in range(3):
        cut = _SUFFIX_RE.sub("", n).strip(" ,")
        if cut == n or not cut:
            break
        n = cut
    return n


def week_outline(days: list[dict], per_session: int = PER_SESSION) -> list[dict]:
    """[{date, session, symbols, names, caps, date_unconfirmed}] for each
    session of a week slate that has names, in date order, biggest names
    first. `caps` is in millions of dollars, 0 when the feed has none."""
    out: list[dict] = []
    for d in days or []:
        if d.get("status") != "ok":
            continue
        try:
            dt = date.fromisoformat(str(d.get("date"))[:10])
        except ValueError:
            continue
        for key, _label in _SESSIONS:
            seen, syms, names, caps, unconfirmed = set(), [], [], [], []
            for r in d.get(key) or []:
                nm = short_name(r.get("name") or "") or r["symbol"]
                if nm.lower() in seen:
                    continue            # GOOG and GOOGL are one company
                seen.add(nm.lower())
                syms.append(r["symbol"])
                names.append(nm)
                caps.append(float(r.get("market_cap_musd") or 0))
                if r.get("date_confirmed") is False:
                    unconfirmed.append(r["symbol"])
                if len(syms) == per_session:
                    break
            if syms:
                out.append({"date": dt.isoformat(), "session": key, "symbols": syms,
                            "names": names, "caps": caps, "date_unconfirmed": unconfirmed})
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


def block_lines(outline: list[dict], today: date,
                morning_reported: bool = False) -> list[str]:
    """One line per session from `today` on, in date order: the names at
    or above CAP_FLOOR_MUSD, or a line saying there are none.
    `morning_reported` (after 9:30 ET) skips today's before-open session,
    which has happened."""
    labels = dict(_SESSIONS)
    floor_b = CAP_FLOOR_MUSD // 1000
    lines = []
    for s in outline or []:
        if s["date"] < today.isoformat():
            continue
        if morning_reported and s["date"] == today.isoformat() and s["session"] == "before_open":
            continue
        dt = date.fromisoformat(s["date"])
        big = [(n, sym) for n, sym, c in zip(s["names"], s["symbols"], s.get("caps") or [])
               if c >= CAP_FLOOR_MUSD]
        odd = set(s.get("date_unconfirmed") or [])
        body = (", ".join((f"{n} ({sym})" if n != sym else sym)
                          + (" (date not confirmed)" if sym in odd else "") for n, sym in big)
                or f"nothing above ${floor_b}B")
        lines.append(f"→ **{dt:%a %b} {dt.day}, {labels[s['session']]}:** {body}")
    return lines


def mentions_any(text: str, outline: list[dict], today: date) -> int:
    """How many of the companies from `today` on the text names."""
    return sum(_mentions(text, sym, nm)
               for s in outline or [] if s["date"] >= today.isoformat()
               for sym, nm in zip(s["symbols"], s["names"]))
