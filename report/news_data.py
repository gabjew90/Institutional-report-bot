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

import pytz

from config import settings

log = logging.getLogger(__name__)

_ET = pytz.timezone("America/New_York")


def _utc_to_et(utc_iso: str) -> str:
    """Convert a UTC ISO string 'YYYY-MM-DD HH:MM' (space or T) to ET display."""
    if not utc_iso:
        return ""
    try:
        # Accept either space or T separator, strip tz suffix if present
        clean = utc_iso.replace("T", " ")[:16]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M").replace(tzinfo=pytz.UTC)
        return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return utc_iso

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
        ts_utc = datetime.utcfromtimestamp(n.get("datetime", 0))
        ts = ts_utc.replace(tzinfo=pytz.UTC).astimezone(_ET).strftime("%Y-%m-%d %H:%M %Z")
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
    # Fetch from yesterday so we can catch AMC earnings that released last
    # night (e.g. NFLX Thu AMC checked on Fri → still relevant context).
    start = today - timedelta(days=1)
    end = today + timedelta(days=days_ahead)
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={start.isoformat()}&to={end.isoformat()}"
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
    reported_lines = []
    upcoming_lines = []
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

        already_reported = (
            eps_actual is not None
            or rev_actual is not None
            or (ev_date and ev_date < today_date)
            or (ev_date == today_date and hour == "bmo" and now_utc.hour >= 14)
            or (ev_date == today_date and hour == "amc" and now_utc.hour >= 21)
        )

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
        row = f"  {date_str} {sym} — {timing}{extra_str}"

        if already_reported:
            reported_lines.append(row)
        else:
            upcoming_lines.append(row)

    out = []
    if reported_lines:
        out.append("EARNINGS ALREADY REPORTED (belongs in RECAP, NEVER in WHAT TO WATCH):")
        out.extend(reported_lines)
    if upcoming_lines:
        if out:
            out.append("")
        out.append("EARNINGS STILL UPCOMING (belongs in WHAT TO WATCH):")
        out.extend(upcoming_lines)
    if not out:
        return f"EARNINGS CALENDAR: no major tickers in window."
    return "\n".join(out)


