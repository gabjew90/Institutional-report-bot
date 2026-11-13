"""The code-built lines for "when does X report" (discord_bot/earnings_block.py),
from the 2026-11-05 audit's figure sample. Where they are posted is
discord_bot/stock_body.py (tests/test_stock_body.py)."""
from datetime import date

from discord_bot import answer_block, earnings_block as eb


def _amd():
    edate = {"status": "ok", "symbol": "AMD", "next": {
        "date": "2026-11-03", "timing": "after market close",
        "when": "tomorrow (Tue Nov 3), after market close",
        "eps_estimate": 1.97, "revenue_estimate": 13.22e9,
        "date_status": "listed by both the calendar feed and Yahoo"}}
    snap = {"status": "ok", "growth": {
        "next_report": {"date": "2026-11-03", "session": "after the close"},
        "next_quarter": {"eps": 1.96, "eps_year_ago": 1.20, "revenue": 13.09e9,
                         "revenue_year_ago": 9.246e9},
        "last_report": {"date": "2026-08-04", "actual": 1.05, "estimate": 1.02}}}
    price = {"status": "ok", "session": "REGULAR", "quotes": [
        {"symbol": "AMD", "price": 503.61, "change_pct": -0.24, "data_freshness": "live_regular_session"}]}
    return edate, snap, price


def test_consensus_and_its_growth_come_from_one_source():
    # 2026-11-02: "$13.22B (+41.6% y/y)" put the calendar's revenue beside
    # the growth of Yahoo's $13.09B
    edate, snap, price = _amd()
    lines = eb.block_lines(edate, snap, price)
    assert lines[0] == "→ **AMD** reports tomorrow (Tue Nov 3), after market close."
    assert "revenue **$13.09B** (+41.6% y/y)" in lines[1]
    assert "EPS **$1.96** (+63.3% y/y)" in lines[1]
    assert "13.22" not in "\n".join(lines)
    assert lines[2] == "→ Last report (Aug 4): EPS **$1.05**, beat the **$1.02** estimate."
    assert lines[3] == "→ Trading at **$503.61**, -0.2% today."


def test_a_different_yahoo_quarter_leaves_the_calendar_figures_without_growth():
    edate, snap, _ = _amd()
    snap["growth"]["next_report"]["date"] = "2027-02-03"
    lines = eb.block_lines(edate, snap, None)
    assert lines[1] == "→ Consensus: EPS **$1.97** on revenue **$13.22B**."


def test_the_weekday_is_the_computed_one():
    # 2026-11-03: "Wednesday, November 5" for Main Street; Nov 5 was a Thursday
    edate = {"status": "ok", "symbol": "MAIN", "next": {
        "date": "2026-11-05", "when": "in 2 days (Thu Nov 5), after market close",
        "eps_estimate": 0.92}}
    lines = eb.block_lines(edate, None, None)
    assert lines[0] == "→ **MAIN** reports in 2 days (Thu Nov 5), after market close."
    assert lines[1] == "→ Consensus: EPS **$0.92**."


def test_a_projected_date_says_so():
    edate, snap, _ = _amd()
    edate["next"]["date_status"] = "projected, not confirmed: say the date is expected, not set"
    assert "is expected to report" in eb.block_lines(edate, snap, None)[0]


def test_a_report_already_out_or_not_listed_gives_no_lines():
    edate, snap, price = _amd()
    out = dict(edate, next=dict(edate["next"], when="today (Tue Nov 3), after market close "
                                                    "(already released: the results are out)"))
    assert eb.block_lines(out, snap, price) == []
    assert eb.block_lines(dict(edate, next=None), snap, price) == []
    assert eb.block_lines(edate, snap, price)


def test_after_hours_and_stale_quotes_are_labelled():
    ah = {"status": "ok", "session": "AFTER-HOURS", "quotes": [
        {"symbol": "AMD", "price": 480.0, "change_pct": -0.5, "regular_session_close": 500.0,
         "extended_hours_change_pct": -4.0, "data_freshness": "live_extended_hours"}]}
    assert eb.price_line(ah, "AMD") == "→ Closed at **$500.00** (-0.5% on the day), **$480.00** after hours, -4.0%."
    stale = {"status": "ok", "session": "WEEKEND-CLOSED", "quotes": [
        {"symbol": "AMD", "price": 500.0, "change_pct": 1.2, "data_freshness": "regular_session_close"}]}
    assert eb.price_line(stale, "AMD") == "→ Last close **$500.00** (+1.2% that session)."


def test_the_date_lines_are_no_longer_an_answer_block():
    """They are posted by the stock body on every date question; the
    opening-above-lines block stays for lists and calculations."""
    edate, snap, price = _amd()
    ctx = {"shape": "earnings_date", "snapshot": snap, "price": price}
    assert answer_block.build("lookup_earnings_date", edate, date(2026, 11, 2),
                              question="when does amd report", context=ctx) is None
