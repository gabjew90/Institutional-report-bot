"""Second-source report-date check on the omni-calendar (2026-09-14).

The Monday 9/14 sheet listed Cracker Barrel before the open with a
straddle; the company had announced Wednesday 9/23 on 9/9 and Finnhub
still carried the stale date. A Finnhub row now needs Nasdaq's
calendar to list the symbol for the same date. When Nasdaq is down,
the sheet keeps Finnhub alone.
"""
import sys
from unittest.mock import patch

import db
from report import calendar_data as cd
from report import news_data as nd

ROWS = [{"symbol": "CBRL", "hour": "bmo"}, {"symbol": "PLAY", "hour": "amc"},
        {"symbol": "KMTS", "hour": "amc"}]
CAPS = {"CBRL": {"cap": 1102.0, "name": "Cracker Barrel Old Country Store Inc"},
        "PLAY": {"cap": 287.0, "name": "Dave and Buster's Entertainment, Inc"},
        "KMTS": {"cap": 1340.0, "name": "Kestra Medical Technologies Ltd"}}


def _build(nasdaq):
    with patch.object(nd, "fetch_earnings_calendar_all", lambda d: [dict(r) for r in ROWS]), \
         patch.object(nd, "fetch_us_econ_events_for_date", lambda d: []), \
         patch.object(nd, "fetch_nasdaq_earnings_symbols", lambda d: nasdaq), \
         patch.object(cd, "_resolve_caps", lambda syms: {s: CAPS[s] for s in syms if s in CAPS}), \
         patch.object(cd, "_implied_move_fetch", lambda s, d, session=None: 9.0), \
         patch.object(cd, "_MOVE_PACE_S", 0), \
         patch.object(cd, "_has_options", lambda s: True), \
         patch.object(cd, "_resolve_logos", lambda syms, caps: {}), \
         patch.object(db, "recently_covered_tickers", lambda days=7: set()), \
         patch.object(db, "conference_sessions_for_date", lambda d, **kw: []):
        return cd.build_calendar_day("2026-09-14")


def test_a_row_nasdaq_does_not_list_for_the_date_is_dropped_and_recorded():
    day = _build({"PLAY", "KMTS", "RLGT", "HAIN"})
    shown = [r.symbol for r in day.bmo + day.amc]
    assert "CBRL" not in shown, shown
    assert shown == ["KMTS", "PLAY"], shown
    assert day.date_unconfirmed == ["CBRL"]


def test_nasdaq_unavailable_keeps_finnhub_alone():
    day = _build(None)
    shown = sorted(r.symbol for r in day.bmo + day.amc)
    assert shown == ["CBRL", "KMTS", "PLAY"], shown
    assert day.date_unconfirmed == []


def test_a_lower_case_finnhub_symbol_still_matches():
    rows = [{"symbol": "cbrl", "hour": "bmo"}]
    with patch.object(nd, "fetch_earnings_calendar_all", lambda d: rows), \
         patch.object(nd, "fetch_us_econ_events_for_date", lambda d: []), \
         patch.object(nd, "fetch_nasdaq_earnings_symbols", lambda d: {"CBRL"}), \
         patch.object(cd, "_resolve_caps", lambda syms: {s: {"cap": 1102.0, "name": "Cracker Barrel"} for s in syms}), \
         patch.object(cd, "_implied_move_fetch", lambda s, d, session=None: 9.0), \
         patch.object(cd, "_MOVE_PACE_S", 0), \
         patch.object(cd, "_has_options", lambda s: True), \
         patch.object(cd, "_resolve_logos", lambda syms, caps: {}), \
         patch.object(db, "recently_covered_tickers", lambda days=7: set()), \
         patch.object(db, "conference_sessions_for_date", lambda d, **kw: []):
        day = cd.build_calendar_day("2026-09-14")
    assert [r.symbol for r in day.bmo] == ["cbrl"] and day.date_unconfirmed == []


def test_fetcher_parses_the_nasdaq_payload_and_treats_empty_as_unavailable():
    import io
    import json

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    payload = {"data": {"rows": [{"symbol": "PLAY", "time": "time-after-hours"},
                                 {"symbol": "kmts", "time": "time-not-supplied"}]}}
    with patch("urllib.request.urlopen", lambda req, timeout=15: _Resp(json.dumps(payload).encode())):
        assert nd.fetch_nasdaq_earnings_symbols("2026-09-14") == {"PLAY", "KMTS"}
    with patch("urllib.request.urlopen", lambda req, timeout=15: _Resp(b'{"data": {"rows": []}}')):
        assert nd.fetch_nasdaq_earnings_symbols("2026-09-14") is None
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert nd.fetch_nasdaq_earnings_symbols("2026-09-14") is None


def test_the_calendar_job_path_calls_the_check():
    import inspect
    src = inspect.getsource(cd.build_calendar_day)
    assert "fetch_nasdaq_earnings_symbols(date_iso)" in src
    assert src.index("fetch_earnings_calendar_all(date_iso)") < src.index("fetch_nasdaq_earnings_symbols(date_iso)")


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