def fetch_economic_calendar_structured(
    query: str | None = None,
    days_window: int = 14,
) -> list[dict]:
    """Return Tier-1 economic events as structured rows for the /ask
    `lookup_economic_calendar` tool.

    Same Finnhub source + same Tier-1 whitelist as `fetch_economic_calendar`
    above — pulse and /ask end up reading from the same canonical data,
    so any answer the bot gives via /ask matches the pulse's numbers.
    This closes the 2026-06-05 NFP cross-source conflict (pulse said 120k
    ADP, /ask said 172k BLS via Google grounding) and the recurring
    macro-print fabrication family.

    Args:
      query: optional case-insensitive substring to match against the
             event name (e.g. "CPI", "NFP", "ECB", "May payrolls").
             When omitted, returns all Tier-1 events in the window.
      days_window: ±days from today to include (default 14 — covers
             "what's this week" and "what was last week's print").

    Returns: list[dict], one per event, with keys:
      - event              : str (Finnhub event name, e.g. "CPI YoY")
      - country            : str ("US" / "ECB" / "JP" / "GB")
      - scheduled_iso_utc  : str (ISO datetime, e.g. "2026-06-10T12:30:00")
      - scheduled_et_human : str (human ET, e.g. "06-10 08:30 ET")
      - impact             : str ("high" / "medium" / "low")
      - consensus          : float | None — Finnhub `estimate` field
      - prev               : float | None — last reading
      - actual             : float | None — released value, None if
                             still scheduled
      - unit               : str (e.g. "%", "K", "")
      - status             : "released" | "scheduled" | "past_no_data"

    Empty list on fetch failure or no matches. Caller must handle the
    empty case — do NOT treat it as "no event" since it could be a
    transient API issue.
    """
    key = settings.finnhub_api_key
    if not key:
        return []

    today = datetime.utcnow().date()
    start = today - timedelta(days=days_window)
    end = today + timedelta(days=days_window)
    url = (
        f"https://finnhub.io/api/v1/calendar/economic"
        f"?from={start.isoformat()}&to={end.isoformat()}"
        f"&token={urllib.parse.quote(key)}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return []

    items = data.get("economicCalendar", []) or []
    from world_context import FED_SPEAKER_KEYWORDS
    TIER1_KEYWORDS = [
        *FED_SPEAKER_KEYWORDS,
        "cpi", "core cpi",
        "pce", "core pce",
        # Both spellings: Finnhub uses "Non Farm Payrolls" (spaced) for the
        # high-impact headline BLS Establishment series; "Nonfarm Payrolls
        # Private" (compound) for the low-impact BLS Private subseries.
        # Without the spaced spelling, the headline event was being silently
        # dropped — caused the 2026-06-05 RECAP to report 120k Private
        # instead of 172k headline.
        "nonfarm payroll", "non farm payroll",
        "employment situation", "unemployment rate",
        "gdp ",
        "retail sales",
        "ism manufacturing", "ism services", "ism non-manufacturing",
        "ppi ",
        "ecb rate decision", "ecb interest rate",
        "boj rate decision", "boj interest rate",
        "boe rate decision", "boe interest rate",
    ]

    def _is_tier1(evt: dict) -> bool:
        name = (evt.get("event") or "").lower()
        country = evt.get("country", "")
        if country == "US":
            # Impact filter: drop low-impact subreleases (Government Payrolls,
            # Manufacturing Payrolls, U-6 Unemployment, Nonfarm Payrolls
            # PRIVATE) that share the 8:30 ET BLS window but aren't what
            # traders price off. Keeps high + medium impact only.
            impact = (evt.get("impact") or "").lower()
            if impact not in ("high", "medium"):
                return False
            return any(kw in name for kw in TIER1_KEYWORDS)
        return any(kw in name for kw in ("ecb rate", "boj rate", "boe rate",
                                          "ecb interest", "boj interest", "boe interest"))

    filtered = [e for e in items if _is_tier1(e)]

    # Apply user query filter on top of Tier-1 if provided.
    if query:
        q_lower = query.strip().lower()
        # Expand common trader abbreviations before tokenization — these
        # don't substring-match the spelled-out Finnhub event names.
        # Without this, /ask query="NFP" returns empty because "nfp"
        # doesn't appear in "Non Farm Payrolls".
        QUERY_ALIASES = {
            "nfp": "non farm payroll",
            "ppi": "ppi",
            "ism": "ism",
            "fomc": "fomc",
        }
        if q_lower in QUERY_ALIASES:
            q_lower = QUERY_ALIASES[q_lower]
        # Handle multi-word queries: split on space and require ALL tokens
        # to appear in the event name. "May payrolls" matches "Nonfarm
        # Payrolls" only if both "may"/"payroll" appear; relaxed to
        # any-token-match if strict-AND yields empty.
        q_tokens = [t for t in q_lower.split() if t]
        if q_tokens:
            strict = [
                e for e in filtered
                if all(tok in (e.get("event") or "").lower() for tok in q_tokens)
            ]
            relaxed = [
                e for e in filtered
                if any(tok in (e.get("event") or "").lower() for tok in q_tokens)
            ]
            filtered = strict if strict else relaxed

    filtered.sort(key=lambda e: e.get("time", ""))

    now_utc = datetime.utcnow()
    rows: list[dict] = []
    for e in filtered[:40]:
        sched_iso = (e.get("time") or "")[:19] or None
        actual = e.get("actual")

        status = "scheduled"
        if actual is not None:
            status = "released"
        elif sched_iso:
            try:
                sched_dt = datetime.fromisoformat(sched_iso)
                if sched_dt < now_utc:
                    status = "past_no_data"
            except (ValueError, TypeError):
                pass

        rows.append({
            "event": (e.get("event") or "").strip(),
            "country": e.get("country", ""),
            "scheduled_iso_utc": sched_iso,
            "scheduled_et_human": _utc_to_et(e.get("time", "")),
            "impact": (e.get("impact") or "").lower(),
            "consensus": e.get("estimate"),
            "prev": e.get("prev"),
            "actual": actual,
            "unit": e.get("unit", "") or "",
            "status": status,
        })
    return rows


def fetch_economic_calendar(days_ahead: int = 7) -> str:
    """Return upcoming US + major economic releases with actual dates/times/estimates."""
    key = settings.finnhub_api_key
    if not key:
        return "ECONOMIC CALENDAR: (no FINNHUB_API_KEY — release dates/forecasts not verified)"

    today = datetime.utcnow().date()
    # Fetch from yesterday to capture data released overnight that's still context
    start = today - timedelta(days=1)
    end = today + timedelta(days=days_ahead)
    url = (
        f"https://finnhub.io/api/v1/calendar/economic"
        f"?from={start.isoformat()}&to={end.isoformat()}"
        f"&token={urllib.parse.quote(key)}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return "ECONOMIC CALENDAR: (fetch failed)"

    items = data.get("economicCalendar", []) or []
    # Whitelist of event name substrings that actually move markets for a US
    # options/crypto trader. Anything else (regional Fed surveys, Fed governor
    # speeches that aren't the chair, minor US data, foreign macro without US
    # read-through) is filtered out so Gemini can't include filler in the pulse.
    # Fed-speaker keywords come from world_context.py — single source of truth
    # for the current chair name; when a new chair is confirmed, edit that
    # file and the filter updates without touching this one.
    from world_context import FED_SPEAKER_KEYWORDS
    TIER1_KEYWORDS = [
        # US macro — headline only (Fed-speaker subset from world_context)
        *FED_SPEAKER_KEYWORDS,
        "cpi", "core cpi",
        "pce", "core pce",
        # Both spellings: Finnhub uses "Non Farm Payrolls" (spaced) for the
        # high-impact headline BLS Establishment series; "Nonfarm Payrolls
        # Private" (compound) for the low-impact BLS Private subseries.
        # Without the spaced spelling, the headline event was silently
        # dropped — caused the 2026-06-05 RECAP to use 120k Private
        # instead of 172k headline.
        "nonfarm payroll", "non farm payroll",
        "employment situation", "unemployment rate",
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
            # Impact filter: drop low-impact subreleases (Government Payrolls,
            # Manufacturing Payrolls, U-6 Unemployment, Nonfarm Payrolls
            # PRIVATE) that share the 8:30 ET BLS window but aren't what
            # traders price off. Keeps high + medium impact only.
            impact = (evt.get("impact") or "").lower()
            if impact not in ("high", "medium"):
                return False
            return any(kw in name for kw in TIER1_KEYWORDS)
        # Foreign only for top central bank rate decisions
        return any(kw in name for kw in ("ecb rate", "boj rate", "boe rate",
                                         "ecb interest", "boj interest", "boe interest"))

    filtered = [e for e in items if _is_tier1(e)]
    filtered.sort(key=lambda e: e.get("time", ""))

    if not filtered:
        return f"ECONOMIC CALENDAR (next {days_ahead}d): no high-impact releases."

    now_utc = datetime.utcnow()
    released_lines = []
    upcoming_lines = []
    for e in filtered[:40]:
        country = e.get("country", "")
        event = e.get("event", "").strip()
        time = _utc_to_et(e.get("time", ""))
        impact = (e.get("impact") or "").lower()
        estimate = e.get("estimate")
        prev = e.get("prev")
        actual = e.get("actual")
        unit = e.get("unit", "")

        is_released = actual is not None
        if not is_released:
            try:
                sched = datetime.fromisoformat(e.get("time", "")[:19])
                if sched < now_utc:
                    is_released = True  # past scheduled time without actual — treat as released (even if no data yet)
            except (ValueError, TypeError):
                pass

        bits = [time, f"[{country}]", event, f"impact={impact}"]
        if actual is not None:
            bits.append(f"ACTUAL={actual}{unit}")
        if estimate is not None:
            bits.append(f"est={estimate}{unit}")
        if prev is not None:
            bits.append(f"prev={prev}{unit}")
        row = "  " + " | ".join(bits)

        if is_released:
            released_lines.append(row)
        else:
            upcoming_lines.append(row)

    out = []
    if released_lines:
        out.append("ECONOMIC EVENTS ALREADY RELEASED (belongs in RECAP, NEVER in WHAT TO WATCH):")
        out.extend(released_lines)
    if upcoming_lines:
        if out:
            out.append("")
        out.append("ECONOMIC EVENTS STILL UPCOMING (belongs in WHAT TO WATCH):")
        out.extend(upcoming_lines)
    if not out:
        return "ECONOMIC CALENDAR: no high-impact releases in window."
    return "\n".join(out)
