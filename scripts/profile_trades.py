"""A member's own trade log for their profile (2026-10-08 audit).

Three defects in how the ledger reached the profile writer, one subsystem:

1. The block took the newest 200 trades across EVERY member and filtered
   to this one afterwards, so the "30-day" window reached back about six
   days. Profiles called members with winning closes "all talk".
2. An open whose expiry had passed was tagged "EXPIRED, no close posted"
   even when a close for the same contract followed it.
3. Impossible gains (-600%) were printed as fact.

On top of that the writer invented outcomes ("stopped out", "closed at a
realized loss", "took profits") for trades the log shows still open.
Since 2026-10-30 the Recent trades section is written here from the log
(`write_recent_trades`) and the writer's version is thrown away.
"""
from __future__ import annotations

import re
from datetime import date

_OPENS = ("open", "add")
# newest rows per member: an active caller logs 200+ in 30 days
MAX_ROWS = 150
_EXITS = ("trim", "close")


def member_trades(user_id: int, days: int = 30) -> list[dict]:
    """This member's trade rows, newest first, scoped in SQL."""
    import db
    return db.get_recent_analyst_trades(
        hours=days * 24, limit=MAX_ROWS, caller=None, tracking_mode=None,
        author_id=int(user_id))


def shown_gain(gain, contract_type=None) -> float | None:
    """A gain worth printing: the shared rule in db_parts.analyst
    (below -100% is an extraction error, and so is a share position up
    more than 300% in a month)."""
    from db_parts.analyst import plausible_gain
    return plausible_gain(gain, contract_type)


def _key(r: dict) -> tuple:
    strike = r.get("strike")
    try:
        strike = float(strike) if strike is not None else None
    except (TypeError, ValueError):
        strike = None
    return ((r.get("ticker") or "").upper(), strike,
            (r.get("contract_type") or "").lower()[:1])


def _strike_str(strike) -> str:
    if strike is None:
        return "?"
    try:
        f = float(strike)
    except (TypeError, ValueError):
        return str(strike)
    return f"{int(f)}" if f == int(f) else f"{f:g}"


def exits_by_contract(rows: list[dict]) -> dict[tuple, list[dict]]:
    out: dict[tuple, list[dict]] = {}
    for r in rows:
        if (r.get("action") or "").lower() in _EXITS:
            out.setdefault(_key(r), []).append(r)
    return out


def render_block(rows: list[dict], today: date | None = None) -> str:
    """The STRUCTURED TRADE LOG block the profile prompt carries."""
    if not rows:
        return ("(no structured trade log entries for this user in the window — "
                "Recent trades section must describe positions from chat WITHOUT "
                "inventing percentages)")
    today = today or date.today()
    exits = exits_by_contract(rows)
    lines = []
    for r in sorted(rows, key=lambda x: x.get("posted_at") or ""):
        ts = (r.get("posted_at") or "")[:16].replace("T", " ")
        action = (r.get("action") or "?").upper()
        ticker = r.get("ticker") or "?"
        suffix = {"call": "C", "put": "P"}.get((r.get("contract_type") or "").lower(), "")
        strike_str = _strike_str(r.get("strike"))
        expiry = r.get("expiry") or ""
        exp_short = expiry[5:] if len(expiry) >= 10 else (expiry or "?")
        gain = shown_gain(r.get("gain_pct"), r.get("contract_type"))
        gain_str = f"gain {gain:+.2f}%" if gain is not None else "gain ?"
        price = r.get("price")
        price_str = f" @{price}" if price not in (None, 0) else ""
        mode = ("(caller log)" if (r.get("tracking_mode") or "caller") == "caller"
                else "(member alert)")
        tag = ""
        status = (r.get("inferred_status") or "").lower()
        if action.lower() in _OPENS:
            later = [e for e in exits.get(_key(r), [])
                     if (e.get("posted_at") or "") >= (r.get("posted_at") or "")]
            expired = False
            if len(expiry) >= 10:
                try:
                    expired = date.fromisoformat(expiry[:10]) < today
                except ValueError:
                    pass
            if later:
                tag = "  [CLOSED later in this log, see its exit row]"
            elif status == "expired_unknown" or expired:
                tag = ("  [EXPIRED — expiry passed, NO close posted; OUTCOME UNKNOWN "
                       "(may have sold early, expired ITM, or expired worthless — "
                       "do NOT assert which)]")
            else:
                tag = ("  [OPEN per log — no exit posted; we only see screenshotted "
                       "trades, so they MAY have closed it without posting — do NOT "
                       "assert it's still held, and do NOT invent an exit]")
        elif status == "close_without_open":
            tag = "  [EXIT only — no logged entry]"
        lines.append(f"  - {ts}  {action:>5}  {ticker} {strike_str}{suffix}  "
                     f"exp {exp_short}  {gain_str}{price_str}  {mode}{tag}")
    return "\n".join(lines)


