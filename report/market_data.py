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

# Finnhub uses liquid US-listed ETF proxies for the indexes/futures we want.
# Free tier fully supports these; they track their underlying % moves ~1:1.
# Labels shown to Gemini keep the underlying name so the pulse reads naturally.
_FINNHUB_PROXIES = [
    # (display_name, finnhub_ticker, note)
    ("S&P 500", "SPY", "SPDR S&P 500 ETF; tracks $SPX"),
    ("Nasdaq 100", "QQQ", "Invesco QQQ; tracks $NDX"),
    ("VIX", "VIXY", "ProShares VIX Short-Term Futures ETF; tracks VIX"),
    ("Brent Crude", "BNO", "United States Brent Oil ETF"),
    ("WTI Crude", "USO", "United States Oil Fund ETF"),
    ("Gold", "GLD", "SPDR Gold Shares"),
    ("10Y Treasury (inverse)", "TLT", "iShares 20+ Year Treasury Bond ETF — yields INVERSELY; if TLT up, 10Y yield down"),
    ("DXY", "UUP", "Invesco DB US Dollar Bullish ETF"),
]


def _fetch_json(url: str, timeout: float = 5.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"Market data fetch failed for {url[:60]}: {e}")
        return None


def _fetch_crypto_midnight_utc(coin_id: str) -> float | None:
    """Get the coin price at most-recent 00:00 UTC via CoinGecko market_chart.

    Returns the single price point closest to (but <=) today's 00:00 UTC in USD.
    """
    # Pull last 2 days of hourly data; find the point nearest 00:00 UTC today.
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
        f"?vs_currency=usd&days=2"
    )
    data = _fetch_json(url)
    if not data:
        return None
    prices = data.get("prices") or []  # list of [timestamp_ms, price_usd]
    if not prices:
        return None
    from datetime import datetime, timezone
    today_midnight_utc_ms = int(
        datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )
    # Find the closest-to-but-not-after-midnight-UTC price
    best = None
    best_diff = float("inf")
    for ts_ms, price in prices:
        diff = today_midnight_utc_ms - ts_ms
        if 0 <= diff < best_diff:
            best_diff = diff
            best = price
    return best


def _fetch_crypto() -> dict:
    import time
    data = _fetch_json(_COINGECKO_URL)
    if not data:
        return {}
    out = {}
    for key, name in [("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")]:
        if key in data:
            current = data[key].get("usd")
            # Small delay between market_chart calls to stay under CoinGecko free-tier RPM
            time.sleep(0.7)
            midnight_utc_price = _fetch_crypto_midnight_utc(key) if current is not None else None
            # Day-to-date change anchored at 00:00 UTC today (= 8pm ET yesterday during EDT)
            change_utc_day = None
            if current is not None and midnight_utc_price:
                change_utc_day = ((current - midnight_utc_price) / midnight_utc_price) * 100
            out[name] = {
                "price": current,
                "change_utc_day": change_utc_day,
                "change_7d": data[key].get("usd_7d_change"),
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
    """Fetch all traditional market proxies via Finnhub, spaced to avoid bursts."""
    import time
    out = {}
    for label, ticker, _note in _FINNHUB_PROXIES:
        data = _fetch_finnhub_quote(ticker)
        if data:
            # Keep the underlying label but include the proxy ticker in the output
            # so Gemini has context (e.g., can write "$SPY +2.39%" if it prefers).
            data["proxy_ticker"] = ticker
            out[label] = data
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
        "Markets PRE-OPEN (before 9:30 AM ET). Traditional markets haven't traded today yet — "
        "the % changes reflect YESTERDAY'S full session. Do NOT describe traditional-market % as 'today's moves'. "
        "Phrase as 'heading into today's open' or 'yesterday's close left SPX up X%'. "
        "Crypto trades 24/7 and its % IS a live current-day move — describe crypto as 'today's' normally."
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
        lines.append(
            f"Traditional markets (% shown = {pct_label}; "
            "quotes via liquid US-listed ETF proxies on Finnhub — "
            "$SPY for S&P, $QQQ for Nasdaq 100, $VIXY for VIX, $GLD for Gold, "
            "$BNO for Brent, $USO for WTI, $TLT for 10Y (inverse), $UUP for DXY):"
        )
        for name, data in traditional.items():
            price = data.get("price")
            pct = data.get("change_pct")
            proxy = data.get("proxy_ticker", "")
            if price is None:
                continue
            pct_str = f"{pct:+.2f}%" if pct is not None else "n/a"
            proxy_str = f" [{proxy}]" if proxy else ""
            lines.append(f"  {name}{proxy_str}: ${price:,.2f} ({pct_str} {pct_label})")
        lines.append("")

    if crypto:
        # Crypto daily % is anchored at 00:00 UTC = 8pm ET prior day (during EDT).
        # Matches the way crypto desks / Coinglass / daily candles compute "day change."
        lines.append("Crypto (% shown = since 00:00 UTC today, which is 8pm ET yesterday during EDT):")
        for name, data in crypto.items():
            price = data.get("price")
            utc_day = data.get("change_utc_day")
            c7d = data.get("change_7d")
            if price is None:
                continue
            day_str = f"{utc_day:+.2f}%" if utc_day is not None else "n/a"
            c7d_str = f"{c7d:+.2f}%" if c7d is not None else "n/a"
            lines.append(f"  {name}: ${price:,.0f} ({day_str} UTC-day, {c7d_str} 7d)")
        lines.append("")

    if not crypto and not traditional:
        lines.append("(Market data unavailable — using research-quoted numbers.)")

    return "\n".join(lines)
