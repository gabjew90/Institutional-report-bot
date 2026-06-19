"""Fetch live market news, earnings calendar, and economic calendar from Finnhub.

Uses Finnhub's free endpoints. If FINNHUB_API_KEY is not set, all fetchers
return placeholder strings and the synthesizer falls back to research-only context.

Sign up free at https://finnhub.io/register.
"""

import json
import logging
import urllib.error
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
    # High-IV options movers (added 2026-06-09 — end-to-end review found
    # the 20-ticker whitelist too narrow for an options-trader audience.
    # These names carry 20-30% earnings IV and large options volume; the
    # room actively trades them. Without them, research mentions of e.g.
    # CRWD leak into INSIGHTS while the structured earnings calendar
    # stays silent on the date.)
    "AMD", "AVGO", "QCOM", "MU", "SMCI", "ARM", "MRVL",     # semis beyond NVDA/TSM
    "CRWD", "PANW", "PLTR", "SNOW", "NET", "DDOG",          # high-IV software/security
    "CRM", "ADBE", "ORCL", "NOW", "INTU",                   # enterprise software
    "COIN", "HOOD", "MSTR", "MARA",                         # crypto-adjacent equities
    "LLY", "UNH",                                            # healthcare index movers
    "COST", "HD", "TGT",                                     # consumer bellwethers
    "DELL", "UBER", "SHOP", "ROKU",                          # other high-IV movers
}


# HTTP status of the most recent _fetch_json failure (None on success
# or non-HTTP errors). Lets callers distinguish a 403 entitlement block
# (back off — it won't fix itself this hour) from transient errors
# (retry next cycle). Module-global because _fetch_json's None-on-error
# contract is baked into every fetcher + smoke; widening the return
# type would churn all of them.
_LAST_HTTP_ERROR_CODE: int | None = None


