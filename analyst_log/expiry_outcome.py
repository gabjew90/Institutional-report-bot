"""What a contract was worth at expiry, when no exit was posted (2026-11-20).

A position that expired with no trim or close posted has an unknown result
in the trade log: the trader may have sold it, or held it to zero. One part
is not unknown. The underlying's close on expiry day against the strike says
whether the contract finished out of the money, worth nothing to anyone
still holding it, or in the money.

Over the 45 days to 2026-11-20 the room had 157 such positions. For 155 the
close was available: 113 finished out of the money and 42 in the money.

This states a fact about the contract, not about the trader. A contract
that finished worthless may have been sold earlier at a gain or a loss that
was never posted. The record says "expired worthless with no exit posted",
never "lost".

The close is stored once, by the daily sweep, on the day after expiry
(`analyst_trades.expiry_close`). Reading it later would be wrong after a
stock split, because the price feed restates old closes and a stored strike
is never restated. For the same reason a close far from the strike (outside
strike_check.BAND) is not stored: it is more likely a restated price or a
misread strike than a result.
"""
from __future__ import annotations

WORTHLESS = "worthless"
IN_MONEY = "in_money"


def intrinsic(contract_type, strike, close) -> float | None:
    """The contract's value per share at expiry, None when it cannot be
    worked out (no close stored, no strike, not a call or a put)."""
    kind = (contract_type or "").strip().lower()[:1]
    try:
        k, px = float(strike), float(close)
    except (TypeError, ValueError):
        return None
    if kind not in ("c", "p") or k <= 0 or px <= 0:
        return None
    return max(0.0, px - k) if kind == "c" else max(0.0, k - px)


def state(contract_type, strike, close) -> str | None:
    """WORTHLESS, IN_MONEY, or None when unknown."""
    v = intrinsic(contract_type, strike, close)
    if v is None:
        return None
    return IN_MONEY if v > 0 else WORTHLESS


def closes_for(rows: list[dict], price_on=None) -> dict[int, float]:
    """row id -> the underlying's close on the row's expiry day, for rows
    with a ticker, a strike, an expiry and a call or put. Blocking (the
    price feed): call it from a worker thread.

    A row whose close cannot be found, or sits outside the plausible band
    around the strike, is left out and stays unknown."""
    from analyst_log import strike_check
    price_on = price_on or strike_check.price_on
    out: dict[int, float] = {}
    for r in rows:
        tk = (r.get("ticker") or "").strip().upper()
        day = str(r.get("expiry") or "")[:10]
        if not tk or len(day) != 10 or (r.get("contract_type") or "").lower() not in ("call", "put"):
            continue
        try:
            strike = float(r.get("strike"))
        except (TypeError, ValueError):
            continue
        try:
            px = price_on(tk, day)
        except Exception:
            px = None
        if px and strike > 0 and strike_check.fits(strike, px):
            out[int(r["id"])] = round(float(px), 4)
    return out
