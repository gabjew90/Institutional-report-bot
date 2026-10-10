"""How much of the S&P 500 is tech, computed from SPY's holdings (owner,
2026-10-10).

"What % of the S&P is tech" means, to this room, the tech sector plus
Alphabet, Meta and Amazon, which the sector scheme files elsewhere. Left
to the model with a search, the first answer gave the broad share as
40.2% beside a 38.9% sector: it never added the three companies (about
12 points). Both figures are computed here instead, from Yahoo's view of
SPY: its sector weights (Morningstar's sectors, where Alphabet and Meta
sit in communication services and Amazon in consumer cyclical, so nothing
is counted twice) and its top holdings.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Alphabet's two share classes are listed separately in the holdings.
ADDED = (("Alphabet", ("GOOGL", "GOOG")), ("Amazon", ("AMZN",)), ("Meta", ("META",)))
_CACHE_SECONDS = 6 * 3600
_cache: dict = {"at": 0.0, "value": None}


def compute(sector_weights: dict, holdings: dict) -> dict | None:
    """{tech, adds: {name: pct}, broad} in percent, or None when SPY's
    data lacks the tech sector or any of the added companies."""
    tech = sector_weights.get("technology")
    if not tech:
        return None
    adds = {}
    for name, symbols in ADDED:
        parts = [holdings.get(s) for s in symbols]
        if any(p is None for p in parts):
            return None
        adds[name] = round(100 * sum(parts), 1)
    tech_pct = round(100 * tech, 1)
    return {"tech": tech_pct, "adds": adds,
            "broad": round(tech_pct + sum(adds.values()), 1)}


def tech_share() -> dict | None:
    """The computed shares, cached six hours; None if Yahoo fails."""
    now = time.time()
    if _cache["value"] and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["value"]
    try:
        import yfinance as yf
        fd = yf.Ticker("SPY").funds_data
        sectors = dict(fd.sector_weightings or {})
        top = fd.top_holdings
        holdings = {str(sym).upper(): float(row["Holding Percent"])
                    for sym, row in top.iterrows()}
        value = compute(sectors, holdings)
    except Exception as e:
        log.info(f"index_weights: SPY holdings unavailable: {e}")
        return None
    if value:
        _cache.update(at=now, value=value)
    return value
