"""The economic calendar with Finnhub down and the ForexFactory week empty
(2026-10-26 audit A27/R3)."""
from datetime import date

from report import fred_data as F
from report import news_data as N


def test_fred_schedule_carries_second_tier_releases(monkeypatch):
    monkeypatch.setattr(F.settings, "fred_api_key", "k")
    monkeypatch.setattr(F, "_SCHEDULE_CACHE", {"at": None, "rows": None})
    monkeypatch.setattr(F, "_fred_get", lambda path, params: {"release_dates": [
        {"release_name": "New Residential Construction", "date": "2026-10-21"},
        {"release_name": "Unemployment Insurance Weekly Claims Report", "date": "2026-10-22"},
        {"release_name": "Gross Domestic Product", "date": "2026-10-29"},
        {"release_name": "Visa Spending Momentum Index", "date": "2026-10-22"}]})
    rows = {r["event"]: r for r in F.fetch_fred_release_schedule()}
    assert set(rows) == {"Housing Starts and Building Permits", "Initial Jobless Claims",
                         "GDP (Gross Domestic Product)"}
    assert rows["Housing Starts and Building Permits"]["impact"] == "medium"
    assert rows["Housing Starts and Building Permits"]["time"] == "2026-10-21T12:30:00"
    assert rows["GDP (Gross Domestic Product)"]["impact"] == "high"


def test_fallback_adds_the_fomc_decision(monkeypatch):
    monkeypatch.setattr(N.settings, "finnhub_api_key", "")
    monkeypatch.setattr(N.settings, "fred_api_key", "k")
    monkeypatch.setattr(N, "_fetch_ff_economic_events", lambda: [])
    # FRED serves the week (a GDP row), so the schedule is live
    monkeypatch.setattr(F, "fetch_fred_release_schedule", lambda *a, **k: [
        {"event": "GDP (Gross Domestic Product)", "country": "US", "time": "2026-10-29T12:30:00",
         "impact": "high", "source": "fred"}])
    monkeypatch.setattr(F, "enrich_rows_with_fred_actuals", lambda rows: rows)
    rows, _ = N._fetch_economic_events_raw(date(2026, 10, 26), date(2026, 10, 30))
    fomc = [r for r in rows if r["event"] == "FOMC Statement"]
    assert fomc and fomc[0]["time"] == "2026-10-28T18:00:00"


def test_official_dates_do_not_make_a_dead_feed_look_alive(monkeypatch):
    import pytest
    monkeypatch.setattr(N.settings, "finnhub_api_key", "")
    monkeypatch.setattr(N.settings, "fred_api_key", "k")
    monkeypatch.setattr(N, "_fetch_ff_economic_events", lambda: [])
    monkeypatch.setattr(F, "fetch_fred_release_schedule", lambda *a, **k: [])
    with pytest.raises(N.EconomicCalendarUnavailable):
        N._fetch_economic_events_raw(date(2026, 10, 26), date(2026, 10, 30))
