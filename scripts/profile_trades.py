"""A member's own trade log for their profile (2026-10-08 audit).

Three defects in how the ledger reached the profile writer, one subsystem:

1. The block took the newest 200 trades across EVERY member and filtered
   to this one afterwards, so the "30-day" window reached back about six
   days. Profiles called members with winning closes "all talk".
2. An open whose expiry had passed was tagged "EXPIRED, no close posted"
   even when a close for the same contract followed it.
3. Impossible gains (-600%) were printed as fact.

On top of that the writer invented outcomes ("stopped out", "closed at a
realized loss", "took profits") for trades the log shows still open. The
prompt already forbids it; `ground_recent_trades` enforces it in code:
any Recent-trades commentary that claims an outcome the log does not
have is replaced with what the log says.
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


# Outcome claims a Recent-trades line may make only when the log has an
# exit for that contract.
_OUTCOME_RE = re.compile(
    r"\b(?:stopped\s+out|stop(?:ped)?[\s-]loss(?:ed)?|realized\s+(?:a\s+)?(?:loss|gain|profit)"
    r"|took\s+(?:profits?|gains|the\s+(?:win|loss|[lw]))|took\s+(?:a|the)\s+(?:loss|hit)"
    r"|scaled\s+out|cashed\s+out|locked\s+in|cut\s+(?:it|the|his|her|their|losses|bait)"
    r"|(?:sold|closed|exited|dumped|bailed)\b|expired\s+worthless|went\s+to\s+zero"
    r"|recover(?:ed|y)|banked|watch(?:ed)?\s+(?:the\s+)?stock\s+recover)", re.I)
_LOSS_RE = re.compile(r"\b(?:loss|lost|losing|bagholder|blew\s+up|wiped|zeroed)\b", re.I)
_WIN_RE = re.compile(r"\b(?:win|won|profit(?:s|able)?|printed|cashed)\b", re.I)
_CONTRACT_RE = re.compile(r"\$?\b([A-Z]{1,6})\s+\$?(\d+(?:\.\d+)?)\s*([CcPp])\b")
_COMMENT_RE = re.compile(r"\[[^\[\]]*\]\s*$")
_SECTION_RE = re.compile(r"(\*\*Recent trades\.\*\*\s*\n)(.*?)(?=\n\s*\*\*|\Z)", re.S)


def _log_says(contract_rows: list[dict], exits: list[dict]) -> str:
    if exits:
        last = sorted(exits, key=lambda e: e.get("posted_at") or "")[-1]
        g = shown_gain(last.get("gain_pct"), last.get("contract_type"))
        return f"[closed {g:+.2f}% per the log]" if g is not None else "[an exit is logged, gain not recorded]"
    return "[no exit posted in the log]"


def ground_recent_trades(profile_text: str, rows: list[dict]) -> tuple[str, list[str]]:
    """Replace Recent-trades commentary that claims an outcome the log does
    not show. Returns the text and the replaced lines (for the run log).
    Lines about tickers the log does not hold are left alone: they come
    from chat and cannot be checked here."""
    m = _SECTION_RE.search(profile_text or "")
    if not m or not rows:
        return profile_text, []
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault((r.get("ticker") or "").upper(), []).append(r)
    exits = exits_by_contract(rows)
    fixed, out_lines = [], []
    for line in m.group(2).split("\n"):
        com = _COMMENT_RE.search(line)
        if not line.lstrip().startswith("-") or not com:
            out_lines.append(line)
            continue
        head = line[:com.start()]
        # the contract anywhere before the commentary ("- Closed TSLA 385C")
        cm = _CONTRACT_RE.search(head)
        ticker = cm.group(1).upper() if cm else next(
            (w.upper() for w in re.findall(r"\$?\b([A-Za-z]{1,6})\b", head)
             if w.upper() in by_ticker), "")
        if ticker not in by_ticker:
            out_lines.append(line)
            continue
        if cm and cm.group(1).upper() == ticker:
            key = (ticker, float(cm.group(2)), cm.group(3).lower())
            contract_rows = [r for r in by_ticker[ticker] if _key(r) == key]
            contract_exits = exits.get(key, [])
        else:
            contract_rows = by_ticker[ticker]
            contract_exits = [e for k, v in exits.items() if k[0] == ticker for e in v]
        if not contract_rows:
            out_lines.append(line)
            continue
        comment = com.group(0)
        # A comment quoting a logged exit's own gain is about that exit.
        logged = {round(g, 2) for g in (shown_gain(e.get("gain_pct"), e.get("contract_type")) for e in contract_exits)
                  if g is not None}
        quoted = {round(float(x), 2) for x in re.findall(r"([+-]?\d+(?:\.\d+)?)\s*%", comment)}
        if logged & quoted or {abs(q) for q in quoted} & {abs(g) for g in logged}:
            out_lines.append(line)
            continue
        # Otherwise an exit counts only after the latest open: an earlier
        # round trip on the same strike says nothing about a re-open.
        opens = [r.get("posted_at") or "" for r in contract_rows
                 if (r.get("action") or "").lower() in _OPENS]
        if opens:
            contract_exits = [e for e in contract_exits
                              if (e.get("posted_at") or "") >= max(opens)]
        bad = False
        if not contract_exits:
            claims_loss = (_LOSS_RE.search(comment)
                           and not re.search(r"\bunrealized\b", comment, re.I))
            bad = bool(_OUTCOME_RE.search(comment) or claims_loss)
        else:
            last = sorted(contract_exits, key=lambda e: e.get("posted_at") or "")[-1]
            g = shown_gain(last.get("gain_pct"), last.get("contract_type"))
            if g is not None:
                bad = bool((g > 0 and _LOSS_RE.search(comment))
                           or (g < 0 and _WIN_RE.search(comment)))
        if bad:
            new = line[:com.start()] + _log_says(contract_rows, contract_exits)
            out_lines.append(new)
            fixed.append(f"{line.strip()[:120]} -> {_log_says(contract_rows, contract_exits)}")
        else:
            out_lines.append(line)
    if not fixed:
        return profile_text, []
    text = profile_text[:m.start(2)] + "\n".join(out_lines) + profile_text[m.end(2):]
    return text, fixed