_SECTION_RE = re.compile(r"\*\*Recent trades\.?\*\*.*?(?=\n\s*\*\*|\Z)", re.S | re.I)
# Words the extractor has read as tickers (2026-10-30 audit M21: "MON").
_NOT_TICKERS = {"", "?", "UNCLEAR", "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}
SECTION_LINES = 6


def _day(ts: str) -> str:
    try:
        d = date.fromisoformat((ts or "")[:10])
    except ValueError:
        return "?"
    return f"{d:%b} {d.day}"


def _contract(r: dict) -> str:
    ticker = (r.get("ticker") or "?").upper()
    ct = (r.get("contract_type") or "").lower()
    if ct in ("call", "put"):
        exp = r.get("expiry") or ""
        exp = f" exp {exp[5:7]}/{exp[8:10]}" if len(exp) >= 10 else ""
        return f"{ticker} {_strike_str(r.get('strike'))}{ct[0].upper()}{exp}"
    return f"{ticker} shares" if ct == "stock" else ticker


def section_lines(rows: list[dict], today: date | None = None,
                  limit: int = SECTION_LINES) -> list[str]:
    """One line per contract from the log: what was opened and when, and
    the logged exit with its gain, or that no exit was posted. Closes come
    first, newest first, so a logged win is never left out for a run of
    opens (2026-10-30 audit M20)."""
    today = today or date.today()
    by_key: dict[tuple, list[dict]] = {}
    for r in sorted(rows, key=lambda x: x.get("posted_at") or ""):
        if (r.get("ticker") or "").upper() in _NOT_TICKERS:
            continue
        by_key.setdefault(_key(r), []).append(r)
    closed, other = [], []
    for events in by_key.values():
        entry = None                    # the first open since the last exit
        for e in events:
            if (e.get("action") or "").lower() in _EXITS:
                g = shown_gain(e.get("gain_pct"), e.get("contract_type"))
                result = f"{g:+.1f}%" if g is not None else "gain not recorded"
                if entry is not None:
                    text = (f"{_contract(entry)}: opened {_day(entry.get('posted_at'))}, "
                            f"closed {_day(e.get('posted_at'))}, {result}")
                else:
                    text = f"{_contract(e)}: exit posted {_day(e.get('posted_at'))}, {result}, no entry logged"
                closed.append((e.get("posted_at") or "", text))
                entry = None
            elif entry is None and (e.get("action") or "").lower() in _OPENS:
                entry = e
        if entry is None:
            continue
        price = entry.get("price")
        at = f" at {price:g}" if isinstance(price, (int, float)) and price else ""
        expiry = entry.get("expiry") or ""
        expired = (entry.get("inferred_status") or "").lower() == "expired_unknown"
        if len(expiry) >= 10:
            try:
                expired = expired or date.fromisoformat(expiry[:10]) < today
            except ValueError:
                pass
        tail = "expiry passed with no exit posted, outcome unknown" if expired else "no exit posted"
        other.append((entry.get("posted_at") or "",
                      f"{_contract(entry)}: opened {_day(entry.get('posted_at'))}{at}, {tail}"))
    picked = sorted(closed, reverse=True)[:limit]
    picked += sorted(other, reverse=True)[:limit - len(picked)]
    return [f"- {t}" for _, t in picked]


def write_recent_trades(profile_text: str, rows: list[dict],
                        today: date | None = None) -> str:
    """The profile with its Recent trades section written from the log.

    The writer's version is discarded (2026-10-30 audit, design note 3):
    it invented gains (M7), outcomes on open trades (M8), listed months-old
    chat trades as recent (M9), left the header empty (M15) and skipped
    logged wins (M20). Four regex passes on its text had not stopped it."""
    lines = section_lines(rows, today) or ["- No trades logged in the last 30 days."]
    section = "**Recent trades.**\n" + "\n".join(lines)
    m = _SECTION_RE.search(profile_text or "")
    if not m:
        return (profile_text or "").rstrip() + "\n\n" + section
    return profile_text[:m.start()] + section + profile_text[m.end():]