def _fetch_json(url: str, timeout: float = 8.0) -> list | dict | None:
    global _LAST_HTTP_ERROR_CODE
    _LAST_HTTP_ERROR_CODE = None
    req = urllib.request.Request(url, headers={"User-Agent": "MarketPulseBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        _LAST_HTTP_ERROR_CODE = e.code
        log.warning(f"Finnhub fetch failed: {e}")
        return None
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

        # Sanity guard: Finnhub's free-tier earnings estimates are
        # sometimes garbage (2026-06-18: MU came back EPS est $20.69 /
        # Rev est $35.88B — ~10x/4x too high for Micron, relayed verbatim
        # into the pulse). No name in our whitelist posts a single-quarter
        # EPS above ~15 or quarterly revenue above ~$200B, so values past
        # those bounds are bad data — drop the estimate rather than ship a
        # wrong number a reader trades off.
        def _bad_eps(v):
            try:
                return v is not None and abs(float(v)) > 15
            except (TypeError, ValueError):
                return True

        def _bad_rev(v):
            try:
                return v is not None and not (0 < float(v) / 1e9 <= 200)
            except (TypeError, ValueError):
                return True

        # If EITHER number on a row is implausible, the whole Finnhub row
        # is suspect — drop all of its figures rather than ship the
        # "good-looking" half of a bad row (MU 06-18: EPS $20.69 was
        # absurd AND rev $35.88B was wrong-for-Micron; dropping only the
        # EPS would still have shipped the bad revenue).
        row_suspect = (
            _bad_eps(eps_actual) or _bad_rev(rev_actual)
            or _bad_eps(eps_est) or _bad_rev(rev_est)
        )
        if row_suspect:
            log.warning(
                f"earnings calendar: implausible figures for {sym} "
                f"(eps_est={eps_est}, rev_est={rev_est}, eps_act={eps_actual}, "
                f"rev_act={rev_actual}) — dropping estimates (bad Finnhub data)"
            )

        extra = []
        if not row_suspect:
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


def fetch_earnings_date_for_symbol(symbol: str) -> dict | None:
    """Earnings dates for ONE symbol via Finnhub's earnings calendar.

    Added 2026-06-10 for the /ask `lookup_earnings_date` tool — the
    pulse's earnings block is whitelist-filtered (noise control), so
    /ask had NO source for "when does GEO report next" on non-whitelist
    tickers and the model dodged the question with adjacent facts.
    A user naming a specific ticker IS the filter; no whitelist here.

    Queries a -30d…+120d window so both the NEXT upcoming report and
    the LAST reported one come back. Returns:
      {"symbol", "next": {date, hour, eps_estimate, revenue_estimate} | None,
       "last": {date, hour, eps_actual, eps_estimate} | None}
    or None on fetch failure (caller distinguishes failure from
    no-data so the model can fall back to Google).
    """
    key = settings.finnhub_api_key
    if not key or not symbol:
        return None
    sym = symbol.strip().upper()
    today = datetime.utcnow().date()
    start = today - timedelta(days=30)
    end = today + timedelta(days=120)
    url = (
        f"https://finnhub.io/api/v1/calendar/earnings"
        f"?from={start.isoformat()}&to={end.isoformat()}"
        f"&symbol={urllib.parse.quote(sym)}"
        f"&token={urllib.parse.quote(key)}"
    )
    data = _fetch_json(url)
    if not data or not isinstance(data, dict):
        return None
    items = [e for e in (data.get("earningsCalendar") or [])
             if (e.get("symbol") or "").upper() == sym]
    items.sort(key=lambda e: e.get("date", ""))

    today_iso = today.isoformat()
    upcoming = [e for e in items
                if (e.get("date") or "") >= today_iso
                and e.get("epsActual") is None]
    reported = [e for e in items
                if (e.get("date") or "") < today_iso
                or e.get("epsActual") is not None]

    def _fmt_next(e: dict) -> dict:
        hour = (e.get("hour") or "").lower()
        return {
            "date": e.get("date"),
            "timing": {"bmo": "before market open",
                       "amc": "after market close",
                       "dmh": "during market hours"}.get(hour, "timing TBD"),
            "eps_estimate": e.get("epsEstimate"),
            "revenue_estimate": e.get("revenueEstimate"),
        }

    def _fmt_last(e: dict) -> dict:
        return {
            "date": e.get("date"),
            "eps_actual": e.get("epsActual"),
            "eps_estimate": e.get("epsEstimate"),
        }

    return {
        "symbol": sym,
        "next": _fmt_next(upcoming[0]) if upcoming else None,
        "last": _fmt_last(reported[-1]) if reported else None,
    }


# ─── Economic calendar source layer ──────────────────────────────────
# 2026-06-11: Finnhub's /calendar/economic endpoint started returning
# 403 ("You don't have access to this resource") — the endpoint moved
# behind a paid entitlement while earnings + news endpoints stayed on
# the free tier. That morning's pulse shipped with "(fetch failed)" and
# mis-dated the ECB decision to "next week" when it was THE SAME DAY.
# Fix: ForexFactory's public weekly calendar JSON as a fallback source.
# Finnhub is still tried first so access self-heals if the entitlement
# comes back. FF caveats vs Finnhub:
#   - this-week coverage only (Sun–Sat; lastweek/nextweek feeds 404)
#   - NO released ACTUAL values (schedule + forecast + previous only)
# Both consumers (pulse text block + /ask structured tool) read through
# this layer, so the cross-product consistency guarantee survives the
# source switch.

_FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# FF titles → Finnhub-style names the existing Tier-1 keyword whitelist
# already matches. Only renames where FF's name would otherwise slip
# through the filter; CPI/PPI/GDP/retail-sales/ISM titles match as-is.
_FF_TITLE_RENAMES = {
    "non-farm employment change": "Non Farm Payrolls",
    "main refinancing rate": "ECB Interest Rate Decision",
    "official bank rate": "BoE Interest Rate Decision",
    "boj policy rate": "BoJ Interest Rate Decision",
    "federal funds rate": "FOMC Interest Rate Decision",
}

_FF_COUNTRY_MAP = {"USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP"}


class EconomicCalendarUnavailable(RuntimeError):
    """Both Finnhub and the ForexFactory fallback failed. Callers must
    surface 'feed down', NOT 'no events found' — during the 2026-06-11
    outage the empty-list contract made /ask claim no Tier-1 events
    existed, which reads as a fact about the calendar instead of a fact
    about the feed."""


def _parse_ff_value(raw) -> tuple[float | None, str]:
    """Parse FF string values like '0.3%', '220K', '46.1', '<0.5%'.

    Returns (number, unit_suffix); (None, '') when empty/unparseable.
    """
    import re as _re
    s = str(raw or "").strip().lstrip("<>").strip()
    m = _re.match(r"^(-?\d+(?:\.\d+)?)\s*([%KMBT]?)$", s)
    if not m:
        return None, ""
    return float(m.group(1)), m.group(2)


# FF rate-limits burst requests (observed 2026-06-11: third hit within
# ~2s returned 429). It's a weekly file — cache the normalized rows for
# 10 minutes, and serve stale-on-error for up to 6h so a transient 429
# never escalates to "ALL calendar sources failed".
_FF_CACHE: dict = {"at": None, "rows": None}
_FF_CACHE_TTL_S = 600
_FF_CACHE_STALE_OK_S = 6 * 3600


def _fetch_ff_economic_events() -> list[dict]:
    """Fetch ForexFactory's weekly calendar, normalized to the Finnhub
    economicCalendar event shape so downstream filtering/formatting is
    source-agnostic. Cached 10 min (stale-tolerated 6h on fetch
    failure). Empty list when no data and no usable cache."""
    now = datetime.utcnow()
    cached_at, cached_rows = _FF_CACHE["at"], _FF_CACHE["rows"]
    if cached_rows is not None and cached_at is not None:
        if (now - cached_at).total_seconds() < _FF_CACHE_TTL_S:
            return cached_rows
    data = _fetch_json(_FF_CALENDAR_URL)
    if not data or not isinstance(data, list):
        if cached_rows is not None and cached_at is not None and \
                (now - cached_at).total_seconds() < _FF_CACHE_STALE_OK_S:
            log.warning(
                "ForexFactory fetch failed — serving cached calendar "
                f"rows from {cached_at:%H:%M} UTC"
            )
            return cached_rows
        return []
    out: list[dict] = []
    for e in data:
        if not isinstance(e, dict):
            continue
        title = (e.get("title") or "").strip()
        if not title:
            continue
        event = _FF_TITLE_RENAMES.get(title.lower(), title)
        raw_country = (e.get("country") or "").strip()
        country = _FF_COUNTRY_MAP.get(raw_country, raw_country)
        # FF date carries a UTC offset ("2026-06-11T08:30:00-04:00");
        # Finnhub's `time` is naive UTC ("2026-06-11 12:30:00"-ish).
        time_utc = ""
        try:
            dt = datetime.fromisoformat(e.get("date") or "")
            time_utc = dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            pass
        estimate, unit_a = _parse_ff_value(e.get("forecast"))
        prev, unit_b = _parse_ff_value(e.get("previous"))
        impact = (e.get("impact") or "").strip().lower()
        if impact not in ("high", "medium", "low"):
            impact = "low"  # "Holiday" and other non-ratings
        out.append({
            "event": event,
            "country": country,
            "time": time_utc,
            "impact": impact,
            "estimate": estimate,
            "prev": prev,
            "actual": None,  # FF feed carries no released actuals
            "unit": unit_a or unit_b,
            "source": "forexfactory",
        })
    _FF_CACHE["at"] = now
    _FF_CACHE["rows"] = out
    return out


# Circuit breaker for Finnhub's /calendar/economic 403. A 403 is an
# entitlement block (endpoint moved behind a paid tier 2026-06-11) —
# it will not clear in the next 15-minute cycle, so probing every call
# just burns a doomed request and a warning line per dump. After a 403
# we skip Finnhub for 12h, then probe again — so if the entitlement
# ever comes back, the richer source (with actuals) resumes within a
# day with zero code changes. Transient failures (429/5xx/timeouts) do
# NOT trip the breaker; they retry on the next call as before.
_FINNHUB_ECON_BLOCK: dict = {"until": None}
_FINNHUB_ECON_BLOCK_HOURS = 12


def _fetch_economic_events_raw(start, end) -> tuple[list[dict], str]:
    """Fetch raw economic events for [start, end] (date objects).

    Finnhub first (full window, has actuals); ForexFactory this-week
    fallback when Finnhub is inaccessible or no key is set. Returns
    (events, source_label). Raises EconomicCalendarUnavailable when
    BOTH sources fail — callers must distinguish feed-down from
    legitimately-empty.
    """
    key = settings.finnhub_api_key
    blocked_until = _FINNHUB_ECON_BLOCK["until"]
    finnhub_blocked = (
        blocked_until is not None and datetime.utcnow() < blocked_until
    )
    if key and not finnhub_blocked:
        url = (
            f"https://finnhub.io/api/v1/calendar/economic"
            f"?from={start.isoformat()}&to={end.isoformat()}"
            f"&token={urllib.parse.quote(key)}"
        )
        data = _fetch_json(url)
        if data and isinstance(data, dict):
            items = data.get("economicCalendar", []) or []
            for e in items:
                e.setdefault("source", "finnhub")
            _FINNHUB_ECON_BLOCK["until"] = None
            return items, "finnhub"
        if _LAST_HTTP_ERROR_CODE == 403:
            _FINNHUB_ECON_BLOCK["until"] = (
                datetime.utcnow() + timedelta(hours=_FINNHUB_ECON_BLOCK_HOURS)
            )
            log.warning(
                f"Finnhub economic calendar 403 (entitlement block) — "
                f"skipping Finnhub for {_FINNHUB_ECON_BLOCK_HOURS}h, "
                f"using ForexFactory fallback; will re-probe after "
                f"{_FINNHUB_ECON_BLOCK['until']:%Y-%m-%d %H:%M} UTC"
            )
        else:
            log.warning(
                "Finnhub economic calendar unavailable (transient) — "
                "falling back to ForexFactory weekly feed this cycle"
            )
    start_iso, end_iso = start.isoformat(), end.isoformat()
    ff = _fetch_ff_economic_events()
    kept = [
        e for e in ff
        if e.get("time") and start_iso <= e["time"][:10] <= end_iso
    ]

    # FRED layer (2026-06-13, key-optional): extends the schedule
    # horizon beyond FF's this-calendar-week window and fills released
    # actual values that FF never carries. FRED rows are only added for
    # dates OUTSIDE the FF feed's coverage so the two sources never
    # produce duplicate rows for the same print — FF wins inside its
    # window because it has consensus + exact intraday times.
    fred_added = 0
    try:
        from report import fred_data as _fred
        ff_dates = {e["time"][:10] for e in ff if e.get("time")}
        ff_max = max(ff_dates) if ff_dates else ""
        for row in _fred.fetch_fred_release_schedule():
            d = (row.get("time") or "")[:10]
            if not d or not (start_iso <= d <= end_iso):
                continue
            if ff_max and d <= ff_max:
                continue  # inside FF's covered horizon — FF is source
            kept.append(row)
            fred_added += 1
        kept = _fred.enrich_rows_with_fred_actuals(kept)
    except Exception as e:
        log.warning(f"FRED calendar layer failed (non-fatal): {e}")

    if kept:
        kept.sort(key=lambda e: e.get("time", ""))
        return kept, ("forexfactory+fred" if fred_added else "forexfactory")
    raise EconomicCalendarUnavailable(
        "Finnhub economic calendar inaccessible and ForexFactory "
        "fallback failed"
    )


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
      - source             : "finnhub" | "forexfactory" (fallback —
                             this-week coverage only, no actuals)

    Empty list = sources reachable, nothing matched. Raises
    EconomicCalendarUnavailable when BOTH Finnhub and the ForexFactory
    fallback fail — the /ask executor catches it and tells the model
    the FEED is down (so it doesn't claim "no such event exists").
    """
    today = datetime.utcnow().date()
    start = today - timedelta(days=days_window)
    end = today + timedelta(days=days_window)
    items, _source = _fetch_economic_events_raw(start, end)
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
            "source": e.get("source", "finnhub"),
            **({"actual_period": e["actual_period"]}
               if e.get("actual_period") else {}),
        })
    return rows


def fetch_economic_calendar(days_ahead: int = 7) -> str:
    """Return upcoming US + major economic releases with actual dates/times/estimates."""
    today = datetime.utcnow().date()
    # Fetch from yesterday to capture data released overnight that's still context
    start = today - timedelta(days=1)
    end = today + timedelta(days=days_ahead)
    try:
        items, source = _fetch_economic_events_raw(start, end)
    except EconomicCalendarUnavailable:
        # Honest outage line — synthesis must NOT date events from stale
        # research notes as if verified (2026-06-11: calendar fetch
        # failed and the pulse dated the same-day ECB decision to
        # "next week" off an older PDF).
        return (
            "ECONOMIC CALENDAR: (ALL calendar sources failed — Finnhub "
            "down and fallback unreachable. Do NOT state specific "
            "release dates/times as verified; if research PDFs "
            "disagree on an event's date, trust only PDFs published "
            "TODAY and say the date is per that bank's note.)"
        )
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
        if source.startswith("forexfactory"):
            return (
                "ECONOMIC CALENDAR (fallback feed): no high-impact "
                "releases in the visible window."
            )
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
            # actual_period (FRED-enriched rows) names the reference
            # month — "ACTUAL=4.25% (for 2026-05)" — so synthesis can't
            # attach May's print to a June row.
            period = e.get("actual_period")
            suffix = f" (for {period})" if period else ""
            bits.append(f"ACTUAL={actual}{unit}{suffix}")
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
    if source == "forexfactory":
        # Degraded-mode banner: the synthesis prompt leans on ACTUAL
        # values for [RELEASED] RECAP treatment — FF doesn't carry them,
        # and FF only covers the current calendar week.
        out.append(
            "ECONOMIC CALENDAR — FALLBACK FEED (Finnhub down; "
            "ForexFactory weekly feed: THIS CALENDAR WEEK ONLY, no "
            "released ACTUAL values — released events below show "
            "schedule + consensus only; pull actual printed values "
            "from research PDFs published after the release):"
        )
    elif source == "forexfactory+fred":
        out.append(
            "ECONOMIC CALENDAR — FALLBACK FEED (Finnhub down; blended "
            "sources): consensus estimates only exist for THIS calendar "
            "week (ForexFactory); dates beyond this week come from the "
            "FRED official release calendar (no consensus posted — say "
            "'no consensus posted yet', do NOT invent one); ACTUAL "
            "values where shown are official FRED numbers and name "
            "their reference month:"
        )
    if released_lines:
        out.append("ECONOMIC EVENTS ALREADY RELEASED (belongs in RECAP, NEVER in WHAT TO WATCH):")
        out.extend(released_lines)
    if upcoming_lines:
        if out:
            out.append("")
        out.append("ECONOMIC EVENTS STILL UPCOMING (belongs in WHAT TO WATCH):")
        out.extend(upcoming_lines)
    if len(out) <= 1:
        return "ECONOMIC CALENDAR: no high-impact releases in window."
    return "\n".join(out)
