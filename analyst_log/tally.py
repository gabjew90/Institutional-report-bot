"""The trade log read as a record: one count, for one trader, a channel
or the whole room (2026-11-20, audit run 13 design 3).

On 2026-11-19 the room was given three records for one trader in one day:
"4W / 6L, 40% win rate" from the caller tally, "45W/15L" on his profile
line, and his rows hold 61 winning closes and 1 losing. Each reader
counted its own way:

  - the caller tally read only rows posted in the caller's alert channel,
    so an alert whose exit he posted in the main chat counted as an open
    that expired with no exit, a loss;
  - the member record counted closes and trims with no opens at all;
  - the profile line read the points ledger, which pairs entries and
    closes by its own rule;
  - "the win rate of the member channel" was answered with the two
    callers, and "returns on puts this month" with row counts from a
    query the model wrote ("67 wins and 4 losses", room: "that has to be
    inaccurate").

`build` is the one count. Positions come from `analyst_log/positions.py`,
built per trader from every row that trader posted in any channel, so an
exit is matched to its open wherever each was posted. What is counted:

  win       a close with a gain above 0
  loss      a close with a gain below 0
  expired   a position opened in the window that passed its expiry with
            no trim or close posted. The outcome is unknown: it counts
            with the losses in the win rate and is always its own figure.
            Where the sweep stored the underlying's close on expiry day,
            the contract is also counted as `expired_worthless` or
            `expired_in_money` (expiry_outcome.py), and
            `win_rate_worthless_pct` counts only the worthless ones as
            losses, once every such expiry is priced.
  open      a position opened in the window, not expired, not closed

A close is counted by the day it was posted, an expiry and an open by the
day the position was opened. A close with no gain logged is neither a win
nor a loss. A gain that cannot be a result (positions.plausible_gain) is
left out and counted in `discarded`.

Pure functions over row dicts: no database access.
"""
from __future__ import annotations

from datetime import date
from statistics import median

from analyst_log import expiry_outcome as X
from analyst_log import positions as P

KINDS = ("call", "put", "other")


def _ts(s) -> str:
    """A timestamp as 'YYYY-MM-DDTHH:MM:SS': stored rows carry 'T' and an
    offset, SQLite's datetime('now') a space."""
    return (s or "")[:19].replace(" ", "T")


def trader_of(r: dict):
    """Who a row belongs to: the Discord id, never a display name."""
    if r.get("_trader") is not None:
        return r["_trader"]
    if r.get("author_id") is not None:
        return int(r["author_id"])
    return "name:" + (r.get("caller") or r.get("author") or "").strip().lower()


def _kind(p: dict) -> str:
    return {"c": "call", "p": "put"}.get(p["key"][2], "other")


def _zero() -> dict:
    return {"wins": 0, "losses": 0, "expired_no_exit": 0, "expired_worthless": 0,
            "expired_in_money": 0, "open": 0, "opened": 0}


def _trade(p: dict, e: dict | None, gain: float | None, trader) -> dict:
    ref = e or p["entry"]
    out = {
        "trader": trader,
        "ticker": p["ticker"] or None,
        "contract_type": p["contract_type"],
        "strike": p["strike"],
        "expiry": p["expiry"],
        "posted_at": ref.get("posted_at"),
        "entry_price": P.entry_price(p),
    }
    if e is not None:
        out["gain_pct"] = gain
        out["exit_price"] = e.get("price") or None
        out["usd_per_contract"] = P.dollars_per_contract(p, e, gain)
    return out


def _rate(c: dict) -> float | None:
    decided = c["wins"] + c["losses"] + c["expired_no_exit"]
    return round(100.0 * c["wins"] / decided, 1) if decided else None


def _rate_worthless(c: dict) -> float | None:
    """Wins over the closes with a result plus the positions whose contract
    expired worthless with no exit posted. None unless every such expiry
    has its outcome, so the figure never mixes known and unknown."""
    if not c["expired_no_exit"] or c["expired_worthless"] + c["expired_in_money"] != c["expired_no_exit"]:
        return None
    decided = c["wins"] + c["losses"] + c["expired_worthless"]
    return round(100.0 * c["wins"] / decided, 1) if decided else None


