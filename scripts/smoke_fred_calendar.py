"""Smoke test: FRED economic-calendar layer (2026-06-13).

Background: Finnhub gated /calendar/economic behind a paid entitlement
(403 since 06-11). The ForexFactory fallback covers only the CURRENT
calendar week and carries no released actual values — so WHAT TO WATCH
couldn't date next week's prints and released events showed no actuals.

Fix: report/fred_data.py (key-optional via FRED_API_KEY):
  - fetch_fred_release_schedule: `fred/releases/dates` -> Tier-1 US
    release dates weeks ahead, normalized to the Finnhub event shape
    (8:30 ET statutory time -> UTC, names mapped so the existing
    Tier-1 whitelist matches)
  - enrich_rows_with_fred_actuals: fills `actual` on past US rows from
    official FRED series with the transform the desk actually quotes
    (CPI YoY %, NFP monthly change in K, UNRATE level...). Adds
    `actual_period` naming the reference month and a 75-day staleness
    guard (honest gap beats wrong fill).
  - news_data merge: FRED rows added ONLY for dates beyond FF's
    covered horizon (no duplicate prints); source label becomes
    'forexfactory+fred'; pulse banner + /ask coverage_note describe
    the blended coverage honestly.
  - No key -> everything dormant, behavior identical to before.

This smoke covers: schedule normalization, dedupe, ET->UTC time,
transforms (yoy/mom/m_change/level + '.' missing values), staleness
guard, actual_period, merge horizon rule, source labels, dormancy,
failure isolation, executor coverage_note for blended sources.
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _reset_state():
    from report import news_data, fred_data
    news_data._FF_CACHE["at"] = None
    news_data._FF_CACHE["rows"] = None
    news_data._FINNHUB_ECON_BLOCK["until"] = None
    news_data._LAST_HTTP_ERROR_CODE = None
    fred_data._SCHEDULE_CACHE["at"] = None
    fred_data._SCHEDULE_CACHE["rows"] = None
    fred_data._OBS_CACHE.clear()


def _fred_dates_payload(today):
    d10 = (today + timedelta(days=10)).isoformat()
    d12 = (today + timedelta(days=12)).isoformat()
    return {"release_dates": [
        {"release_id": 10, "release_name": "Consumer Price Index",
         "date": d10},
        {"release_id": 10, "release_name": "Consumer Price Index",
         "date": d10},  # duplicate row — must dedupe
        {"release_id": 50, "release_name": "Employment Situation",
         "date": d12},
        {"release_id": 999, "release_name": "H.15 Selected Interest Rates",
         "date": d10},  # not in our map — dropped
    ]}


# === Schedule ===

def test_schedule_normalization_and_dedupe():
    from report import fred_data
    today = datetime.utcnow().date()
    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get",
               return_value=_fred_dates_payload(today)):
        rows = fred_data.fetch_fred_release_schedule()

    assert len(rows) == 2, [r["event"] for r in rows]
    by_event = {r["event"]: r for r in rows}
    assert "CPI (Consumer Price Index)" in by_event
    assert "Non Farm Payrolls (Employment Situation)" in by_event
    cpi = by_event["CPI (Consumer Price Index)"]
    assert cpi["country"] == "US"
    assert cpi["impact"] == "high"
    assert cpi["source"] == "fred"
    assert cpi["estimate"] is None and cpi["actual"] is None
    # 8:30 ET = 12:30 UTC during EDT / 13:30 during EST
    assert cpi["time"].endswith("12:30:00") or \
        cpi["time"].endswith("13:30:00"), cpi["time"]
    _ok("FRED schedule: name mapping, dedupe, unmapped dropped, "
        "8:30 ET -> UTC")


def test_schedule_dormant_without_key():
    from report import fred_data
    called = {"n": 0}

    def fake_get(path, params):
        called["n"] += 1
        return {}

    with patch("config.settings.fred_api_key", ""), \
         patch("report.fred_data._fred_get", side_effect=fake_get):
        assert fred_data.fetch_fred_release_schedule() == []
    assert called["n"] == 0, "no key must mean no FRED requests"
    _ok("FRED schedule dormant without key (no requests)")


def test_schedule_cache():
    from report import fred_data
    today = datetime.utcnow().date()
    calls = {"n": 0}

    def fake_get(path, params):
        calls["n"] += 1
        return _fred_dates_payload(today)

    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get", side_effect=fake_get):
        first = fred_data.fetch_fred_release_schedule()
        second = fred_data.fetch_fred_release_schedule()
    assert calls["n"] == 1
    assert second == first
    _ok("FRED schedule cached (no refetch within TTL)")


# === Actuals transforms ===

def _obs(pairs):
    return {"observations": [
        {"date": d, "value": v} for d, v in pairs
    ]}


def test_actual_transforms():
    from report import fred_data
    # 14 months of a fake index, newest first: latest 130.0, year-ago 125.0
    months = [(f"2026-{m:02d}-01" if m > 0 else "", 0) for m in range(0)]
    obs_pairs = []
    base = datetime(2026, 5, 1)
    vals = [130.0, 129.5, 129.0, 128.6, 128.2, 127.8, 127.4,
            127.0, 126.6, 126.2, 125.8, 125.4, 125.0, 124.6]
    for i, v in enumerate(vals):
        d = (base - timedelta(days=30 * i)).strftime("%Y-%m-01")
        obs_pairs.append((d, v))

    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get",
               return_value=_obs(obs_pairs)):
        yoy, period = fred_data._compute_actual("CPIAUCSL", "yoy_pct")
        fred_data._OBS_CACHE.clear()
        mom, _ = fred_data._compute_actual("RSAFS", "mom_pct")
        fred_data._OBS_CACHE.clear()
        chg, _ = fred_data._compute_actual("PAYEMS", "m_change")
        fred_data._OBS_CACHE.clear()
        lvl, _ = fred_data._compute_actual("UNRATE", "level")

    assert yoy == 4.0, yoy        # 130 / 125 - 1 = 4.0%
    assert period == "2026-05"
    assert mom == 0.39, mom        # 130 / 129.5 - 1 = 0.386...
    assert chg == 0.5, chg         # 130 - 129.5
    assert lvl == 130.0
    _ok("FRED transforms: yoy_pct / mom_pct / m_change / level + "
        "period naming")


def test_actuals_skip_missing_values():
    """FRED encodes missing observations as value '.' — they must be
    skipped, not parsed as 0."""
    from report import fred_data
    payload = {"observations": [
        {"date": "2026-05-01", "value": "."},
        {"date": "2026-04-01", "value": "4.1"},
        {"date": "2026-03-01", "value": "4.3"},
    ]}
    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get", return_value=payload):
        lvl, period = fred_data._compute_actual("UNRATE", "level")
    assert lvl == 4.1 and period == "2026-04"
    _ok("FRED observations: '.' missing values skipped")


def test_enrich_rows_and_staleness_guard():
    from report import fred_data
    now = datetime.utcnow()
    past = (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (now + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    recent_period = (now - timedelta(days=20)).strftime("%Y-%m-01")
    rows = [
        # past US CPI row with no actual -> should be enriched
        {"event": "CPI y/y", "country": "US", "time": past,
         "actual": None, "unit": "%", "source": "forexfactory"},
        # future row -> untouched
        {"event": "CPI y/y", "country": "US", "time": future,
         "actual": None, "unit": "%", "source": "fred"},
        # non-US -> untouched
        {"event": "ECB Interest Rate Decision", "country": "EU",
         "time": past, "actual": None, "unit": "%",
         "source": "forexfactory"},
    ]
    obs_pairs = [(recent_period, 130.0)] + [
        ((now - timedelta(days=20 + 30 * i)).strftime("%Y-%m-01"),
         130.0 - i * 0.4)
        for i in range(1, 14)
    ]
    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get",
               return_value=_obs(obs_pairs)):
        out = fred_data.enrich_rows_with_fred_actuals(rows)

    assert out[0]["actual"] is not None, "past US row must be enriched"
    assert out[0]["actual_period"] == recent_period[:7]
    assert out[0]["actual_source"].startswith("fred:")
    assert out[1]["actual"] is None, "future row must stay None"
    assert out[2]["actual"] is None, "non-US row must stay None"

    # Staleness guard: observation 6 months older than the row date
    fred_data._OBS_CACHE.clear()
    stale_pairs = [
        ((now - timedelta(days=200 + 30 * i)).strftime("%Y-%m-01"),
         130.0 - i * 0.4)
        for i in range(14)
    ]
    rows2 = [{"event": "CPI y/y", "country": "US", "time": past,
              "actual": None, "unit": "%", "source": "forexfactory"}]
    with patch("config.settings.fred_api_key", "test_key"), \
         patch("report.fred_data._fred_get",
               return_value=_obs(stale_pairs)):
        out2 = fred_data.enrich_rows_with_fred_actuals(rows2)
    assert out2[0]["actual"] is None, (
        "stale observation (>75d from row) must NOT be attached"
    )
    _ok("enrich: past-US-only + actual_period + staleness guard")


# === Merge into news_data ===

def _ff_week_payload(now):
    d0 = now.strftime("%Y-%m-%d")
    return [
        {"title": "CPI y/y", "country": "USD",
         "date": f"{d0}T08:30:00-04:00", "impact": "High",
         "forecast": "4.2%", "previous": "3.8%"},
    ]


def test_merge_adds_fred_only_beyond_ff_horizon():
    """FRED rows inside FF's covered window are dropped (FF has
    consensus + exact times there); beyond it they extend the
    schedule. Source label flips to forexfactory+fred only when FRED
    actually contributed."""
    from report import news_data, fred_data
    now = datetime.utcnow()
    today = now.date()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None  # 403/down
        return _ff_week_payload(now)

    fred_rows = [
        # same day as FF coverage -> must be dropped (dup horizon)
        {"event": "CPI (Consumer Price Index)", "country": "US",
         "time": now.strftime("%Y-%m-%dT12:30:00"), "impact": "high",
         "estimate": None, "prev": None, "actual": None, "unit": "",
         "source": "fred"},
        # +10 days -> beyond FF horizon -> must be added
        {"event": "Non Farm Payrolls (Employment Situation)",
         "country": "US",
         "time": (now + timedelta(days=10)).strftime("%Y-%m-%dT12:30:00"),
         "impact": "high", "estimate": None, "prev": None,
         "actual": None, "unit": "", "source": "fred"},
    ]
    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch), \
         patch("report.fred_data.fetch_fred_release_schedule",
               return_value=fred_rows), \
         patch("report.fred_data.enrich_rows_with_fred_actuals",
               side_effect=lambda rows: rows):
        items, source = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today + timedelta(days=14))

    events = [(e["event"], e["source"]) for e in items]
    assert source == "forexfactory+fred", source
    assert ("CPI y/y", "forexfactory") in events
    assert ("Non Farm Payrolls (Employment Situation)", "fred") in events
    # the in-window FRED CPI duplicate is NOT present
    assert ("CPI (Consumer Price Index)", "fred") not in events
    _ok("merge: FRED extends beyond FF horizon, no duplicate prints, "
        "blended source label")


def test_merge_label_stays_ff_when_fred_dormant():
    from report import news_data
    now = datetime.utcnow()
    today = now.date()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None
        return _ff_week_payload(now)

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("config.settings.fred_api_key", ""), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        items, source = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today + timedelta(days=14))
    assert source == "forexfactory"
    assert all(e["source"] == "forexfactory" for e in items)
    _ok("merge: no FRED key -> identical pre-FRED behavior + label")


def test_merge_fred_failure_is_isolated():
    """A FRED exception must never take down the calendar — FF rows
    still ship."""
    from report import news_data
    now = datetime.utcnow()
    today = now.date()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None
        return _ff_week_payload(now)

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch), \
         patch("report.fred_data.fetch_fred_release_schedule",
               side_effect=RuntimeError("FRED 500")):
        items, source = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today + timedelta(days=14))
    assert items and source == "forexfactory"
    _ok("merge: FRED failure isolated — FF rows still served")


def test_pulse_banner_blended_sources():
    from report import news_data
    now = datetime.utcnow()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None
        return _ff_week_payload(now)

    fred_rows = [{
        "event": "PPI (Producer Price Index)", "country": "US",
        "time": (now + timedelta(days=9)).strftime("%Y-%m-%dT12:30:00"),
        "impact": "high", "estimate": None, "prev": None,
        "actual": None, "unit": "", "source": "fred",
    }]
    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch), \
         patch("report.fred_data.fetch_fred_release_schedule",
               return_value=fred_rows), \
         patch("report.fred_data.enrich_rows_with_fred_actuals",
               side_effect=lambda rows: rows):
        text = news_data.fetch_economic_calendar(days_ahead=14)
    assert "blended" in text.lower(), text[:200]
    assert "FRED official release calendar" in text
    assert "do NOT invent" in text
    assert "PPI (Producer Price Index)" in text
    _ok("pulse banner: blended-sources wording + FRED row rendered")


def test_executor_coverage_note_mentions_fred_semantics():
    from discord_bot import bot as bot_mod
    fake_rows = [
        {"event": "CPI y/y", "country": "US",
         "scheduled_iso_utc": "2026-06-10T12:30:00",
         "scheduled_et_human": "2026-06-10 08:30 EDT",
         "impact": "high", "consensus": 4.2, "prev": 3.8,
         "actual": 4.25, "unit": "%", "status": "released",
         "source": "forexfactory", "actual_period": "2026-05"},
        {"event": "Non Farm Payrolls (Employment Situation)",
         "country": "US",
         "scheduled_iso_utc": "2026-06-23T12:30:00",
         "scheduled_et_human": "2026-06-23 08:30 EDT",
         "impact": "high", "consensus": None, "prev": None,
         "actual": None, "unit": "", "status": "scheduled",
         "source": "fred"},
    ]
    with patch("report.news_data.fetch_economic_calendar_structured",
               return_value=fake_rows):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": None}))
    assert result["status"] == "ok"
    note = result.get("coverage_note", "")
    assert "fred" in note.lower()
    assert "actual_period" in note
    assert "no consensus posted yet" in note
    _ok("executor coverage_note: blended semantics (fred dates, "
        "actual_period, no-invented-consensus)")


_ALL = [
    test_schedule_normalization_and_dedupe,
    test_schedule_dormant_without_key,
    test_schedule_cache,
    test_actual_transforms,
    test_actuals_skip_missing_values,
    test_enrich_rows_and_staleness_guard,
    test_merge_adds_fred_only_beyond_ff_horizon,
    test_merge_label_stays_ff_when_fred_dormant,
    test_merge_fred_failure_is_isolated,
    test_pulse_banner_blended_sources,
    test_executor_coverage_note_mentions_fred_semantics,
]

if __name__ == "__main__":
    print("=== FRED calendar layer smoke ===")
    for t in _ALL:
        _reset_state()
        t()
    print("\nALL FRED CALENDAR SMOKE TESTS PASS")
