"""FRED actuals must be the print, or nothing (2026-09-11).

One minute after the August 2026 CPI release, /ask answered "what was
the economic print" with +0.07% m/m and 3.52% y/y. BLS printed +0.4%
and 3.4%. Two defects, both in report/fred_data.py:

1. FRED had not yet posted August, and the 75-day staleness guard let
   July's observation ride on the August row as its "actual".
2. FRED has no October 2025 CPI (the shutdown month), so "12
   observations back" was 13 months back and every year-over-year the
   feed produced was a 13-month change (3.69% for August once FRED
   updated, against the printed 3.4%).
"""
import sys
from unittest.mock import patch

from report import fred_data as F

# FRED's CPIAUCNS window as fetched on 2026-09-11 (newest first). Note
# the missing 2025-10 observation.
NSA = [("2026-08", 334.98), ("2026-07", 333.918), ("2026-06", 333.952), ("2026-05", 335.123),
       ("2026-04", 333.02), ("2026-03", 330.213), ("2026-02", 326.785), ("2026-01", 325.252),
       ("2025-12", 324.054), ("2025-11", 324.122), ("2025-09", 324.8), ("2025-08", 323.976),
       ("2025-07", 323.048), ("2025-06", 322.561)]


def _obs(pairs):
    return {"observations": [{"date": f"{d}-01", "value": str(v)} for d, v in pairs]}


def _with(pairs):
    F._OBS_CACHE.clear()
    return patch("config.settings.fred_api_key", "k"), patch("report.fred_data._fred_get", return_value=_obs(pairs))


def test_month_shift_and_reference_period():
    assert F.month_shift("2026-08", -12) == "2025-08"
    assert F.month_shift("2026-01", -1) == "2025-12"
    assert F.month_shift("2025-12", 1) == "2026-01"
    assert F.reference_period("2026-09-11T12:30:00") == "2026-08"
    assert F.reference_period("2026-01-14T13:30:00") == "2025-12"


def test_year_over_year_uses_the_calendar_month_not_the_index():
    a, b = _with(NSA)
    with a, b:
        v, period = F._compute_actual("CPIAUCNS", "yoy_pct")
    # 2025-08 is in the window, so the true 12-month change exists
    assert period == "2026-08" and v == round((334.98 / 323.976 - 1) * 100, 2) == 3.4
    # drop the true base month: the index-based code would have used
    # 2025-07 and reported 3.69; the date-based code reports nothing
    a, b = _with([p for p in NSA if p[0] != "2025-08"])
    with a, b:
        v, period = F._compute_actual("CPIAUCNS", "yoy_pct")
    assert v is None and period is None


def test_month_over_month_needs_the_previous_calendar_month():
    a, b = _with(NSA)
    with a, b:
        v, _ = F._compute_actual("CPIAUCNS", "mom_pct")
    assert v == 0.32
    a, b = _with([p for p in NSA if p[0] != "2026-07"])
    with a, b:
        v, _ = F._compute_actual("CPIAUCNS", "mom_pct")
    assert v is None
    a, b = _with([("2026-08", 159.9), ("2026-06", 159.5)])
    with a, b:
        v, _ = F._compute_actual("PAYEMS", "m_change")
    assert v is None, "a level change across a missing month is not the monthly print"


def test_the_0831_scenario_attaches_nothing_when_fred_lags_the_release():
    """CPI row for 2026-09-11 08:30 ET, FRED still ending at July: the
    July figures must NOT become the August 'actual'."""
    july_only = [p for p in NSA if p[0] != "2026-08"]
    rows = [{"event": "CPI y/y", "country": "US", "time": "2026-09-11T12:30:00",
             "actual": None, "unit": "%", "source": "forexfactory"},
            {"event": "CPI m/m", "country": "US", "time": "2026-09-11T12:30:00",
             "actual": None, "unit": "%", "source": "forexfactory"}]
    a, b = _with(july_only)
    with a, b, patch("report.fred_data.datetime") as dt:
        from datetime import datetime as real_dt
        dt.utcnow.return_value = real_dt(2026, 9, 11, 12, 31)
        dt.strptime = real_dt.strptime
        out = F.enrich_rows_with_fred_actuals(rows)
    assert out[0]["actual"] is None and out[1]["actual"] is None
    assert "actual_period" not in out[0]
    # once August lands, the row carries August and names it
    a, b = _with(NSA)
    with a, b, patch("report.fred_data.datetime") as dt:
        from datetime import datetime as real_dt
        dt.utcnow.return_value = real_dt(2026, 9, 11, 13, 0)
        dt.strptime = real_dt.strptime
        out = F.enrich_rows_with_fred_actuals(rows)
    assert out[0]["actual"] == 3.4 and out[0]["actual_period"] == "2026-08"
    assert out[1]["actual"] == 0.32


def test_quarterly_gdp_keeps_the_wide_window():
    rows = [{"event": "GDP q/q", "country": "US", "time": "2026-08-28T12:30:00",
             "actual": None, "unit": "%", "source": "forexfactory"}]
    a, b = _with([("2026-04", 3.0), ("2026-01", 2.1)])
    with a, b, patch("report.fred_data.datetime") as dt:
        from datetime import datetime as real_dt
        dt.utcnow.return_value = real_dt(2026, 9, 11, 13, 0)
        dt.strptime = real_dt.strptime
        out = F.enrich_rows_with_fred_actuals(rows)
    assert out[0]["actual"] == 3.0 and out[0]["actual_period"] == "2026-04"


def test_econ_injection_tells_the_model_what_past_no_data_means():
    from discord_bot import ask_router as R
    txt = R.inject_text(R.T_ECON, {"status": "ok", "events": [
        {"event": "CPI y/y", "status": "past_no_data", "consensus": 3.4, "prev": 3.4, "actual": None}]})
    assert "past_no_data" in txt and "never fill the actual from memory" in txt


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
