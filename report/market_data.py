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
import urllib.parse
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


def _fetch_yahoo_extended_hours(symbol: str) -> dict | None:
    """Fetch the most recent print + session classification from Yahoo
    chart endpoint, including extended-hours bars.

    The v8 chart endpoint with `includePrePost=true` returns minute-level
    bars across pre + regular + post sessions. We walk the close array
    from the end backwards to find the most recent non-null close, then
    use `tradingPeriods` to classify which session that bar belongs to.

    Used as a richer alternative to Finnhub /quote when the session is
    AFTER-HOURS or PRE-MARKET — Finnhub /quote does NOT return
    extended-hours prints. Falls back to None on any failure (Yahoo
    rate-limits datacenter IPs intermittently); caller should fall
    back to Finnhub.

    Returns dict with:
      - last_price       : most recent print
      - last_timestamp   : unix seconds for that bar
      - last_session     : 'pre' | 'regular' | 'post'
      - regular_close    : regularMarketPrice (today's 4 PM close, or
                           in-progress if currently OPEN)
      - prev_close       : previousClose (yesterday's regular close)
      - source           : 'yahoo'

    Or None on fetch/parse failure.
    """
    # Spoofed UA — Yahoo returns 401/403 to obvious bot user agents
    # but happily serves Mozilla/Chrome strings.
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1m&range=1d&includePrePost=true"
    )
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/javascript,*/*;q=0.01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        log.info(f"yahoo AH fetch for {symbol} failed: {e}")
        return None

    try:
        result = data["chart"]["result"][0]
        meta = result["meta"]
        timestamps = result.get("timestamp") or []
        closes = (result.get("indicators", {}).get("quote", [{}])[0]
                  .get("close") or [])
    except (KeyError, IndexError, TypeError) as e:
        log.info(f"yahoo AH parse for {symbol} failed: {e}")
        return None

    if not timestamps or not closes or len(timestamps) != len(closes):
        return None

    # Walk backwards to find the last non-null bar.
    last_price = None
    last_timestamp = None
    for ts, c in zip(reversed(timestamps), reversed(closes)):
        if c is not None:
            last_price = float(c)
            last_timestamp = int(ts)
            break
    if last_price is None or last_timestamp is None:
        return None

    # Classify which session that bar belongs to using tradingPeriods.
    # Shape: {"pre": [[{start, end}]], "regular": [[...]], "post": [[...]]}
    last_session = "regular"
    try:
        periods = meta.get("tradingPeriods", {}) or {}
        for sess_key in ("pre", "regular", "post"):
            blocks = periods.get(sess_key) or []
            # Yahoo nests one level (list-of-lists)
            for block in blocks:
                if isinstance(block, list):
                    for p in block:
                        s = p.get("start")
                        e = p.get("end")
                        if s and e and s <= last_timestamp < e:
                            last_session = sess_key
                            break
                elif isinstance(block, dict):
                    s = block.get("start")
                    e = block.get("end")
                    if s and e and s <= last_timestamp < e:
                        last_session = sess_key
                        break
    except Exception:
        pass  # fall through with default 'regular'

    return {
        "last_price": last_price,
        "last_timestamp": last_timestamp,
        "last_session": last_session,
        "regular_close": meta.get("regularMarketPrice"),
        "prev_close": meta.get("previousClose"),
        "source": "yahoo",
    }


def _fetch_yahoo_options_chain(
    symbol: str,
    expiration_iso: str | None = None,
) -> dict | None:
    """Fetch the options chain for `symbol` via yfinance.

    Yahoo's v7/v8 endpoints introduced a session-crumb (CSRF) gate
    around 2024; raw urllib calls hit HTTP 401 from datacenter IPs.
    yfinance handles the crumb-session dance internally so we don't
    have to replicate it.

    Without `expiration_iso`, returns the chain for the nearest
    expiration plus the full list of available expirations (so the
    caller can re-call for a further-out one). With `expiration_iso`,
    returns that specific expiration's chain.

    Returns dict with:
      - underlying_symbol     : "SPY"
      - underlying_spot_price : current spot from yf info
      - expiration_dates      : list[str] (ISO YYYY-MM-DD, sorted)
      - chain                 : {"expiration_iso", "calls": [...], "puts": [...]}
        where each option is a dict (keys: strike, openInterest,
        volume, impliedVolatility, bid, ask, lastPrice, contractSymbol)
      - source                : "yahoo"

    Or None on fetch/parse failure (yfinance is generally reliable
    but can still hit Yahoo rate limits intermittently).
    """
    try:
        import yfinance as yf
    except ImportError as e:
        log.warning(f"yfinance not installed: {e}")
        return None

    try:
        ticker = yf.Ticker(symbol)
        expirations = list(ticker.options or [])
        if not expirations:
            return {
                "underlying_symbol": symbol.upper(),
                "underlying_spot_price": None,
                "expiration_dates": [],
                "chain": {"expiration_iso": None, "calls": [], "puts": []},
                "source": "yahoo",
            }

        target_iso = expiration_iso if expiration_iso in expirations else expirations[0]
        chain_obj = ticker.option_chain(target_iso)

        # Underlying spot — try yf's fast_info (faster, less rate-limited)
        # then fall back to .info. Both can fail under heavy yahoo load;
        # spot may end up None and ATM IV becomes None downstream — but
        # call/put volume + OI + put-call ratios still work without spot.
        spot = None
        try:
            spot = float(ticker.fast_info["last_price"])
        except Exception:
            try:
                spot = float(ticker.info.get("regularMarketPrice") or 0) or None
            except Exception:
                pass

        def _df_to_dicts(df):
            """Convert yfinance options DataFrame to list[dict] of just
            the fields we need. yfinance columns: strike, lastPrice, bid,
            ask, change, percentChange, volume, openInterest,
            impliedVolatility, inTheMoney, contractSize, currency."""
            if df is None or df.empty:
                return []
            out = []
            for _, row in df.iterrows():
                out.append({
                    "strike": float(row["strike"]),
                    "openInterest": (
                        int(row["openInterest"])
                        if row["openInterest"] == row["openInterest"]  # NaN check
                        else 0
                    ),
                    "volume": (
                        int(row["volume"])
                        if row["volume"] == row["volume"]
                        else 0
                    ),
                    "impliedVolatility": (
                        float(row["impliedVolatility"])
                        if row["impliedVolatility"] == row["impliedVolatility"]
                        else None
                    ),
                    "bid": (
                        float(row["bid"])
                        if row["bid"] == row["bid"] else None
                    ),
                    "ask": (
                        float(row["ask"])
                        if row["ask"] == row["ask"] else None
                    ),
                    "lastPrice": (
                        float(row["lastPrice"])
                        if row["lastPrice"] == row["lastPrice"] else None
                    ),
                })
            return out

        chain = {
            "expiration_iso": target_iso,
            "calls": _df_to_dicts(chain_obj.calls),
            "puts": _df_to_dicts(chain_obj.puts),
        }
        return {
            "underlying_symbol": symbol.upper(),
            "underlying_spot_price": spot,
            "expiration_dates": expirations,
            "chain": chain,
            "source": "yahoo",
        }
    except Exception as e:
        log.info(f"yfinance options-chain fetch for {symbol} failed: {e}")
        return None


def summarize_options_chain(raw: dict) -> dict:
    """Aggregate a single-expiration chain into LLM-ready summary stats.

    Takes the raw fetch result from `_fetch_yahoo_options_chain` and
    collapses the per-strike call/put lists into:
      - call_volume / put_volume   : sum across all strikes
      - call_oi / put_oi           : sum across all strikes
      - put_call_volume_ratio      : put_vol / call_vol
      - put_call_oi_ratio          : put_oi / call_oi
      - atm_iv                     : IV of the strike NEAREST to spot
                                     (averaged across the call + put at
                                     that strike when both exist)
      - num_call_strikes / num_put_strikes
      - expiration_iso             : 'YYYY-MM-DD'
      - underlying_spot_price      : passthrough

    Returns ratios rounded to 3 decimals, IV to 4 decimals. ATM IV is
    None if the chain has no strikes or no IV values.
    """
    chain = raw.get("chain") or {}
    calls = chain.get("calls") or []
    puts = chain.get("puts") or []
    spot = raw.get("underlying_spot_price")

    def _sum(items, key):
        return sum(int(it.get(key) or 0) for it in items)

    call_vol = _sum(calls, "volume")
    put_vol = _sum(puts, "volume")
    call_oi = _sum(calls, "openInterest")
    put_oi = _sum(puts, "openInterest")

    atm_iv = None
    if spot and (calls or puts):
        all_strikes = (
            [(c.get("strike"), c.get("impliedVolatility"), "call") for c in calls]
            + [(p.get("strike"), p.get("impliedVolatility"), "put") for p in puts]
        )
        all_strikes = [
            (s, iv) for s, iv, _ in all_strikes if s is not None and iv is not None
        ]
        if all_strikes:
            # Find the strike(s) closest to spot, then average IV of call + put
            min_dist = min(abs(s - spot) for s, _ in all_strikes)
            atm_ivs = [iv for s, iv in all_strikes if abs(s - spot) == min_dist]
            if atm_ivs:
                atm_iv = round(sum(atm_ivs) / len(atm_ivs), 4)

    exp_iso = chain.get("expiration_iso")

    return {
        "underlying_symbol": raw.get("underlying_symbol"),
        "underlying_spot_price": spot,
        "expiration_iso": exp_iso,
        "call_volume": call_vol,
        "put_volume": put_vol,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "put_call_volume_ratio": (
            round(put_vol / call_vol, 3) if call_vol else None
        ),
        "put_call_oi_ratio": (
            round(put_oi / call_oi, 3) if call_oi else None
        ),
        "atm_iv": atm_iv,
        "num_call_strikes": len(calls),
        "num_put_strikes": len(puts),
        "source": raw.get("source", "yahoo"),
    }


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


# Yahoo yield-index symbols. NOTE: although the old CBOE convention
# quoted these indices at yield × 10, Yahoo's v8 chart endpoint returns
# the ACTUAL yield directly (^TNX = 4.53 means the 10Y yields 4.53%).
# Verified live on prod 2026-06-09: raw values 4.25 / 4.53 / 5.01
# matched the pulse-narrated levels exactly ("30Y briefly breached 5%").
# An initial /10 scaling based on the CBOE convention produced 0.453%
# — don't reintroduce it. 2Y has no reliable Yahoo index symbol —
# omitted rather than proxied.
_YIELD_SYMBOLS = {
    "^FVX": "5Y",
    "^TNX": "10Y",
    "^TYX": "30Y",
}


def _fetch_treasury_yields() -> dict[str, dict]:
    """Fetch live US Treasury yields via Yahoo's yield indices.

    Added 2026-06-09 — end-to-end review found the pulse discusses
    Treasury yields and curve shape constantly (research quotes them in
    nearly every macro piece) but the live snapshot only carried $TLT,
    a long-duration ETF whose price is a noisy proxy. Traders reading
    "the 30Y broke 5%" in INSIGHTS could not verify it against the
    snapshot. This gives RECAP/AUDIT the actual yield levels.

    Returns {tenor: {"yield_pct": float, "change_bps": float|None}}
    e.g. {"10Y": {"yield_pct": 4.44, "change_bps": +3.2}}.
    Empty dict on total failure — caller renders nothing rather than
    stale numbers.
    """
    out: dict[str, dict] = {}
    for symbol, tenor in _YIELD_SYMBOLS.items():
        try:
            yh = _fetch_yahoo_extended_hours(symbol)
        except Exception as e:
            log.info(f"treasury yield fetch {symbol} failed: {e}")
            continue
        if not yh or yh.get("last_price") is None:
            continue
        # Yahoo's v8 chart endpoint returns the actual yield (4.53 =
        # 4.53%) — no scaling. See _YIELD_SYMBOLS comment.
        yield_pct = round(float(yh["last_price"]), 3)
        change_bps = None
        prev = yh.get("prev_close")
        if prev:
            # Δ percentage points × 100 = bps (4.53 → 4.55 = +2 bps)
            change_bps = round((float(yh["last_price"]) - float(prev)) * 100.0, 1)
        # Sanity bound: US Treasury yields live in (0, 20). A value
        # outside that band means the quoting convention changed (e.g.,
        # Yahoo reverting to CBOE ×10) — drop rather than publish a
        # wrong-scale number into the pulse.
        if not (0.0 < yield_pct < 20.0):
            log.warning(
                f"treasury yield {tenor} out of sane range "
                f"({yield_pct}) — dropping; check quoting convention"
            )
            continue
        out[tenor] = {"yield_pct": yield_pct, "change_bps": change_bps}
    return out


def fetch_market_snapshot() -> str:
    """Return a human-readable snapshot of current market levels for the prompt."""
    import pytz
    crypto = _fetch_crypto()
    traditional = _fetch_traditional_markets()
    yields = _fetch_treasury_yields()

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

    if yields:
        # Real yield levels, not the $TLT proxy. Research quotes yields
        # constantly; this lets RECAP/AUDIT ground them in live numbers.
        lines.append("US Treasury yields (live via Yahoo yield indices; change = vs prior close):")
        for tenor in ("5Y", "10Y", "30Y"):
            data = yields.get(tenor)
            if not data:
                continue
            chg = data.get("change_bps")
            chg_str = f" ({chg:+.1f} bps)" if chg is not None else ""
            lines.append(f"  {tenor}: {data['yield_pct']:.2f}%{chg_str}")
        # Curve spread when both legs present — the steepness read the
        # research references as "the curve" without a number.
        if "10Y" in yields and "30Y" in yields:
            spread = round(
                (yields["30Y"]["yield_pct"] - yields["10Y"]["yield_pct"]) * 100
            )
            lines.append(f"  30Y-10Y spread: {spread:+d} bps")
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
