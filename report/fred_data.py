"""FRED (St. Louis Fed) economic-calendar layer.

Added 2026-06-13 after Finnhub gated /calendar/economic behind a paid
entitlement (403 since 2026-06-11). The ForexFactory fallback only
covers the CURRENT calendar week and carries no released actual
values. FRED closes both gaps for US data, free:

  1. RELEASE SCHEDULE — `fred/releases/dates` returns upcoming release
     dates weeks/months ahead for the Tier-1 US prints (CPI, NFP /
     Employment Situation, GDP, retail sales, PPI, PCE). Fixes the
     "WHAT TO WATCH can't date next week's prints" hole.
  2. ACTUAL VALUES — `fred/series/observations` gives the printed
     numbers shortly after release (computed to the same shape the
     pulse quotes: CPI YoY %, NFP monthly change in K, etc.).

NOT covered by FRED: consensus estimates (nobody free has those beyond
ForexFactory's week-of numbers — research PDFs carry desk consensus),
ISM (pulled their data from FRED in 2016), and non-US central banks.

Layering (in report.news_data._fetch_economic_events_raw):
  Finnhub (if entitlement returns) → ForexFactory (this week, has
  consensus) + FRED (schedule beyond this week + actuals enrichment).

Key is optional: settings.fred_api_key empty → every function here
returns empty/None and the calendar behaves exactly as before.
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
_BASE = "https://api.stlouisfed.org/fred"

# FRED release names → the event name our Tier-1 keyword whitelist
# (report.news_data) already matches. All six are 8:30 AM ET statutory
# releases (BLS / BEA / Census), so the schedule rows get that time.
_RELEASE_EVENT_MAP = {
    "consumer price index": "CPI (Consumer Price Index)",
    "employment situation": "Non Farm Payrolls (Employment Situation)",
    "gross domestic product": "GDP (Gross Domestic Product)",
    "advance monthly sales for retail and food services": "Retail Sales",
    "producer price index": "PPI (Producer Price Index)",
    "personal income and outlays": "PCE (Personal Income and Outlays)",
}
_RELEASE_TIME_ET = "08:30"

# Actuals transforms — produce the number the pulse/desk actually quotes:
#   yoy_pct   — % change vs 12 observations ago (monthly series)
#   mom_pct   — % change vs previous observation
#   m_change  — level change vs previous observation (PAYEMS is in
#               thousands, so the change IS the "175K" NFP number)
#   level     — latest observation as-is (already a %, e.g. UNRATE, GDP)
#
# Series choice is keyed on BOTH core-vs-headline AND m/m-vs-y/y read
# from the event NAME — a coarse "any cpi -> headline YoY" match
# stapled headline CPI YoY (4.27%) onto "Core CPI m/m" rows (consensus
# 0.3%), an obvious wrong-number mismatch (caught live 2026-06-13).
# Headline YoY uses the NSA index (the official BLS 12-month figure
# desks quote); m/m uses the SA index (BLS headline m/m is seasonally
# adjusted). Quarterly GDP gets a wider staleness window since its
# print-to-period gap is a full quarter.
_STALE_DAYS_MONTHLY = 75
_STALE_DAYS_QUARTERLY = 160


def _resolve_actual_series(event_name: str):
    """(series_id, transform, unit, max_stale_days) for an event, or
    None when we can't confidently match — honest gap beats wrong fill.
    """
    name = (event_name or "").lower()
    core = "core" in name
    if "m/m" in name or "mom" in name or "month-over-month" in name:
        tf = "mom_pct"
    elif "y/y" in name or "yoy" in name or "year-over-year" in name:
        tf = "yoy_pct"
    else:
        tf = None  # bare name → per-family default below

    if ("non farm payroll" in name or "nonfarm payroll" in name
            or "employment situation" in name
            or "employment change" in name):  # raw ForexFactory title
        return ("PAYEMS", "m_change", "K", _STALE_DAYS_MONTHLY)
    if "unemployment rate" in name:
        return ("UNRATE", "level", "%", _STALE_DAYS_MONTHLY)
    if "cpi" in name or "consumer price" in name:
        if tf == "mom_pct":
            series = "CPILFESL" if core else "CPIAUCSL"  # SA for m/m
        else:
            series = "CPILFENS" if core else "CPIAUCNS"  # NSA for y/y
            tf = "yoy_pct"
        return (series, tf, "%", _STALE_DAYS_MONTHLY)
    if "pce" in name or "personal income" in name or "personal consumption" in name:
        series = "PCEPILFE" if core else "PCEPI"
        return (series, tf or "yoy_pct", "%", _STALE_DAYS_MONTHLY)
    if "ppi" in name or "producer price" in name:
        if core:
            return None  # core-PPI FRED series mapping is ambiguous — skip
        return ("PPIFIS", tf or "mom_pct", "%", _STALE_DAYS_MONTHLY)
    if "retail sales" in name:
        series = "RSFSXMV" if core else "RSAFS"  # ex-autos for "core"
        return (series, tf or "mom_pct", "%", _STALE_DAYS_MONTHLY)
    if "gdp" in name or "gross domestic product" in name:
        # Real GDP, QoQ annualized %, already a level
        return ("A191RL1Q225SBEA", "level", "%", _STALE_DAYS_QUARTERLY)
    return None

# Schedule changes rarely; observations update on release mornings.
# Modest TTLs keep us polite at the 15-minute dump cadence.
_SCHEDULE_CACHE: dict = {"at": None, "rows": None}
_SCHEDULE_CACHE_TTL_S = 6 * 3600
_OBS_CACHE: dict = {}  # series_id -> {"at": dt, "obs": list}
_OBS_CACHE_TTL_S = 30 * 60


def _fred_get(path: str, params: dict) -> dict | None:
    key = settings.fred_api_key
    if not key:
        return None
    qs = urllib.parse.urlencode(
        {**params, "api_key": key, "file_type": "json"}
    )
    url = f"{_BASE}/{path}?{qs}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "MarketPulseBot/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log.warning(f"FRED fetch failed ({path}): {e}")
        return None


def fetch_fred_release_schedule(days_ahead: int = 60) -> list[dict]:
    """Upcoming Tier-1 US release dates as Finnhub-shaped event rows.

    Returns rows {event, country, time, impact, estimate, prev, actual,
    unit, source} — same shape the ForexFactory normalizer emits, so
    news_data merges them without special cases. estimate/prev/actual
    are None (FRED has no consensus; actuals are attached separately by
    enrich_rows_with_fred_actuals for PAST rows).

    Empty list when no key, on fetch failure, or nothing matched.
    """
    if not settings.fred_api_key:
        return []
    now = datetime.utcnow()
    cached_at, cached = _SCHEDULE_CACHE["at"], _SCHEDULE_CACHE["rows"]
    if cached is not None and cached_at is not None and \
            (now - cached_at).total_seconds() < _SCHEDULE_CACHE_TTL_S:
        return cached

    today = now.date()
    data = _fred_get("releases/dates", {
        "realtime_start": today.isoformat(),
        "realtime_end": (today + timedelta(days=days_ahead)).isoformat(),
        "include_release_dates_with_no_data": "true",
        "sort_order": "asc",
        "limit": 500,
    })
    if not data or "release_dates" not in data:
        return cached or []

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for rd in data.get("release_dates") or []:
        name = (rd.get("release_name") or "").strip().lower()
        event = _RELEASE_EVENT_MAP.get(name)
        date_iso = (rd.get("date") or "")[:10]
        if not event or not date_iso:
            continue
        if (event, date_iso) in seen:
            continue
        seen.add((event, date_iso))
        # 8:30 ET → UTC for the row's `time` field
        try:
            local = _ET.localize(datetime.strptime(
                f"{date_iso} {_RELEASE_TIME_ET}", "%Y-%m-%d %H:%M"))
            time_utc = local.astimezone(pytz.UTC).strftime(
                "%Y-%m-%dT%H:%M:%S")
        except (ValueError, TypeError):
            continue
        rows.append({
            "event": event,
            "country": "US",
            "time": time_utc,
            "impact": "high",
            "estimate": None,
            "prev": None,
            "actual": None,
            "unit": "",
            "source": "fred",
        })
    rows.sort(key=lambda r: r["time"])
    _SCHEDULE_CACHE["at"] = now
    _SCHEDULE_CACHE["rows"] = rows
    return rows


def _get_observations(series_id: str) -> list[dict]:
    now = datetime.utcnow()
    c = _OBS_CACHE.get(series_id)
    if c and (now - c["at"]).total_seconds() < _OBS_CACHE_TTL_S:
        return c["obs"]
    data = _fred_get(f"series/observations", {
        "series_id": series_id,
        "sort_order": "desc",
        "limit": 15,  # 13 needed for YoY on monthlies + headroom
    })
    obs = []
    for o in (data or {}).get("observations") or []:
        try:
            obs.append({"date": o.get("date", ""),
                        "value": float(o.get("value"))})
        except (TypeError, ValueError):
            continue  # FRED uses "." for missing values
    if obs:
        _OBS_CACHE[series_id] = {"at": now, "obs": obs}
    return obs


def _compute_actual(series_id: str, transform: str) -> tuple[float | None, str | None]:
    """Latest value for a series under a transform.

    Returns (value, period_iso) — period is the observation month the
    number refers to ('2026-05'), so callers can say WHICH print this
    is instead of implying it's today's.
    """
    obs = _get_observations(series_id)
    if not obs:
        return None, None
    latest = obs[0]
    period = (latest["date"] or "")[:7] or None
    try:
        if transform == "level":
            return round(latest["value"], 2), period
        if transform == "m_change" and len(obs) >= 2:
            return round(latest["value"] - obs[1]["value"], 1), period
        if transform == "mom_pct" and len(obs) >= 2 and obs[1]["value"]:
            return round((latest["value"] / obs[1]["value"] - 1) * 100, 2), period
        if transform == "yoy_pct" and len(obs) >= 13 and obs[12]["value"]:
            return round((latest["value"] / obs[12]["value"] - 1) * 100, 2), period
    except (TypeError, ZeroDivisionError):
        pass
    return None, None


def enrich_rows_with_fred_actuals(rows: list[dict]) -> list[dict]:
    """Fill `actual` on past US rows that have none (mutates + returns).

    Only touches rows whose scheduled time has passed and whose actual
    is None (ForexFactory rows always, FRED schedule rows once the date
    passes). Adds `actual_period` ('2026-05') so the model can name the
    reference month — a May CPI number attached to a June row would be
    a silent lie without it. Guard: the observation period must be
    within 75 days of the row date, else the series is stale relative
    to the event and we leave actual=None (honest gap beats wrong fill).
    """
    if not settings.fred_api_key:
        return rows
    now_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
    for row in rows:
        if row.get("country") != "US" or row.get("actual") is not None:
            continue
        t = row.get("time") or ""
        if not t or t > now_iso:
            continue
        resolved = _resolve_actual_series(row.get("event") or "")
        if not resolved:
            continue
        series, transform, unit, max_stale = resolved
        value, period = _compute_actual(series, transform)
        if value is None or not period:
            continue
        try:
            row_d = datetime.strptime(t[:10], "%Y-%m-%d")
            per_d = datetime.strptime(period + "-01", "%Y-%m-%d")
            if abs((row_d - per_d).days) > max_stale:
                continue  # series stale vs event — don't mislabel
        except (ValueError, TypeError):
            continue
        row["actual"] = value
        row["actual_period"] = period
        if not row.get("unit"):
            row["unit"] = unit
        row["actual_source"] = f"fred:{series}"
    return rows
