"""Smoke test: ForexFactory fallback for the economic calendar
(2026-06-11 Finnhub entitlement outage).

Background: Finnhub's /calendar/economic endpoint started returning
403 "You don't have access to this resource" (endpoint moved behind a
paid entitlement; earnings + news endpoints still work on the free
key). The 06-11 scheduled pulse shipped with "ECONOMIC CALENDAR:
(fetch failed)" and, dating events from research PDFs alone, filed the
SAME-DAY ECB rate decision under "Next week". The /ask macro tool was
also silently broken — empty fetch read as "no Tier-1 events found".

Fix:
  - _fetch_economic_events_raw: Finnhub first (self-heals if access
    returns), ForexFactory public weekly JSON as fallback, raises
    EconomicCalendarUnavailable when both fail
  - _fetch_ff_economic_events: normalizes FF rows to the Finnhub event
    shape (title renames so the existing Tier-1 whitelist matches,
    USD->US country mapping, '0.3%'/'220K' value parsing, tz-offset
    date -> naive-UTC time, actual=None since FF carries no actuals)
  - both consumers (pulse text block + /ask structured tool) read
    through the shared layer -> cross-product consistency survives
  - degraded-mode honesty: pulse banner + /ask coverage_note say
    "this calendar week only, no actuals"; dual failure says "feed
    down", never "no events exist"

This smoke covers: normalizer field mapping, Finnhub-first ordering,
fallback trigger, window filtering, dual-failure raise, Tier-1
matching of renamed FF events, pulse banner, executor coverage_note +
feed-down message.
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


def _ff_payload(now=None):
    """Realistic FF rows mirroring the live 2026-06-11 feed shape."""
    now = now or datetime.utcnow()
    d0 = now.strftime("%Y-%m-%d")
    d1 = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    return [
        {"title": "CPI y/y", "country": "USD",
         "date": f"{d0}T08:30:00-04:00", "impact": "High",
         "forecast": "4.2%", "previous": "3.8%"},
        {"title": "Main Refinancing Rate", "country": "EUR",
         "date": f"{d0}T08:15:00-04:00", "impact": "High",
         "forecast": "2.40%", "previous": "2.15%"},
        {"title": "Non-Farm Employment Change", "country": "USD",
         "date": f"{d1}T08:30:00-04:00", "impact": "High",
         "forecast": "175K", "previous": "172K"},
        # Should be dropped by Tier-1: no keyword match
        {"title": "Prelim UoM Consumer Sentiment", "country": "USD",
         "date": f"{d1}T10:00:00-04:00", "impact": "Medium",
         "forecast": "46.1", "previous": "48.2"},
        # Should be dropped: holiday impact + irrelevant
        {"title": "Bank Holiday", "country": "AUD",
         "date": f"{d0}T17:00:00-04:00", "impact": "Holiday",
         "forecast": "", "previous": ""},
    ]


# === Normalizer ===

def test_ff_normalizer_field_mapping():
    from report import news_data
    with patch("report.news_data._fetch_json",
               return_value=_ff_payload(datetime(2026, 6, 11, 9, 0))):
        rows = news_data._fetch_ff_economic_events()

    by_event = {r["event"]: r for r in rows}
    # Renames so the existing Tier-1 whitelist matches
    assert "ECB Interest Rate Decision" in by_event, list(by_event)
    assert "Non Farm Payrolls" in by_event
    # Country mapping
    assert by_event["CPI y/y"]["country"] == "US"
    assert by_event["ECB Interest Rate Decision"]["country"] == "EU"
    # Value parsing: '4.2%' -> 4.2 + '%', '175K' -> 175 + 'K'
    assert by_event["CPI y/y"]["estimate"] == 4.2
    assert by_event["CPI y/y"]["unit"] == "%"
    assert by_event["Non Farm Payrolls"]["estimate"] == 175.0
    assert by_event["Non Farm Payrolls"]["unit"] == "K"
    # tz-offset date -> naive UTC (08:30 EDT = 12:30 UTC)
    assert by_event["CPI y/y"]["time"] == "2026-06-11T12:30:00", \
        by_event["CPI y/y"]["time"]
    # FF carries no actuals
    assert all(r["actual"] is None for r in rows)
    # Holiday impact coerced to low
    assert by_event["Bank Holiday"]["impact"] == "low"
    # Source tag on every row
    assert all(r["source"] == "forexfactory" for r in rows)
    _ok("FF normalizer: renames + country map + value parse + UTC "
        "conversion + no-actuals + source tag")


def test_ff_value_parser_edge_cases():
    from report.news_data import _parse_ff_value
    assert _parse_ff_value("0.3%") == (0.3, "%")
    assert _parse_ff_value("220K") == (220.0, "K")
    assert _parse_ff_value("46.1") == (46.1, "")
    assert _parse_ff_value("-0.5%") == (-0.5, "%")
    assert _parse_ff_value("<0.5%") == (0.5, "%")
    assert _parse_ff_value("") == (None, "")
    assert _parse_ff_value(None) == (None, "")
    assert _parse_ff_value("Tentative") == (None, "")
    _ok("FF value parser handles %, K, bare, negative, '<', empty, "
        "non-numeric")


# === Source ordering + fallback ===

def test_finnhub_first_when_available():
    """When Finnhub answers, FF must NOT be consulted (self-heal: if
    the entitlement comes back we automatically prefer the richer
    source with actuals)."""
    from report import news_data
    today = datetime.utcnow().date()
    fake_finnhub = {"economicCalendar": [
        {"event": "CPI YoY", "country": "US",
         "time": f"{today}T12:30:00", "impact": "high",
         "estimate": 4.0, "prev": 3.8, "actual": 4.25, "unit": "%"},
    ]}
    calls = []

    def fake_fetch(url, timeout=8.0):
        calls.append(url)
        return fake_finnhub

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        items, source = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today + timedelta(days=7))

    assert source == "finnhub"
    assert len(items) == 1 and items[0]["source"] == "finnhub"
    assert len(calls) == 1 and "finnhub.io" in calls[0]
    assert not any("faireconomy" in u for u in calls)
    _ok("Finnhub-first: FF not consulted when Finnhub answers")


def test_fallback_on_finnhub_403():
    """Finnhub fetch failure (403 -> _fetch_json None) falls through to
    FF; events outside the requested window are dropped."""
    from report import news_data
    now = datetime.utcnow()
    today = now.date()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None  # 403 swallowed by _fetch_json
        return _ff_payload(now)

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        items, source = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today + timedelta(days=7))

    assert source == "forexfactory"
    assert len(items) == len(_ff_payload(now))  # all within window
    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        # Window ending yesterday excludes tomorrow's NFP row
        items2, _ = news_data._fetch_economic_events_raw(
            today - timedelta(days=1), today)
    assert all(e["time"][:10] <= today.isoformat() for e in items2)
    _ok("fallback engages on Finnhub failure + respects window filter")


def test_no_key_goes_straight_to_ff():
    from report import news_data
    now = datetime.utcnow()
    calls = []

    def fake_fetch(url, timeout=8.0):
        calls.append(url)
        return _ff_payload(now)

    with patch("config.settings.finnhub_api_key", ""), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        items, source = news_data._fetch_economic_events_raw(
            now.date(), now.date() + timedelta(days=7))
    assert source == "forexfactory"
    assert all("faireconomy" in u for u in calls)
    _ok("no FINNHUB key -> straight to FF (no wasted Finnhub call)")


def test_dual_failure_raises():
    from report import news_data
    today = datetime.utcnow().date()
    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", return_value=None):
        try:
            news_data._fetch_economic_events_raw(
                today, today + timedelta(days=7))
            _fail("expected EconomicCalendarUnavailable")
        except news_data.EconomicCalendarUnavailable:
            pass
    _ok("dual source failure raises EconomicCalendarUnavailable")


# === Through the consumers ===

def test_structured_fetch_tier1_matches_renamed_ff_events():
    """The whole point of the renames: FF's 'Main Refinancing Rate' and
    'Non-Farm Employment Change' must survive the existing Tier-1
    whitelist; UoM sentiment must not."""
    from report import news_data
    now = datetime.utcnow()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None
        return _ff_payload(now)

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        rows = news_data.fetch_economic_calendar_structured(days_window=7)

    events = {r["event"] for r in rows}
    assert "ECB Interest Rate Decision" in events, events
    assert "Non Farm Payrolls" in events
    assert "CPI y/y" in events
    assert "Prelim UoM Consumer Sentiment" not in events
    assert "Bank Holiday" not in events
    # Source flows through to structured rows for the executor
    assert all(r["source"] == "forexfactory" for r in rows)
    # FF released events have no actual -> past events read past_no_data
    assert all(r["actual"] is None for r in rows)
    _ok("structured fetch via FF: Tier-1 keeps renamed ECB/NFP/CPI, "
        "drops UoM + holiday, tags source")


def test_pulse_text_block_banner_and_outage_line():
    from report import news_data
    now = datetime.utcnow()

    def fake_fetch(url, timeout=8.0):
        if "finnhub.io" in url:
            return None
        return _ff_payload(now)

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", side_effect=fake_fetch):
        text = news_data.fetch_economic_calendar(days_ahead=7)
    assert "FALLBACK FEED" in text, text[:200]
    assert "THIS CALENDAR WEEK ONLY" in text
    assert "ECB Interest Rate Decision" in text

    with patch("config.settings.finnhub_api_key", "test_key"), \
         patch("report.news_data._fetch_json", return_value=None):
        text2 = news_data.fetch_economic_calendar(days_ahead=7)
    assert "ALL calendar sources failed" in text2
    assert "TODAY" in text2  # the date-trust instruction for synthesis
    _ok("pulse block: FF banner in fallback mode; honest "
        "date-discipline line on total outage")


# === /ask executor ===

def test_executor_coverage_note_on_ff_source():
    from discord_bot import bot as bot_mod
    fake_rows = [{
        "event": "ECB Interest Rate Decision", "country": "EU",
        "scheduled_iso_utc": "2026-06-11T12:15:00",
        "scheduled_et_human": "2026-06-11 08:15 EDT",
        "impact": "high", "consensus": 2.40, "prev": 2.15,
        "actual": None, "unit": "%", "status": "scheduled",
        "source": "forexfactory",
    }]
    with patch("report.news_data.fetch_economic_calendar_structured",
               return_value=fake_rows):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "ECB"}))
    assert result["status"] == "ok"
    assert "coverage_note" in result
    assert "CALENDAR WEEK" in result["coverage_note"]
    assert "Google Search" in result["coverage_note"]
    _ok("executor adds coverage_note when rows come from the fallback")


def test_executor_feed_down_message():
    from discord_bot import bot as bot_mod
    from report.news_data import EconomicCalendarUnavailable
    with patch("report.news_data.fetch_economic_calendar_structured",
               side_effect=EconomicCalendarUnavailable("both down")):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "CPI"}))
    assert result["status"] == "error"
    assert "down" in result["error"].lower()
    assert "GOOGLE SEARCH" in result["error"]
    assert "doesn't exist" in result["error"] or \
        "does not exist" in result["error"]
    _ok("executor on dual failure: 'feed down' + Google fallback, "
        "never 'event doesn't exist'")


def test_executor_no_coverage_note_on_finnhub():
    from discord_bot import bot as bot_mod
    fake_rows = [{
        "event": "CPI YoY", "country": "US",
        "scheduled_iso_utc": "2026-06-10T12:30:00",
        "scheduled_et_human": "2026-06-10 08:30 EDT",
        "impact": "high", "consensus": 4.0, "prev": 3.8,
        "actual": 4.25, "unit": "%", "status": "released",
        "source": "finnhub",
    }]
    with patch("report.news_data.fetch_economic_calendar_structured",
               return_value=fake_rows):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "CPI"}))
    assert result["status"] == "ok"
    assert "coverage_note" not in result
    _ok("executor: no coverage_note when Finnhub is the source")


if __name__ == "__main__":
    print("=== economic-calendar ForexFactory fallback smoke ===")
    test_ff_normalizer_field_mapping()
    test_ff_value_parser_edge_cases()
    test_finnhub_first_when_available()
    test_fallback_on_finnhub_403()
    test_no_key_goes_straight_to_ff()
    test_dual_failure_raises()
    test_structured_fetch_tier1_matches_renamed_ff_events()
    test_pulse_text_block_banner_and_outage_line()
    test_executor_coverage_note_on_ff_source()
    test_executor_feed_down_message()
    test_executor_no_coverage_note_on_finnhub()
    print("\nALL CALENDAR-FALLBACK SMOKE TESTS PASS")