def build(rows: list[dict], *, since: str, until: str | None = None, today: date | None = None,
          contract_type: str | None = None, ticker: str | None = None, in_scope=None) -> dict:
    """The record of the positions in `rows`.

    `rows` reach back before `since` so a close inside the window finds
    its open. `since` and `until` are UTC timestamps. `contract_type`
    'call' or 'put' and `ticker` keep only those positions. `in_scope`
    takes a position and says whether it belongs (a channel, a caller's
    alerts). Positions are still built from all of a trader's rows.
    """
    lo, hi = _ts(since), _ts(until) if until else None

    def _in(r: dict | None) -> bool:
        t = _ts((r or {}).get("posted_at"))
        return bool(t) and t > lo and (hi is None or t <= hi)

    by_trader: dict = {}
    for r in rows:
        by_trader.setdefault(trader_of(r), []).append(r)

    want = (contract_type or "").lower()[:1]
    tick = (ticker or "").upper()
    total = _zero()
    kinds = {k: _zero() for k in KINDS}
    traders: dict = {}
    wins, losses, silent = [], [], []
    flat = discarded = no_entry = 0

    for trader, trows in by_trader.items():
        for p in P.build(trows, today):
            if want and p["key"][2] != want:
                continue
            if tick and p["ticker"] != tick:
                continue
            if in_scope is not None and not in_scope(p):
                continue
            buckets = (total, kinds[_kind(p)], traders.setdefault(trader, _zero()))

            def add(field: str) -> None:
                for b in buckets:
                    b[field] += 1

            entered = _in(p["entry"])
            if entered:
                add("opened")
            for e in p["exits"]:
                if (e.get("action") or "").lower() != "close" or not _in(e):
                    continue
                if not e.get("gain_pct"):
                    flat += 1
                    continue
                g = P.plausible_gain(e.get("gain_pct"), p["contract_type"])
                if g is None:
                    discarded += 1
                    continue
                add("wins" if g > 0 else "losses")
                (wins if g > 0 else losses).append(_trade(p, e, g, trader))
                if p["entry"] is None:
                    no_entry += 1
            if entered and p["status"] == "expired_no_exit":
                add("expired_no_exit")
                # what the contract was worth at expiry, when the sweep
                # stored the underlying's close (expiry_outcome.py)
                close = next((r.get("expiry_close") for r in p["opens"] if r.get("expiry_close")), None)
                at = X.state(p["contract_type"], p["strike"], close)
                if at:
                    add("expired_worthless" if at == X.WORTHLESS else "expired_in_money")
                silent.append({**_trade(p, None, None, trader), "expiry_close": close, "at_expiry": at})
            elif entered and p["status"] == "open":
                add("open")

    # a trader with a position in scope and nothing inside the window is
    # not one of the traders counted
    traders = {k: c for k, c in traders.items() if any(c.values())}
    win_gains = [t["gain_pct"] for t in wins]
    loss_gains = [t["gain_pct"] for t in losses]
    scored = win_gains + loss_gains
    for c in (*kinds.values(), *traders.values()):
        c["win_rate_pct"] = _rate(c)
        c["win_rate_worthless_pct"] = _rate_worthless(c)
    return {
        **total,
        "decided": total["wins"] + total["losses"] + total["expired_no_exit"],
        "win_rate_pct": _rate(total),
        "win_rate_worthless_pct": _rate_worthless(total),
        "flat": flat,
        "discarded": discarded,
        "closes_without_entry": no_entry,
        "avg_win_pct": round(sum(win_gains) / len(win_gains), 1) if win_gains else None,
        "median_win_pct": round(median(win_gains), 1) if win_gains else None,
        "avg_loss_pct": round(sum(loss_gains) / len(loss_gains), 1) if loss_gains else None,
        "avg_close_pct": round(sum(scored) / len(scored), 1) if scored else None,
        "by_kind": kinds,
        "by_trader": traders,
        "win_trades": sorted(wins, key=lambda t: _ts(t["posted_at"]), reverse=True),
        "loss_trades": sorted(losses, key=lambda t: _ts(t["posted_at"]), reverse=True),
        "silent_expiry_trades": sorted(silent, key=lambda t: t["expiry"] or "", reverse=True),
    }
