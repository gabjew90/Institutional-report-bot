"""A large name Nasdaq lacks for a date is kept only when Nasdaq lists it on
no nearby date (report/calendar_data.py, 2026-11-17, audit run 12 C1/C2/A11).

The 11-17 sheet and its X post listed QXO, which had reported on 11-12. The
11-16 sheet listed PDD and the 11-17 sheet WSM, both on Nasdaq for 11-19.
Each was a Finnhub row with no cached cap and a $1B+ revenue estimate, kept
by the rule written for SpaceX ("Nasdaq has not listed it yet") without
looking at any other day of Nasdaq's calendar."""
from unittest.mock import patch

import db
from report import calendar_data as cd
from report import news_data as nd

GE = {"hour": "bmo", "cap": 303330.0, "name": "GE Aerospace"}
RAW = [{"symbol": "GE", "hour": "bmo"},
       {"symbol": "QXO", "hour": "amc", "revenueEstimate": 2.7e9},
       {"symbol": "WSM", "hour": "bmo", "revenueEstimate": 2.0e9},
       {"symbol": "SPCX", "hour": "amc", "revenueEstimate": 7.4e9},
       {"symbol": "TINYQ", "hour": "amc", "revenueEstimate": 3e6}]


def _reconcile(listed_elsewhere=None, raw=RAW):
    with patch.object(db, "get_market_caps", lambda syms, **kw: {}):
        return cd.reconcile_earnings("2026-11-17", [dict(r) for r in raw], {"GE": GE},
                                     lambda syms: {}, listed_elsewhere=listed_elsewhere)


def test_a_name_nasdaq_lists_on_another_date_is_dropped():
    rec = _reconcile(lambda d, syms: {"QXO": "2026-11-12", "WSM": "2026-11-19"})
    assert rec["bmo"] == ["GE"] and rec["amc"] == ["SPCX"]
    assert rec["listed_elsewhere"] == {"QXO": "2026-11-12", "WSM": "2026-11-19"}
    # SpaceX is on none of Nasdaq's nearby dates: still kept, still flagged
    assert rec["finnhub_only_kept"] == ["SPCX"] and rec["confirmed"]["SPCX"] is False
    assert rec["date_unconfirmed"] == ["QXO", "TINYQ", "WSM"]


def test_only_the_large_unlisted_names_are_looked_up():
    asked = []
    _reconcile(lambda d, syms: asked.append((d, sorted(syms))) or {})
    assert asked == [("2026-11-17", ["QXO", "SPCX", "WSM"])]
    # nothing large is missing: no lookup at all
    asked.clear()
    _reconcile(lambda d, syms: asked.append(1) or {}, raw=RAW[:1] + RAW[-1:])
    assert asked == []


def test_a_failed_lookup_keeps_the_names_as_before():
    def boom(d, syms):
        raise OSError("nasdaq down")
    rec = _reconcile(boom)
    assert rec["finnhub_only_kept"] == ["QXO", "SPCX", "WSM"] and rec["listed_elsewhere"] == {}


def test_nearby_dates_are_read_from_nasdaqs_calendar():
    tables = {"2026-11-12": {"QXO": {"hour": "", "cap": 13290.0, "name": "QXO, Inc."}},
              "2026-11-19": {"WSM": {"hour": "bmo", "cap": 29921.0, "name": "Williams-Sonoma"},
                             "PDD": {"hour": "bmo", "cap": 112405.0, "name": "PDD Holdings"}},
              "2026-12-01": {"WSM": {"hour": "bmo", "cap": 29921.0, "name": "Williams-Sonoma"}},
              "2026-12-02": {"SPCX": {"hour": "amc", "cap": 2e6, "name": "SpaceX"}}}   # past the window
    read = []

    def fetch(d):
        read.append(d)
        return tables.get(d)
    with patch.object(nd, "fetch_nasdaq_earnings_rows", fetch):
        found = cd.nasdaq_listed_elsewhere("2026-11-17", ["qxo", "WSM", "SPCX"])
        assert found == {"QXO": "2026-11-12", "WSM": "2026-11-19"}        # the nearest date wins
        assert "2026-11-17" not in read and "2026-11-14" not in read      # not the date itself, no weekends
        assert min(read) == "2026-11-03" and max(read) == "2026-12-01"
        # a second question in the same minutes reads nothing again
        n = len(read)
        assert cd.nasdaq_listed_elsewhere("2026-11-17", ["PDD"]) == {"PDD": "2026-11-19"}
        assert len(read) == n
    assert cd.nasdaq_listed_elsewhere("2026-11-17", []) == {}
    assert cd.nasdaq_listed_elsewhere("not a date", ["QXO"]) == {}


def test_the_sheet_and_the_slate_both_get_the_rule():
    """Through the default lookup, with Nasdaq's other days stubbed."""
    def fetch(d):
        return {"2026-11-17": {"GE": GE},
                "2026-11-12": {"QXO": {"hour": "", "cap": 13290.0, "name": "QXO, Inc."}},
                "2026-11-19": {"WSM": {"hour": "bmo", "cap": 29921.0, "name": "Williams-Sonoma"}}}.get(d)
    with patch.object(nd, "fetch_nasdaq_earnings_rows", fetch), \
         patch.object(db, "get_market_caps", lambda syms, **kw: {}):
        rec = cd.reconcile_earnings("2026-11-17", [dict(r) for r in RAW], fetch("2026-11-17"),
                                    lambda syms: {})
    assert rec["amc"] == ["SPCX"] and sorted(rec["listed_elsewhere"]) == ["QXO", "WSM"]
    # the sheet stores both lists with the post, so an audit can tell which rule acted
    import json
    day = cd.CalendarDay(date_iso="2026-11-17", weekday_label="TUESDAY 11/17", is_holiday=False,
                         finnhub_only_kept=rec["finnhub_only_kept"], listed_elsewhere=rec["listed_elsewhere"])
    stored = json.loads(cd.lineup_json(day))
    assert stored["finnhub_only_kept"] == ["SPCX"]
    assert stored["listed_elsewhere"] == {"QXO": "2026-11-12", "WSM": "2026-11-19"}


def test_an_unreadable_date_is_asked_again_soon():
    """The fetch returns None for a failed read: that must not stand for hours."""
    calls = []

    def fetch(d):
        calls.append(d)
        return None
    with patch.object(nd, "fetch_nasdaq_earnings_rows", fetch):
        assert cd.nasdaq_listed_elsewhere("2026-11-17", ["QXO"]) == {}
        n = len(calls)
        assert cd.nasdaq_listed_elsewhere("2026-11-17", ["QXO"]) == {} and len(calls) == n
        for key, (at, rows) in list(cd._nearby_cache.items()):
            if key[0] is fetch:
                cd._nearby_cache[key] = (at - cd._NEARBY_TTL_NONE_S - 1, rows)
        cd.nasdaq_listed_elsewhere("2026-11-17", ["QXO"])
        assert len(calls) == 2 * n


def test_a_slow_calendar_does_not_hold_the_answer():
    import time

    def fetch(d):
        time.sleep(0.4)
        return {"QXO": {"hour": "", "cap": 13290.0, "name": "QXO, Inc."}}
    with patch.object(nd, "fetch_nasdaq_earnings_rows", fetch), patch.object(cd, "_NEARBY_BUDGET_S", 0.05):
        started = time.time()
        assert cd.nasdaq_listed_elsewhere("2026-11-17", ["QXO"]) == {}
        assert time.time() - started < 0.35
