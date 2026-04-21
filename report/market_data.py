"""Fetch live market data to ground the Market Pulse in current reality.

Sources:
- CoinGecko public API (no key) for BTC/ETH with 24h and 7d changes
- Yahoo Finance v7 quote endpoint (no key) for indices/commodities/FX

This runs synchronously at pulse time, inserted into the synthesis prompt so
Gemini uses current prices instead of whatever stale numbers the research quoted.
"""

import json
import logging
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

_COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana"
    "&vs_currencies=usd"
    "&include_24hr_change=true"
    "&include_7d_change=true"
)

# Yahoo Finance tickers for the indicators we reference in pulses
_YAHOO_TICKERS = {
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "VIX": "^VIX",
    "Brent Crude": "BZ=F",
    "WTI Crude": "CL=F",
    "Gold": "GC=F",
    "10Y Treasury": "^TNX",
    "DXY": "DX-Y.NYB",
}


def _fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"Market data fetch failed for {url[:60]}: {e}")
        return None


def _fetch_crypto() -> dict:
    data = _fetch_json(_COINGECKO_URL)
    if not data:
        return {}
    out = {}
    for key, name in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
        if key in data:
            out[name] = {
                "price": data[key].get("usd"),
                "change_24h": data[key].get("usd_24h_change"),
                "change_7d": data[key].get("usd_7d_change"),
            }
    return out


def _fetch_yahoo_single(symbol: str) -> dict | None:
    """Fetch last close + previous close for a single symbol via v8 chart endpoint."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=5d"
    data = _fetch_json(url)
    if not data:
        return None
    try:
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice")
        prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price is None:
            return None
        change_pct = None
        if prev_close:
            change_pct = ((price - prev_close) / prev_close) * 100
        return {"price": price, "prev_close": prev_close, "change_pct": change_pct}
    except (KeyError, IndexError, TypeError):
        return None


def _fetch_yahoo() -> dict:
    out = {}
    for name, symbol in _YAHOO_TICKERS.items():
        data = _fetch_yahoo_single(symbol)
        if data:
            out[name] = data
    return out


def fetch_market_snapshot() -> str:
    """Return a human-readable snapshot of current market levels for the prompt."""
    import pytz
    crypto = _fetch_crypto()
    traditional = _fetch_yahoo()

    et = pytz.timezone("America/New_York")
    now_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(et)
    ts = now_et.strftime("%Y-%m-%d %H:%M %Z")
    lines = [f"CURRENT MARKET DATA (as of {ts}):", ""]

    if traditional:
        lines.append("Traditional markets:")
        for name, data in traditional.items():
            price = data.get("price")
            pct = data.get("change_pct")
            if price is None:
                continue
            pct_str = f"{pct:+.2f}%" if pct is not None else "n/a"
            lines.append(f"  {name}: {price:,.2f} ({pct_str} today)")
        lines.append("")

    if crypto:
        lines.append("Crypto:")
        for name, data in crypto.items():
            price = data.get("price")
            c24 = data.get("change_24h")
            c7d = data.get("change_7d")
            if price is None:
                continue
            c24_str = f"{c24:+.2f}%" if c24 is not None else "n/a"
            c7d_str = f"{c7d:+.2f}%" if c7d is not None else "n/a"
            lines.append(f"  {name}: ${price:,.0f} ({c24_str} 24h, {c7d_str} 7d)")
        lines.append("")

    if not crypto and not traditional:
        lines.append("(Market data unavailable — using research-quoted numbers.)")

    return "\n".join(lines)
