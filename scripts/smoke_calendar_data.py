"""Smoke: calendar_data build/filter/rank/truncate (spec §7). No network
— fetchers and cache patched with fixtures."""

import sys
from unittest.mock import patch

import report.calendar_data as cd


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _fixture_earnings(n_bmo=30, n_amc=25, n_blank=10, n_dmh=2):
    rows = []
    for i in range(n_bmo):
        rows.append({"symbol": f"B{i}", "hour": "bmo"})
    for i in range(n_amc):
        rows.append({"symbol": f"A{i}", "hour": "amc"})
    for i in range(n_blank):
        rows.append({"symbol": f"X{i}", "hour": ""})
    for i in range(n_dmh):
        rows.append({"symbol": f"D{i}", "hour": "dmh"})
    return rows


def _caps_for(rows):
    # cap = index-derived so ranking is deterministic; B0 largest
    out = {}
    for i, r in enumerate(rows):
        out[r["symbol"]] = {"cap": 10000.0 - i, "name": f"Co {r['symbol']}"}
    return out


def _build(date="2026-08-20", earnings=None, econ=None, caps=None,
           holiday=False):
    earnings = _fixture_earnings() if earnings is None else earnings
    caps = _caps_for(earnings if isinstance(earnings, list) else []) \
        if caps is None else caps
    patches = [
        patch.object(cd.news_data, "fetch_earnings_calendar_all",
                     lambda d: earnings),
        patch.object(cd.news_data, "fetch_us_econ_events_for_date",
                     lambda d: econ if econ is not None else []),
        patch.object(cd.db, "get_market_caps", lambda syms: caps),
        patch.object(cd.db, "upsert_market_caps", lambda rows: len(rows)),
        patch.object(cd.news_data, "fetch_symbol_profiles",
                     lambda syms, **kw: {}),
    ]
    import world_context
    hp = patch.object(world_context, "US_MARKET_HOLIDAYS",
                      {date: "Test Holiday"} if holiday else {})
    for p in patches + [hp]:
        p.start()
    try:
        return cd.build_calendar_day(date)
    finally:
        for p in patches + [hp]:
            p.stop()


def test_top20_and_dropped_counts():
    day = _build()
    assert len(day.bmo) == 20 and day.dropped_bmo == 10, \
        (len(day.bmo), day.dropped_bmo)
    # AMC pool = 25 amc + 10 blank + 2 dmh = 37 -> 20 kept, 17 dropped
    assert len(day.amc) == 20 and day.dropped_amc == 17, \
        (len(day.amc), day.dropped_amc)
    _ok("exactly 20 per session; dropped_* counts right")


def test_cap_sort_and_missing_cap_last():
    earnings = [{"symbol": "BIG", "hour": "bmo"},
                {"symbol": "MID", "hour": "bmo"},
                {"symbol": "NOCAP", "hour": "bmo"}]
    caps = {"BIG": {"cap": 900.0, "name": "Big"},
            "MID": {"cap": 50.0, "name": "Mid"},
            "NOCAP": {"cap": 0.0, "name": "NOCAP"}}
    day = _build(earnings=earnings, caps=caps)
    assert [r.symbol for r in day.bmo] == ["BIG", "MID", "NOCAP"], day.bmo
    _ok("sorted by cap desc; missing-cap symbols sort last, still shown")


def test_blank_and_dmh_land_in_amc_flagged():
    earnings = [{"symbol": "W", "hour": "bmo"},
                {"symbol": "BLANK", "hour": ""},
                {"symbol": "DUR", "hour": "dmh"},
                {"symbol": "REAL", "hour": "amc"}]
    day = _build(earnings=earnings,
                 caps={s["symbol"]: {"cap": 1, "name": s["symbol"]}
                       for s in earnings})
    amc = {r.symbol: r.session_confirmed for r in day.amc}
    assert set(amc) == {"BLANK", "DUR", "REAL"}, amc
    assert amc["REAL"] is True and amc["BLANK"] is False \
        and amc["DUR"] is False, amc
    assert day.bmo[0].session_confirmed is True
    _ok("dmh/blank -> AMC and flagged; true amc/bmo confirmed")


def test_holiday_flag_and_no_earnings():
    day = _build(holiday=True,
                 econ=[{"time": "2026-08-20T12:30:00", "event": "Claims",
                        "impact": "high"}])
    assert day.is_holiday == "Test Holiday"
    assert day.bmo == [] and day.amc == [], "holiday must skip earnings"
    assert day.econ and day.econ[0].time_et == "8:30", day.econ
    _ok("holiday: name set, earnings skipped, rare econ release kept")


def test_feed_down_vs_quiet_day():
    day = _build(earnings=None if False else [], econ=[])
    assert day.earnings_available and day.econ_available
    with patch.object(cd.news_data, "fetch_earnings_calendar_all",
                      lambda d: None), \
         patch.object(cd.news_data, "fetch_us_econ_events_for_date",
                      lambda d: None), \
         patch.object(cd.db, "get_market_caps", lambda s: {}), \
         patch.object(cd.db, "upsert_market_caps", lambda r: 0), \
         patch.object(cd.news_data, "fetch_symbol_profiles",
                      lambda s, **kw: {}):
        down = cd.build_calendar_day("2026-08-20")
    assert not down.earnings_available and not down.econ_available
    _ok("None (feed down) distinguished from [] (quiet day)")


def test_et_conversion_dst_correct():
    # August = EDT (UTC-4); a January date = EST (UTC-5)
    assert cd._to_et_hhmm("2026-08-20T12:30:00") == "8:30"
    assert cd._to_et_hhmm("2026-01-15T13:30:00") == "8:30"
    _ok("UTC->ET conversion is DST-correct (zoneinfo)")


if __name__ == "__main__":
    print("=== calendar data smoke ===")
    test_top20_and_dropped_counts()
    test_cap_sort_and_missing_cap_last()
    test_blank_and_dmh_land_in_amc_flagged()
    test_holiday_flag_and_no_earnings()
    test_feed_down_vs_quiet_day()
    test_et_conversion_dst_correct()
    print("\nALL CALENDAR DATA SMOKE TESTS PASS")
