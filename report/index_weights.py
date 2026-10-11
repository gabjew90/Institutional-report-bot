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


def ex_top_return(spy_ytd: float, holdings: dict, ytd: dict) -> dict | None:
    """Year-to-date return of the S&P without its ten largest holdings.

    Each holding's start-of-year weight is backed out of today's weight
    (w0 = w * (1 + R) / (1 + r)); the rest of the index returned
    (R - sum(w0 * r)) / (1 - sum(w0)). Exact for a buy-and-hold
    cap-weighted index, close for the real one (rebalances and share
    changes are ignored). Inputs are fractions; output is percent.
    2026-10-11 audit: "what would the S&P gain be without the top 10"
    got an equal-weight range and no number."""
    if spy_ytd is None or not holdings:
        return None
    rows, w0_sum, contrib = [], 0.0, 0.0
    for sym, w in holdings.items():
        r = ytd.get(sym)
        if r is None:
            return None
        w0 = w * (1 + spy_ytd) / (1 + r)
        w0_sum += w0
        contrib += w0 * r
        rows.append({"symbol": sym, "weight_pct": round(100 * w, 1), "ytd_pct": round(100 * r, 1)})
    if w0_sum >= 1:
        return None
    rest = (spy_ytd - contrib) / (1 - w0_sum)
    return {"spx_ytd_pct": round(100 * spy_ytd, 1),
            "top_holdings": rows,
            "top_contribution_pts": round(100 * contrib, 1),
            "ex_top_ytd_pct": round(100 * rest, 1)}


def _spy_holdings() -> tuple[dict, dict]:
    import yfinance as yf
    fd = yf.Ticker("SPY").funds_data
    sectors = dict(fd.sector_weightings or {})
    holdings = {str(sym).upper(): float(row["Holding Percent"])
                for sym, row in fd.top_holdings.iterrows()}
    return sectors, holdings


def ex_top10() -> dict | None:
    """ex_top_return for SPY's current top ten holdings, cached six hours;
    None if Yahoo fails. Alphabet's two share classes are two holdings."""
    now = time.time()
    cached = _cache.get("ex_top10")
    if cached and now - cached[0] < _CACHE_SECONDS:
        return cached[1]
    try:
        import datetime as _dt
        import yfinance as yf
        import math
        _, holdings = _spy_holdings()
        # Yahoo spells share classes with a hyphen (BRK.B is BRK-B)
        holdings = {s.replace(".", "-"): w for s, w in holdings.items()}
        syms = ["SPY"] + list(holdings)
        year = _dt.date.today().year
        close = yf.download(syms, start=f"{year - 1}-12-20", progress=False,
                            auto_adjust=True)["Close"]
        base = close.loc[:f"{year - 1}-12-31"].iloc[-1]
        last = close.iloc[-1]
        ytd = {}
        for s in syms:
            v = float(last[s] / base[s] - 1) if s in close.columns else float("nan")
            if not math.isfinite(v):
                return None     # a name Yahoo could not price: no estimate
            ytd[s] = v
        value = ex_top_return(ytd["SPY"], holdings, ytd)
        if value:
            value["as_of"] = str(close.index[-1].date())
    except Exception as e:
        log.info(f"index_weights: ex-top-10 unavailable: {e}")
        return None
    if value:
        _cache["ex_top10"] = (now, value)
    return value


def tech_share() -> dict | None:
    """The computed shares, cached six hours; None if Yahoo fails."""
    now = time.time()
    if _cache["value"] and now - _cache["at"] < _CACHE_SECONDS:
        return _cache["value"]
    try:
        sectors, holdings = _spy_holdings()
        value = compute(sectors, holdings)
    except Exception as e:
        log.info(f"index_weights: SPY holdings unavailable: {e}")
        return None
    if value:
        _cache.update(at=now, value=value)
    return value
