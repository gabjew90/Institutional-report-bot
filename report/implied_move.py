"""Earnings implied move — the ATM straddle priced into the first
expiry covering the report date, as a percent of spot.

WHY: "the market expects a ±6% move on this print" is the single most
useful number for an options trader looking at an earnings calendar,
and the sheet already ranks the names — it just never said what they
are priced to do.

METHOD: for the first expiration on or after the report date, take the
strike nearest spot and price the straddle from bid/ask midpoints
(call_mid + put_mid) / spot. Straddle-based, not an IV approximation:
IV needs a model and a rate assumption, the straddle is what someone
would actually pay.

HONESTY: an illiquid chain produces a garbage straddle, so a result is
returned ONLY when both legs have real two-sided quotes and the number
lands in a sane band. Everything else returns None and the sheet
renders a dash — the same discipline as the session-not-confirmed flag.

Source is yfinance (already a dependency, no key). It is scrape-
adjacent, so every failure path returns None and the column simply
goes missing rather than blocking the post.
"""

import logging

log = logging.getLogger(__name__)

# Sanity band. Sub-1% on an earnings print means the chain is stale or
# mispriced; >40% is a biotech-binary or a broken quote, not something
# to print on a calendar without context.
_MIN_PCT = 1.0
_MAX_PCT = 40.0


def _mid(opt: dict) -> float | None:
    """Bid/ask midpoint. Requires a REAL two-sided quote — a zero bid
    means nobody is buying and lastPrice is a fossil, which is exactly
    how an illiquid name produces a fake implied move."""
    bid = opt.get("bid")
    ask = opt.get("ask")
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    # A spread wider than the mid itself is not a price, it's a guess.
    mid = (bid + ask) / 2.0
    if (ask - bid) > mid:
        return None
    return mid


def _nearest_strike(options: list[dict], spot: float) -> float | None:
    strikes = [o.get("strike") for o in options
               if isinstance(o.get("strike"), (int, float))]
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s - spot))


def implied_move_pct(symbol: str, report_date_iso: str) -> float | None:
    """±% the ATM straddle implies for `symbol` through its first
    expiry on/after `report_date_iso`. None when unavailable or when
    the chain is too illiquid to price honestly."""
    from report import market_data as _md

    try:
        raw = _md._fetch_yahoo_options_chain(symbol)
    except Exception as e:
        log.info(f"implied move {symbol}: chain fetch failed ({e})")
        return None
    if not raw:
        return None

    spot = raw.get("underlying_spot_price")
    expirations = raw.get("expiration_dates") or []
    if not spot or spot <= 0 or not expirations:
        return None

    # First expiry that still covers the report — an expiry BEFORE the
    # print prices a different event entirely.
    target = next((e for e in sorted(expirations)
                   if e >= str(report_date_iso)[:10]), None)
    if not target:
        return None

    chain = raw.get("chain") or {}
    if chain.get("expiration_iso") != target:
        try:
            raw = _md._fetch_yahoo_options_chain(symbol, expiration_iso=target)
            chain = (raw or {}).get("chain") or {}
        except Exception as e:
            log.info(f"implied move {symbol}: expiry refetch failed ({e})")
            return None
    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    if not calls or not puts:
        return None

    strike = _nearest_strike(calls, spot)
    if strike is None:
        return None
    # Both legs must exist at the SAME strike, or it isn't a straddle.
    call = next((c for c in calls if c.get("strike") == strike), None)
    put = next((p for p in puts if p.get("strike") == strike), None)
    if not call or not put:
        return None

    cm, pm = _mid(call), _mid(put)
    if cm is None or pm is None:
        return None

    pct = (cm + pm) / float(spot) * 100.0
    if not (_MIN_PCT <= pct <= _MAX_PCT):
        log.info(f"implied move {symbol}: {pct:.1f}% outside sane band")
        return None
    return round(pct, 1)


def implied_moves_for(symbols: list[str], report_date_iso: str,
                      pace_seconds: float = 0.6) -> dict:
    """symbol -> ±% (or absent when unavailable). Paced: yfinance hits
    Yahoo, which throttles bursts. One bad symbol never aborts the run."""
    import time as _time
    out: dict = {}
    for i, sym in enumerate(symbols):
        try:
            pct = implied_move_pct(sym, report_date_iso)
            if pct is not None:
                out[sym] = pct
        except Exception as e:
            log.info(f"implied move {sym}: skipped ({e})")
        if i < len(symbols) - 1:
            _time.sleep(pace_seconds)
    return out
