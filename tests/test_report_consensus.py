"""The consensus for a report, recorded before it (report/report_consensus.py,
2026-11-14). Two runs of one AMAT question gave two revenue estimates read
off news lines; the figure is now stored while Yahoo still lists it."""
from datetime import date, datetime, timezone

import db
from discord_bot import ask_router as R
from discord_bot import stock_body as B
from report import report_consensus as C

# Friday 2026-11-13, 3:10 PM in New York
NOW = datetime(2026, 11, 13, 20, 10, tzinfo=timezone.utc)
GROWTH = {"next_report": {"date": "2026-11-17", "session": "after the close"},
          "next_quarter": {"eps": 2.47, "eps_year_ago": 1.30, "revenue": 108.8e9, "revenue_year_ago": 57.0e9}}


def test_a_report_is_recordable_until_its_time_comes():
    assert C.capturable("2026-11-17", "after the close", NOW)
    assert C.capturable("2026-11-13", "amc", NOW), "today after the close, and it is 3:10 PM"
    after_close = datetime(2026, 11, 13, 21, 5, tzinfo=timezone.utc)
    assert not C.capturable("2026-11-13", "amc", after_close)
    assert not C.capturable("2026-11-13", "bmo", NOW) and not C.capturable("2026-11-13", "", NOW)
    assert not C.capturable("2026-11-12", "amc", NOW) and not C.capturable("soon", "", NOW)


def test_ask_records_a_stock_close_to_its_report_and_nothing_after():
    row = C.from_growth("nvda", GROWTH, NOW)
    assert row == {"symbol": "NVDA", "report_date": "2026-11-17", "eps": 2.47, "eps_year_ago": 1.30,
                   "revenue": 108.8e9, "revenue_year_ago": 57.0e9}
    # once per six hours for one stock's report, not on every question
    C._recorded.clear()
    assert C.due(row, clock=0.0) and not C.due(row, clock=3600.0) and C.due(row, clock=7 * 3600.0)
    far = {**GROWTH, "next_report": {"date": "2027-02-10"}}
    assert C.from_growth("NVDA", far, NOW) is None, "three months out: the estimates will move"
    assert C.from_growth("NVDA", {"next_report": GROWTH["next_report"]}, NOW) is None
    # the stale "quarter to be reported next" is dropped by growth_block after
    # a report, so nothing is left to record
    assert C.from_growth("NVDA", {"last_report": {"date": "2026-11-12"}}, NOW) is None


def test_the_stored_estimate_goes_on_the_last_report(tmp_path, monkeypatch):
    monkeypatch.setattr(db.settings, "db_path", str(tmp_path / "t.db"))
    db.reset_connections()
    try:
        db.upsert_report_consensus("amat", "2026-11-12", eps=4.05, revenue=10.30e9, next_revenue=10.87e9)
        # a later record without the next quarter keeps the one stored
        db.upsert_report_consensus("AMAT", "2026-11-12", eps=4.06, revenue=10.31e9)
        row = db.get_report_consensus("AMAT", "2026-11-13")
        assert row["report_date"] == "2026-11-12", "a day apart is the same report"
        assert row["revenue"] == 10.31e9 and row["next_revenue"] == 10.87e9
        assert db.get_report_consensus("AMAT", "2026-11-20") is None
        assert db.get_report_consensus("CSCO", "2026-11-12") is None
    finally:
        db.reset_connections()
    snap = {"status": "ok", "symbol": "AMAT", "growth": {"last_report": {
        "date": "2026-11-12", "session": "after the close", "actual": 4.16, "estimate": 4.05,
        "revenue": 10.409e9, "revenue_year_ago": 6.8e9}}}
    assert C.needs(snap, today=date(2026, 11, 13)) == ("AMAT", "2026-11-12")
    filled = C.apply(snap, row)
    assert filled["growth"]["last_report"]["revenue_estimate"] == 10.31e9
    assert "revenue_estimate" not in snap["growth"]["last_report"], "the cached snapshot is not changed"
    # A record whose EPS consensus is not the estimate the report was set
    # against belongs to another quarter (taken after Yahoo moved on, or for
    # a wrong date): its revenue figure is not used.
    other_quarter = dict(row, eps=4.35, revenue=11.1e9)
    assert C.apply(snap, other_quarter) is snap
    # nothing stored, an old report, or Yahoo's own estimate still there: no lookup
    assert C.apply(snap, None) is snap
    assert C.needs(snap, today=date(2027, 6, 1)) is None
    assert C.needs(filled, today=date(2026, 11, 13)) is None
    # and the result line states the beat from it, with no news line involved
    fresh = R.fresh_print(filled, today=date(2026, 11, 13))
    body = B.build("ticker_opinion", "AMAT", {R.T_SNAPSHOT: filled}, fresh=fresh, today=date(2026, 11, 13))
    assert body["lines"][1] == ("→ Revenue **$10.41B** beat the **$10.31B** estimate by **1.0%** "
                                "and rose **53.1%** from a year earlier.")


def test_the_daily_run_records_the_coming_reports_largest_first():
    feed = {
        "2026-11-13": [{"symbol": "AMAT", "hour": "amc", "revenueEstimate": 10.3e9},
                       {"symbol": "DE", "hour": "bmo", "revenueEstimate": 9e9}],        # reported this morning
        "2026-11-14": [], "2026-11-15": [],
        "2026-11-16": [{"symbol": "TINY", "hour": "bmo", "revenueEstimate": 4e6},       # under the floor
                       {"symbol": "7203.T", "hour": "", "revenueEstimate": 9e10},       # not a US symbol
                       {"symbol": "HD", "hour": "bmo", "revenueEstimate": 41e9},
                       {"symbol": "XPEV", "hour": "bmo", "revenueEstimate": 2e9}],
    }
    stored = []
    out = C.capture(now=NOW, fetch_rows=lambda iso: feed.get(iso),
                    yahoo=lambda sym: None if sym == "XPEV" else {"eps": 1.0, "revenue": 2.0},
                    store=lambda **kw: stored.append((kw["symbol"], kw["report_date"])), pause_s=0)
    assert stored == [("AMAT", "2026-11-13"), ("HD", "2026-11-16")]
    assert out["stored"] == 2 and out["failed"] == 1 and out["names"] == 3 and out["feed_down"] == []
    # the calendar feed down for a day is reported, not read as "no reports"
    down = C.capture(now=NOW, fetch_rows=lambda iso: None, yahoo=lambda s: None,
                     store=lambda **kw: None, pause_s=0)
    assert down["dates"] == [] and len(down["feed_down"]) == C.DAYS_AHEAD + 1


def test_a_yahoo_read_that_raises_costs_one_company_not_the_run():
    def yahoo(sym):
        if sym == "BAD":
            raise RuntimeError("429")
        return {"eps": 1.0}
    feed = [{"symbol": "BAD", "hour": "amc", "revenueEstimate": 9e9},
            {"symbol": "GOOD", "hour": "amc", "revenueEstimate": 8e9}]
    stored = []
    out = C.capture(now=NOW, fetch_rows=lambda iso: feed if iso == "2026-11-13" else [],
                    yahoo=yahoo, store=lambda **kw: stored.append(kw["symbol"]), pause_s=0)
    assert stored == ["GOOD"] and out["failed"] == 1
