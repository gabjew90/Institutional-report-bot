"""The trade log read as positions: one reading, used by every reader.

2026-11-11 audit (run 11). On that morning the bot told the room Abe had
gone 13 wins and 11 losses over 30 days. The log held 13 winning closes
and 6 opens with no exit posted. Five winners were counted a second time
as losses, because each reader worked out a trade's outcome from the raw
rows by its own rule:

  - the daily sweep marked every row past its expiry `expired_unknown`,
    closes included, and opens whose close was logged;
  - the W/L tally counted every open carrying that mark as a loss;
  - the /ask trade block printed "[expired, no close alert]" beside an
    open that had its close on the next line;
  - only the profile's Recent trades matched exits to their opens.

`build` is that matching, moved here. A position is one contract from
its first open to its last exit. The sweep, the tally, the trade block
and the profile all read it, so they cannot disagree about whether a
trade was closed.

Pure functions over row dicts: no database access.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

OPENS = ("open", "add")
EXITS = ("trim", "close")


def contract_key(r: dict) -> tuple:
    """(TICKER, strike, 'c' | 'p' | ''). Calls and puts by their letter;
    shares under whatever type the extractor gave them ("stock",
    "unclear", blank) are one position. Expiry is left out: a close read
    from a P&L card often has none."""
    strike = r.get("strike")
    try:
        strike = float(strike) if strike is not None else None
    except (TypeError, ValueError):
        strike = None
    ct = (r.get("contract_type") or "").lower()
    return ((r.get("ticker") or "").upper(), strike, ct[:1] if ct in ("call", "put") else "")


def distinct(events: list[dict]) -> list[dict]:
    """The events without repeats: the same action on the same day with
    the same gain and price is one post read twice (as text and as a
    screenshot, or two screenshots in one message)."""
    seen, out = set(), []
    for e in events:
        k = ((e.get("action") or "").lower(), (e.get("posted_at") or "")[:10],
             e.get("gain_pct"), e.get("price"))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _expired(expiry: str | None, today: date) -> bool:
    if not expiry or len(expiry) < 10:
        return False
    try:
        return date.fromisoformat(expiry[:10]) < today
    except ValueError:
        return False


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f else None


def build(rows: list[dict], today: date | None = None) -> list[dict]:
    """Positions from one trader's trade rows, oldest first.

    Per contract, in time order: the first open starts a position, every
    trim and close after it is an exit of that position, and an open that
    follows an exit starts the next one. Exits with no open before them
    are a position with `entry` None (a result posted without its entry).

    Each position:
      key, ticker, contract_type, strike, expiry
      entry      the first open row, or None
      opens      every open and add row of the position
      exits      every trim and close row, in order
      rows       every row of the position, repeats of one post included
      closed     True when one of the exits is a close
      expired    the expiry has passed
      status     "closed" | "open" | "expired_no_exit" | "expired_after_trim"
    """
    today = today or datetime.now(timezone.utc).date()
    by_key: dict[tuple, list[dict]] = {}
    for r in sorted(rows, key=lambda x: x.get("posted_at") or ""):
        act = (r.get("action") or "").lower()
        if act not in OPENS and act not in EXITS:
            continue
        by_key.setdefault(contract_key(r), []).append(r)

    out: list[dict] = []

    def emit(key, entry, opens, exits, rows_):
        ref = entry or exits[-1]
        expiry = next((x.get("expiry") for x in [entry, *reversed(exits)] if x and x.get("expiry")), None)
        closed = any((e.get("action") or "").lower() == "close" for e in exits)
        expired = _expired(expiry, today)
        if closed:
            status = "closed"
        elif expired:
            status = "expired_after_trim" if exits else "expired_no_exit"
        else:
            status = "open"
        out.append({
            "key": key, "ticker": key[0], "strike": key[1],
            "contract_type": (ref.get("contract_type") or "").lower() or None,
            "expiry": expiry, "entry": entry, "opens": opens, "exits": exits,
            "rows": rows_,
            "closed": closed, "expired": expired, "status": status,
        })

    for key, events in by_key.items():
        entry, opens, exits, rows_, seen = None, [], [], [], set()
        for e in events:
            act = (e.get("action") or "").lower()
            if act in OPENS and exits:
                # an open after an exit is the next position, even when it
                # carries the same day and price as the first entry
                emit(key, entry, opens, exits, rows_)
                entry, opens, exits, rows_, seen = None, [], [], [], set()
            k = (act, (e.get("posted_at") or "")[:10], e.get("gain_pct"), e.get("price"))
            rows_.append(e)
            if k in seen:
                # one post read twice: it belongs to the position, and is
                # not a second event of it
                continue
            seen.add(k)
            if act in EXITS:
                exits.append(e)
                continue
            if entry is None:
                entry = e
            opens.append(e)
        if entry is not None or exits:
            emit(key, entry, opens, exits, rows_)
    out.sort(key=lambda p: ((p["entry"] or p["exits"][0]).get("posted_at") or ""))
    return out


def position_of(positions: list[dict]) -> dict[int, dict]:
    """Row id -> the position that row belongs to."""
    out: dict[int, dict] = {}
    for p in positions:
        for r in p["rows"]:
            if r.get("id") is not None:
                out[int(r["id"])] = p
    return out


def entry_price(p: dict) -> float | None:
    return _num((p.get("entry") or {}).get("price"))


def dollars_per_contract(p: dict, exit_row: dict, gain: float | None) -> float | None:
    """What one option contract made or lost on this exit, from the logged
    entry and exit prices, or from the entry price and the logged gain.
    None for shares and when the entry price was never logged."""
    if (p.get("contract_type") or "") not in ("call", "put"):
        return None
    ep = entry_price(p)
    if ep is None:
        return None
    xp = _num(exit_row.get("price"))
    if xp is not None:
        return round((xp - ep) * 100, 2)
    if gain is None:
        return None
    return round(ep * gain, 2)


def should_be_marked_expired(rows: list[dict], today: date | None = None) -> set[int]:
    """The ids of open and add rows whose position expired with no exit
    posted. These, and only these, carry `inferred_status =
    'expired_unknown'`."""
    ids: set[int] = set()
    for p in build(rows, today):
        if p["status"] != "expired_no_exit":
            continue
        ids.update(int(r["id"]) for r in p["rows"]
                   if r.get("id") is not None and (r.get("action") or "").lower() in OPENS)
    return ids


def distinct_trades(rows: list[dict]) -> list[dict]:
    """Rows across contracts without repeats: `distinct` per contract."""
    by_key: dict[tuple, list[dict]] = {}
    for r in rows:
        by_key.setdefault(contract_key(r), []).append(r)
    return [e for events in by_key.values() for e in distinct(events)]
