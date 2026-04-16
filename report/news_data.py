"""Fetch live market news, earnings calendar, and economic calendar from Finnhub.

Uses Finnhub's free endpoints. If FINNHUB_API_KEY is not set, all fetchers
return placeholder strings and the synthesizer falls back to research-only context.

Sign up free at https://finnhub.io/register.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from config import settings

log = logging.getLogger(__name__)

# Earnings calendar ticker whitelist — only names that reliably move markets.
# Kept small so the calendar block doesn't tempt Gemini to list filler earnings.
# If research explicitly covers a ticker not in this list, the synthesis prompt
# allows including it from research context.
_MAJOR_TICKERS = {
    # MAG7
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NVDA", "TSLA",
    # Top banks (earnings season)
    "JPM", "GS", "MS", "BAC", "C", "WFC",
    # Select bellwethers (known to move the index or represent a sector)
    "NFLX", "TSM", "ASML", "BRK.B", "XOM", "WMT",
}


def _fetch_json(url: str, timeout: float = 8.0) -> list | dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"Finnhub fetch failed: {e}")
        return None


def fetch_news_snapshot(since_hours: int = 48, limit: int = 15) -> str:
    """Return a human-readable digest of recent market news for the prompt.

    Returns a string; if no key is set or fetch fails, returns a placeholder.
    """
    key = settings.finnhub_api_key
    if not key:
        return "LIVE NEWS: (no FINNHUB_API_KEY set — research-only mode)"

    # General market news
    url = f"https://finnhub.io/api/v1/news?category=general&token={urllib.parse.quote(key)}"
    data = _fetch_json(url)
    if not data or not isinstance(data, list):
        return "LIVE NEWS: (fetch failed, check key / network)"

    cutoff_ts = (datetime.utcnow() - timedelta(hours=since_hours)).timestamp()
    fresh = [n for n in data if n.get("datetime", 0) >= cutoff_ts]
    # Sort newest first
    fresh.sort(key=lambda n: n.get("datetime", 0), reverse=True)
    fresh = fresh[:limit]

    if not fresh:
        return f"LIVE NEWS (last {since_hours}h): no fresh stories from Finnhub."

    lines = [f"LIVE MARKET NEWS (last {since_hours}h from Finnhub, newest first):"]
    for n in fresh:
        ts = datetime.utcfromtimestamp(n.get("datetime", 0)).strftime("%Y-%m-%d %H:%M UTC")
        headline = n.get("headline", "").strip()
        source = n.get("source", "").strip()
        summary = (n.get("summary") or "").strip()[:200]
        lines.append(f"  [{ts}] {source}: {headline}")
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)


def fetch_earnings_calendar(days_ahead: int = 7) -> str:
    """Return a snapshot of major upcoming earnings (next N days) with BMO/AMC flags."""
    key = settings.finnhub_api_key
    if not key:
        return "EARNINGS CALENDAR: (no FINNHUB_API_KEY — dates/BMO-AMC not verified)"

    today = datetime.utcnow().date()
    end = today + timedelta(days=days_ahead)
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={today.isoformat()}&to={end.isoformat()}"
        f"&token={urllib.parse.quote(key)}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return "EARNINGS CALENDAR: (fetch failed)"

    items = data.get("earningsCalendar", []) or []
    # Filter to tickers we care about
    filtered = [
        e for e in items
        if e.get("symbol", "").upper() in _MAJOR_TICKERS
    ]
    # Sort by date
    filtered.sort(key=lambda e: (e.get("date", ""), e.get("symbol", "")))

    if not filtered:
        return f"EARNINGS CALENDAR (next {days_ahead}d): no major tickers reporting."

    today_date = datetime.utcnow().date()
    now_utc = datetime.utcnow()
    lines = [
        f"EARNINGS CALENDAR (next {days_ahead}d, major tickers only — USE THESE DATES + BMO/AMC VERBATIM):",
        "Each row tagged [REPORTED] if actuals present (use in RECAP), [TODAY-BMO/AMC] if scheduled for today, [UPCOMING] otherwise.",
    ]
    # Finnhub `hour` field: "bmo" = before market open, "amc" = after market close, "dmh" = during, "" = unknown
    for e in filtered:
        sym = e.get("symbol", "?")
        date_str = e.get("date", "?")
        hour = (e.get("hour") or "").lower()
        timing = {"bmo": "BMO", "amc": "AMC", "dmh": "intraday"}.get(hour, "timing TBD")
        eps_est = e.get("epsEstimate")
        rev_est = e.get("revenueEstimate")
        eps_actual = e.get("epsActual")
        rev_actual = e.get("revenueActual")

        # Determine release status
        try:
            ev_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            ev_date = None

        if eps_actual is not None or rev_actual is not None:
            status = "[REPORTED]"
        elif ev_date == today_date:
            # BMO on today = before ~9:30 AM ET = ~13:30 UTC. If past that, likely reported.
            if hour == "bmo" and now_utc.hour >= 14:
                status = "[REPORTED-BMO-today]"
            elif hour == "amc" and now_utc.hour >= 21:
                status = "[REPORTED-AMC-today]"
            else:
                status = f"[TODAY-{hour.upper()}]" if hour else "[TODAY]"
        elif ev_date and ev_date < today_date:
            status = "[PAST]"
        else:
            status = "[UPCOMING]"

        extra = []
        if eps_actual is not None:
            extra.append(f"EPS ACTUAL ${eps_actual}")
        if rev_actual is not None:
            extra.append(f"Rev ACTUAL ${rev_actual/1e9:.2f}B")
        if eps_est is not None and eps_actual is None:
            extra.append(f"EPS est ${eps_est}")
        if rev_est is not None and rev_actual is None:
            extra.append(f"Rev est ${rev_est/1e9:.2f}B")
        extra_str = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  {status} {date_str} {sym} — {timing}{extra_str}")
    return "\n".join(lines)


def fetch_economic_calendar(days_ahead: int = 7) -> str:
    """Return upcoming US + major economic releases with actual dates/times/estimates."""
    key = settings.finnhub_api_key
    if not key:
        return "ECONOMIC CALENDAR: (no FINNHUB_API_KEY — release dates/forecasts not verified)"

    today = datetime.utcnow().date()
    end = today + timedelta(days=days_ahead)
    url = (
        f"https://finnhub.io/api/v1/calendar/economic"
        f"?from={today.isoformat()}&to={end.isoformat()}"
        f"&token={urllib.parse.quote(key)}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return "ECONOMIC CALENDAR: (fetch failed)"

    items = data.get("economicCalendar", []) or []
    # Whitelist of event name substrings that actually move markets for a US
    # options/crypto trader. Anything else (regional Fed surveys, Fed governor
    # speeches that aren't Powell, minor US data, foreign macro without US
    # read-through) is filtered out so Gemini can't include filler in the pulse.
    TIER1_KEYWORDS = [
        # US macro — headline only
        "fomc", "fed chair", "powell",
        "cpi", "core cpi",
        "pce", "core pce",
        "nonfarm payroll", "employment situation", "unemployment rate",
        "gdp ",
        "retail sales",
        "ism manufacturing", "ism services", "ism non-manufacturing",
        "ppi ",  # PPI headline (user flagged it matters)
        # Major central bank policy meetings only
        "ecb rate decision", "ecb interest rate",
        "boj rate decision", "boj interest rate",
        "boe rate decision", "boe interest rate",
    ]

    def _is_tier1(evt: dict) -> bool:
        name = (evt.get("event") or "").lower()
        # Only US for most; ECB/BOJ/BOE rate decisions pass through from other countries
        country = evt.get("country", "")
        if country == "US":
            return any(kw in name for kw in TIER1_KEYWORDS)
        # Foreign only for top central bank rate decisions
        return any(kw in name for kw in ("ecb rate", "boj rate", "boe rate",
                                         "ecb interest", "boj interest", "boe interest"))

    filtered = [e for e in items if _is_tier1(e)]
    filtered.sort(key=lambda e: e.get("time", ""))

    if not filtered:
        return f"ECONOMIC CALENDAR (next {days_ahead}d): no high-impact releases."

    now_utc = datetime.utcnow()
    lines = [
        f"ECONOMIC CALENDAR (next {days_ahead}d, high/medium impact from US/EU/CN/JP/GB/DE — USE THESE DATES + FORECASTS VERBATIM):",
        "Each row tagged [RELEASED] if `actual` is set (event already happened, use actual value in RECAP), or [UPCOMING] if estimate-only.",
    ]
    for e in filtered[:30]:  # cap to avoid prompt bloat
        country = e.get("country", "")
        event = e.get("event", "").strip()
        time = e.get("time", "")[:16].replace("T", " ")
        impact = (e.get("impact") or "").lower()
        estimate = e.get("estimate")
        prev = e.get("prev")
        actual = e.get("actual")
        unit = e.get("unit", "")

        # Determine status — actual present OR scheduled time in the past = released
        status = "[UPCOMING]"
        if actual is not None:
            status = "[RELEASED]"
        else:
            try:
                sched = datetime.fromisoformat(e.get("time", "")[:19])
                if sched < now_utc:
                    status = "[PAST — no actual reported]"
            except (ValueError, TypeError):
                pass

        bits = [status, f"{time} UTC", f"[{country}]", event, f"impact={impact}"]
        if actual is not None:
            bits.append(f"ACTUAL={actual}{unit}")
        if estimate is not None:
            bits.append(f"est={estimate}{unit}")
        if prev is not None:
            bits.append(f"prev={prev}{unit}")
        lines.append("  " + " | ".join(bits))
    return "\n".join(lines)
