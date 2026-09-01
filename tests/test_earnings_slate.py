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


def _run(args, raw, caps):
    from report import news_data as nd
    from report import calendar_data as cd
    o_raw, o_caps = nd.fetch_earnings_calendar_all, cd._resolve_caps
    try:
        nd.fetch_earnings_calendar_all = lambda d: raw
        cd._resolve_caps = lambda syms: caps
        return asyncio.run(B._execute_earnings_slate(args))
    finally:
        nd.fetch_earnings_calendar_all = o_caps and o_raw
        cd._resolve_caps = o_caps


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
