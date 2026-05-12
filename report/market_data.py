"""Fetch live market data to ground the Market Pulse in current reality.

Sources:
- Binance.US for BTC/ETH/SOL (price + 24h change + midnight UTC anchor)
- Finnhub /quote for traditional markets (SPY/QQQ/VIXY/oil/gold/TLT/UUP)

CoinGecko was previously the primary crypto source but its free public API
became unreliable from Railway IPs (consistent HTTP 429). Binance.US public
endpoints are unauthenticated, generous-limit, and provide both current
price and the 24h ticker we need.

This runs synchronously at pulse time, inserted into the synthesis prompt so
the synthesis layer uses current prices instead of whatever stale numbers
the research quoted.
"""

import json
import logging
import urllib.request
from datetime import datetime

log = logging.getLogger(__name__)

# Traditional market tickers fetched from Finnhub. Using ETF tickers directly
# in the pulse (no $SPX/$NDX/etc. translation) per user preference.
_FINNHUB_TICKERS = [
    "SPY",    # S&P 500 ETF
    "QQQ",    # Nasdaq 100 ETF
    "VIXY",   # VIX short-term futures ETF
    "BNO",    # Brent oil ETF
    "USO",    # WTI oil ETF
    "GLD",    # Gold ETF
    "TLT",    # Long duration Treasuries (inverse yield)
    "UUP",    # US Dollar index ETF
]


def _fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"Market data fetch failed for {url[:60]}: {e}")
        return None


def _fetch_binance_24h(symbol: str) -> dict | None:
    """Get current price + rolling 24h change from Binance.US."""
    url = f"https://api.binance.us/api/v3/ticker/24hr?symbol={symbol}"
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return None
    try:
        return {
            "price": float(data.get("lastPrice")),
            "change_24h_rolling": float(data.get("priceChangePercent")),
        }
    except (ValueError, TypeError):
        return None


def _fetch_binance_midnight(symbol: str) -> float | None:
    """Get today's 00:00 UTC open price via Binance klines (no auth, generous limits).

    Uses Binance.US (api.binance.us) since the main Binance endpoint is geo-blocked
    with HTTP 451 for US IPs including Railway. symbol: Binance pair like 'BTCUSDT'.
    Returns the open of today's current 1d candle = exactly midnight UTC price.
    """
    url = f"https://api.binance.us/api/v3/klines?symbol={symbol}&interval=1d&limit=1"
    data = _fetch_json(url)
    if not data or not isinstance(data, list) or not data:
        return None
    try:
        # Kline format: [open_time, open, high, low, close, volume, ...]
        return float(data[0][1])
    except (ValueError, IndexError, TypeError):
        return None


def _fetch_crypto() -> dict:
    """Fetch crypto prices via Binance.US (current + 24h rolling + midnight UTC anchor)."""
    binance_map = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}
    out = {}
    for name, symbol in binance_map.items():
        b = _fetch_binance_24h(symbol)
        if not b:
            continue
        current = b["price"]
        rolling_24h = b["change_24h_rolling"]
        midnight_utc_price = _fetch_binance_midnight(symbol)
        change_utc_day = None
        if midnight_utc_price:
            change_utc_day = ((current - midnight_utc_price) / midnight_utc_price) * 100
        out[name] = {
            "price": current,
            "change_utc_day": change_utc_day,
            "change_24h_rolling": rolling_24h,
            "change_7d": None,  # Binance ticker doesn't expose 7d; previously CoinGecko-only
        }
    return out


def _fetch_finnhub_quote(symbol: str) -> dict | None:
    """Fetch price + pct change for a symbol via Finnhub /quote.

    Returns dict with 'price', 'prev_close', 'change_pct' or None on failure.
    Finnhub response fields: c=current, pc=prev close, dp=pct change.
    """
    from config import settings
    import time
    key = settings.finnhub_api_key
    if not key:
        return None
    url = f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(symbol)}&token={urllib.parse.quote(key)}"
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return None
    price = data.get("c")
    prev_close = data.get("pc")
    pct = data.get("dp")  # already in percent form
    if price is None or price == 0:
        return None
    return {"price": price, "prev_close": prev_close, "change_pct": pct}


def _fetch_traditional_markets() -> dict:
    """Fetch traditional market quotes via Finnhub, spaced to avoid bursts."""
    import time
    out = {}
    for ticker in _FINNHUB_TICKERS:
        data = _fetch_finnhub_quote(ticker)
        if data:
            out[ticker] = data
        time.sleep(0.15)  # Finnhub free tier = 60 calls/min; 0.15s is conservative
    return out


