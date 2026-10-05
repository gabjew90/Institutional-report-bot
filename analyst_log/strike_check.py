"""Is an extracted option's strike possible for its underlying? (2026-10-05)

The trade ledger stored contracts that cannot exist: a QQQ 165 call with
QQQ near 750, an SPX 370 call with SPX near 7,700, a SOXL 1.0 call, an
NDX 7685 call (7685 is an SPX level). The model filled a ticker from a
different message than the strike, or picked an index by strike alone,
and nothing compared the result with the market. Those rows feed the
member ledger, profiles and the money jokes in /ask.

`apply` compares the strike with the underlying's close on the post's
date. A strike outside BAND times that price is not a trade. An index
whose strike fits a different index (7685 under NDX) is moved to the one
it fits. When the price is unknown the trade is kept: a missing quote
must never delete a real trade.

Blocking (yfinance): the live watcher calls it through asyncio.to_thread;
the member batch already runs in a worker thread.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

# Far-out-of-the-money lottos and deep in-the-money calls both sit well
# inside this; the rows it is for were off by 3.5x to 150x.
BAND = (0.4, 2.5)

# Yahoo symbol and scale for the index tickers the room trades by strike.
_INDEX = {"SPX": ("^GSPC", 1.0), "SPXW": ("^GSPC", 1.0), "XSP": ("^GSPC", 0.1),
          "NDX": ("^NDX", 1.0), "NDXP": ("^NDX", 1.0), "RUT": ("^RUT", 1.0)}
# The indexes a strike-only trade may be moved to, and the tickers it may
# be moved from ("7685c gotta work" came back as NDX; SPY or QQQ is the
# same mistake one step over).
_REASSIGN = ("SPX", "NDX", "RUT")
_MOVABLE = set(_INDEX) | {"SPY", "QQQ", "IWM"}
# Tickers whose Yahoo symbol is a different instrument (BTC is a Grayscale
# trust near $50, ES is Eversource): their options trade on crypto and
# futures venues, so a Yahoo price would reject real trades.
_UNPRICED = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ES", "NQ", "MES", "MNQ", "YM",
             "RTY", "CL", "GC", "SI", "ZB", "ZN", "NG"}
# A failed lookup is retried after this many seconds, not cached for the day.
_MISS_TTL_S = 900

_cache: dict[tuple[str, str], tuple[float | None, float]] = {}
_lock = threading.Lock()


def _offline() -> bool:
    """Tests and smokes set STRIKE_CHECK_OFFLINE so no run touches Yahoo."""
    return os.environ.get("STRIKE_CHECK_OFFLINE") == "1"


def _day(posted_at: str) -> str:
    try:
        return datetime.fromisoformat(str(posted_at).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return date.today().isoformat()


def price_on(ticker: str, day_iso: str) -> float | None:
    """The underlying's last close on or before `day_iso`, in the
    ticker's own units (XSP is a tenth of SPX). None when unknown."""
    if _offline() or ticker in _UNPRICED:
        return None
    sym, scale = _INDEX.get(ticker, (ticker, 1.0))
    key = (sym, day_iso)
    with _lock:
        hit = _cache.get(key)
        if hit and (hit[0] is not None or time.monotonic() - hit[1] < _MISS_TTL_S):
            return None if hit[0] is None else hit[0] * scale
    from report.market_data import fetch_price_history
    start = (date.fromisoformat(day_iso) - timedelta(days=7)).isoformat()
    end = (date.fromisoformat(day_iso) + timedelta(days=1)).isoformat()
    hist = fetch_price_history(sym, start, end) or []
    bars = [b for b in hist if b.get("date", "") <= day_iso and b.get("close")]
    px = float(bars[-1]["close"]) if bars else None
    with _lock:
        _cache[key] = (px, time.monotonic())
    return None if px is None else px * scale


def fits(strike: float, price: float) -> bool:
    return BAND[0] * price <= strike <= BAND[1] * price


def apply(extracted: dict, posted_at: str) -> dict:
    """Check an extraction in place and return it. An option whose strike
    is impossible for its underlying becomes a non-trade; an index option
    whose strike fits another index is moved to it."""
    if not extracted or not extracted.get("is_trade_screenshot"):
        return extracted
    if (extracted.get("contract_type") or "").lower() not in ("call", "put"):
        return extracted
    ticker = (extracted.get("ticker") or "").upper()
    try:
        strike = float(extracted.get("strike"))
    except (TypeError, ValueError):
        return extracted
    if not ticker or strike <= 0:
        return extracted
    day = _day(posted_at)
    try:
        px = price_on(ticker, day)
    except Exception as e:
        log.info(f"strike check: no price for {ticker} on {day}: {e}")
        return extracted
    if px is None or fits(strike, px):
        return extracted
    if ticker in _MOVABLE:
        for alt in _REASSIGN:
            if alt == ticker:
                continue
            try:
                alt_px = price_on(alt, day)
            except Exception:
                alt_px = None
            if alt_px is not None and fits(strike, alt_px):
                log.info(f"strike check: {ticker} {strike:g} moved to {alt} "
                         f"({ticker} {px:,.0f}, {alt} {alt_px:,.0f})")
                extracted["ticker"] = alt
                extracted["notes"] = (str(extracted.get("notes") or "") +
                                      f"; index moved from {ticker} by strike").lstrip("; ")
                return extracted
    log.info(f"strike check: {ticker} {strike:g} rejected, underlying {px:,.2f} on {day}")
    extracted["is_trade_screenshot"] = False
    extracted["what_it_appears_to_be"] = (
        f"strike {strike:g} is not possible for {ticker} near {px:,.2f}")
    return extracted
