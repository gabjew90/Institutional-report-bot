"""lookup_earnings_slate — the missing tool (2026-09-01).

The room asked "who reports after close today" three times in one
afternoon and got three different PARTIAL answers, each missing PANW,
the largest name on the slate. Every one of those turns called NO
tool: lookup_earnings_date's own docs routed broad sweeps to Google,
and a search snippet is a partial list by construction (one answer
cited digrin.com).
"""
import asyncio
import sys

from discord_bot import bot as B


def _run(args, raw, caps, nasdaq=None):
    from report import news_data as nd
    from report import calendar_data as cd
    o_raw, o_caps, o_nq = nd.fetch_earnings_calendar_all, cd.cached_caps, nd.fetch_nasdaq_earnings_rows
    try:
        nd.fetch_earnings_calendar_all = lambda d: raw
        # since 2026-10-18 the slate reads cached caps only, and Nasdaq's
        # calendar through calendar_data.reconcile_earnings (audit A1/A2)
        cd.cached_caps = lambda syms: dict(caps)
        nd.fetch_nasdaq_earnings_rows = lambda d: nasdaq
        return asyncio.run(B._execute_earnings_slate(args))
    finally:
        nd.fetch_earnings_calendar_all = o_raw
        cd.cached_caps = o_caps
        nd.fetch_nasdaq_earnings_rows = o_nq


CAPS = {
    "PANW": {"cap": 311_436.0, "name": "Palo Alto Networks"},
    "DELL": {"cap": 295_623.0, "name": "Dell Technologies"},
    "MDB": {"cap": 36_465.0, "name": "MongoDB"},
    "TINY": {"cap": 12.0, "name": "Tiny Co"},
}
RAW = [{"symbol": "MDB", "hour": "amc"}, {"symbol": "TINY", "hour": "amc"},
       {"symbol": "PANW", "hour": "amc"}, {"symbol": "DELL", "hour": ""}]


def test_the_missing_name_is_present_and_ranked_first():
    """PANW is the largest name and was omitted from every search-based
    answer. It must lead the slate."""
    r = _run({"date": "2026-09-01"}, RAW, CAPS)
    assert r["status"] == "ok"
    syms = [x["symbol"] for x in r["after_close"]]
    assert syms[0] == "PANW", syms
    assert "MDB" in syms and "DELL" in syms


def test_unconfirmed_session_is_reported_not_dropped():
    """DELL has a blank hour here. The tool must still return it —
    dropping unconfirmed names is exactly the failure this fixes — and
    must flag the session so the answer can say so."""
    r = _run({"date": "2026-09-01"}, RAW, CAPS)
    dell = next(x for x in r["after_close"] if x["symbol"] == "DELL")
    assert dell["session_confirmed"] is False


def test_cap_ranking_is_descending():
    r = _run({"date": "2026-09-01"}, RAW, CAPS)
    caps = [x["market_cap_musd"] for x in r["after_close"]]
    assert caps == sorted(caps, reverse=True), caps


def test_feed_down_is_an_error_not_an_empty_slate():
    """None from the feed means 'unavailable', and the tool must say so
    — an empty slate would read as 'nobody reports today', which is the
    lie the calendar's own fallback exists to prevent."""
    r = _run({"date": "2026-09-01"}, None, CAPS)
    assert r["status"] == "error"


def test_genuinely_empty_date_is_distinct_from_a_failure():
    r = _run({"date": "2026-09-01"}, [], CAPS)
    assert r["status"] == "empty"


def test_duplicate_symbols_collapse():
    r = _run({"date": "2026-09-01"},
             [{"symbol": "MDB", "hour": "amc"},
              {"symbol": "MDB", "hour": "amc"}], CAPS)
    assert [x["symbol"] for x in r["after_close"]] == ["MDB"]


def test_both_sessions_are_returned():
    r = _run({"date": "2026-09-01"},
             [{"symbol": "PANW", "hour": "bmo"},
              {"symbol": "MDB", "hour": "amc"}], CAPS)
    assert [x["symbol"] for x in r["before_open"]] == ["PANW"]
    assert [x["symbol"] for x in r["after_close"]] == ["MDB"]
    assert r["counts"] == {"before_open": 1, "after_close": 1}


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


def test_a_finnhub_date_nasdaq_does_not_list_is_dropped_and_nasdaq_only_added():
    """2026-10-18 audit A2: Finnhub had GE on Monday 10/19 (it reports 10/20)
    and no SpaceX; the slate read Finnhub alone."""
    nasdaq = {"FITB": {"hour": "bmo", "cap": 45662.0, "name": "Fifth Third"},
              "SPCX": {"hour": "amc", "cap": 900000.0, "name": "SpaceX"}}
    r = _run({"date": "2026-10-19"},
             [{"symbol": "GE", "hour": "bmo"}, {"symbol": "FITB", "hour": "bmo"}],
             {"GE": {"cap": 300000.0, "name": "GE Aerospace"}}, nasdaq=nasdaq)
    assert [x["symbol"] for x in r["before_open"]] == ["FITB"]
    assert [x["symbol"] for x in r["after_close"]] == ["SPCX"]
    assert r["before_open"][0]["market_cap_musd"] == 45662   # Nasdaq's dollar cap
    assert r["dates_checked_against_nasdaq"] is True


def test_a_week_is_one_call_with_every_weekday():
    from datetime import datetime
    from discord_bot import ask_tools as T
    now = datetime(2026, 10, 16, 9, 30)                  # Friday
    assert T._slate_dates("next_week", now) == [
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    assert T._slate_dates("this_week", now) == ["2026-10-16"]
    r = _run({"date": "next_week"}, RAW, CAPS)
    assert r["status"] == "ok" and len(r["days"]) == 5
    assert all(d["after_close"][0]["symbol"] == "PANW" for d in r["days"])


def test_this_week_on_a_weekend_is_the_coming_week():
    """Code review 2026-10-18: Saturday's "this week" was an empty list and an IndexError."""
    from datetime import datetime
    from discord_bot import ask_tools as T
    sat = datetime(2026, 10, 17, 10, 0)
    assert T._slate_dates("this_week", sat) == [
        "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