def _session_label(now_et: datetime) -> tuple[str, str]:
    """Classify the current NYSE session.

    Returns (short_code, explanatory_line) pair. short_code one of:
    - 'PRE-MARKET', 'OPEN', 'AFTER-HOURS', 'WEEKEND-CLOSED'
    """
    # NOTE: the session label applies to US equities/bonds/futures. Crypto trades
    # 24/7/365, so crypto % is ALWAYS a live current-day move regardless of session.
    WEEKEND_NOTE = (
        "Markets CLOSED (weekend). Traditional markets (stocks/bonds/oil/gold/DXY) "
        "haven't traded since Friday 4pm ET — the % changes below reflect Friday's full session. "
        "Do NOT describe traditional-market % as 'today's' moves. "
        "Crypto trades 24/7 and its % IS a live current-day move — describe crypto as 'today's' normally."
    )
    PRE_MARKET_NOTE = (
        "Markets PRE-OPEN (before 9:30 AM ET). US CASH EQUITIES haven't opened yet — "
        "the stock %s below reflect YESTERDAY'S full session, not today. Phrase stock levels "
        "as 'heading into today's open' or 'yesterday's close left $SPY up X%'. "
        "IMPORTANT: do NOT write 'crypto is the only thing trading' / 'the one thing actually "
        "moving' / 'the only market open' — that's WRONG. Equity-index futures, Treasury futures, "
        "oil futures, and FX all trade pre-market; only the US cash stock market is closed. "
        "Crypto trades 24/7 so its % IS a live today's-move (describe it as 'today's' normally), "
        "but it is NOT 'the only thing trading'. If crypto moved overnight, note it as a live "
        "data point alongside the yesterday's-close stock levels — not as 'the only live market'."
    )
    OPEN_NOTE = (
        "Markets currently OPEN (regular session). Traditional-market % reflects today's session-to-date move "
        "from yesterday's close. Crypto % reflects today's move since 00:00 UTC."
    )
    AFTER_HOURS_NOTE = (
        "Markets CLOSED — after-hours (post 4 PM ET). Traditional-market % reflects today's full regular session (final). "
        "Crypto trades 24/7 — its % reflects today's move since 00:00 UTC."
    )

    # Weekend
    if now_et.weekday() >= 5:  # Sat=5, Sun=6
        return ("WEEKEND-CLOSED", WEEKEND_NOTE)
    # Weekday
    hm = (now_et.hour, now_et.minute)
    if hm < (9, 30):
        return ("PRE-MARKET", PRE_MARKET_NOTE)
    if hm < (16, 0):
        return ("OPEN", OPEN_NOTE)
    return ("AFTER-HOURS", AFTER_HOURS_NOTE)


def fetch_market_snapshot() -> str:
    """Return a human-readable snapshot of current market levels for the prompt."""
    import pytz
    crypto = _fetch_crypto()
    traditional = _fetch_traditional_markets()

    et = pytz.timezone("America/New_York")
    now_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(et)
    ts = now_et.strftime("%Y-%m-%d %H:%M %Z")
    session_code, session_note = _session_label(now_et)
    lines = [f"CURRENT MARKET DATA (as of {ts}, session: {session_code}):", session_note, ""]

    # Label the % column based on session so Gemini doesn't call it "today's" when it isn't
    pct_label = {
        "OPEN": "session-to-date",
        "AFTER-HOURS": "today's session",
        "PRE-MARKET": "yesterday's session",
        "WEEKEND-CLOSED": "Friday's session",
    }.get(session_code, "last session")

    if traditional:
        lines.append(f"Traditional markets (% shown = {pct_label}):")
        for ticker, data in traditional.items():
            price = data.get("price")
            pct = data.get("change_pct")
            if price is None:
                continue
            pct_str = f"{pct:+.2f}%" if pct is not None else "n/a"
            lines.append(f"  ${ticker}: ${price:,.2f} ({pct_str} {pct_label})")
        lines.append("")

    if crypto:
        # Crypto daily % is anchored at 00:00 UTC = 8pm ET prior day (during EDT).
        # Matches the way crypto desks / Coinglass / daily candles compute "day change."
        lines.append("Crypto (% shown = since 00:00 UTC today, which is 8pm ET yesterday during EDT):")
        for name, data in crypto.items():
            price = data.get("price")
            if price is None:
                continue
            utc_day = data.get("change_utc_day")
            rolling_24h = data.get("change_24h_rolling")
            # Prefer UTC-day anchor; fall back to 24h rolling only if midnight fetch failed
            if utc_day is not None:
                pct_str = f"{utc_day:+.2f}% UTC-day"
            elif rolling_24h is not None:
                pct_str = f"{rolling_24h:+.2f}% (rolling 24h — UTC-day unavailable)"
            else:
                pct_str = "% unavailable"
            lines.append(f"  ${name}: ${price:,.0f} ({pct_str})")
        lines.append("")

    if not crypto and not traditional:
        lines.append("(Market data unavailable — using research-quoted numbers.)")

    return "\n".join(lines)
