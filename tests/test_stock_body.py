"""One code-built body for a turn about one stock (discord_bot/stock_body.py,
2026-11-13). The lines follow the stock's state and what was asked, on every
single-stock route, and the model writes only what no feed carries."""
from datetime import date

from discord_bot import ask_router as R
from discord_bot import stock_body as B
from discord_bot import stock_outline as O

TODAY = date(2026, 11, 2)
EDATE = {"status": "ok", "symbol": "AMD", "next": {
    "date": "2026-11-03", "timing": "after market close",
    "when": "tomorrow (Tue Nov 3), after market close",
    "eps_estimate": 1.97, "revenue_estimate": 13.22e9,
    "date_status": "listed by both the calendar feed and Yahoo"}}
SNAP = {"status": "ok", "symbol": "AMD", "market_cap": 8.2e11,
        "shares_outstanding": 1.63e9, "float_shares": 1.62e9, "short_pct_float": 2.4,
        "short_interest_as_of": "2026-10-15",
        "growth": {"next_report": {"date": "2026-11-03", "session": "after the close"},
                   "next_quarter": {"eps": 1.96, "eps_year_ago": 1.20, "revenue": 13.09e9,
                                    "revenue_year_ago": 9.246e9},
                   "last_report": {"date": "2026-08-04", "session": "after the close", "actual": 1.05,
                                   "estimate": 1.02, "revenue": 10.1e9, "revenue_year_ago": 7.7e9}}}
PRICE = {"status": "ok", "session": "REGULAR", "quotes": [
    {"symbol": "AMD", "price": 503.61, "change_pct": -0.24, "data_freshness": "live_regular_session"}]}
PRIMER = {"status": "ok", "primer": "SELLS: processors\nDRIVERS: Data Center drives growth on AI accelerator "
                                    "demand\nWATCHED: data center revenue and gross margin"}
RESEARCH = {"status": "ok", "notes": [
    {"source": "JPMorgan", "published": "2026-10-30",
     "calls": [{"action": "positive_catalyst_watch", "rating": "Overweight", "price_target": "$600",
                "rationale": "Expects a data center beat", "conviction": "high"}]}]}
NEWS = {"status": "ok", "digest": "- 2026-11-01: AMD signed a supply deal with a cloud provider (Reuters)"}
FRESH = {"date": "2026-11-02", "session": "before the open", "actual": 2.10, "estimate": 1.96,
         "surprise_pct": 7.1, "eps_year_ago": 1.20, "revenue": 13.4e9, "revenue_year_ago": 9.246e9,
         "revenue_source": "yahoo", "symbol": "AMD"}

DATE_LINE = "→ **AMD** reports tomorrow (Tue Nov 3), after market close."
CONSENSUS = "→ Consensus: EPS **$1.96** (+63.3% y/y) on revenue **$13.09B** (+41.6% y/y)."
LAST = "→ Last report (Aug 4): EPS **$1.05**, beat the **$1.02** estimate."
QUOTE = "→ **AMD** at **$503.61**, -0.2% today."


def _results(**extra):
    base = {R.T_EDATE: EDATE, R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE}
    base.update(extra)
    return base


def _slots(text):
    return [ln.split(". ", 1)[1].split(":")[0] for ln in text.splitlines() if ln[:2].rstrip(".").isdigit()]


def test_a_date_lookup_leads_with_the_date_and_ends_with_the_price():
    body = B.build("earnings_date", "amd", _results(), asked="when does amd report", today=TODAY)
    assert body["lines"] == [DATE_LINE, CONSENSUS, LAST, "→ Trading at **$503.61**, -0.2% today."]
    assert body["state"] == "none" and body["has_price"]
    # the lines are the answer: no slots, and the model is not asked for a rundown
    text = O.build_outline(_results(**{R.T_PRIMER: PRIMER}), "earnings_date", "AMD",
                           asked="when does amd report", posted=body)
    assert _slots(text) == [] and DATE_LINE in text
    assert "If the question asks for something the lines do not answer" in text


def test_did_it_beat_is_answered_from_the_last_report_when_no_print_is_fresh():
    """The gap this module closes: "did AMD beat" got no code-built line at
    all, on the date route or as a reply."""
    body = B.build("earnings_date", "AMD", _results(), asked="did amd beat", today=TODAY)
    assert body["state"] == "last"
    assert body["lines"] == [
        "→ **AMD** reported Aug 4 after the close: EPS **$1.05** beat the **$1.02** estimate by **2.9%**.",
        "→ Revenue **$10.10B**, up **31.2%** from a year earlier.",
        "→ Reports tomorrow (Tue Nov 3), after market close.",
        CONSENSUS,
        "→ Trading at **$503.61**, -0.2% today."]
    # a plain date lookup does not get the older report's lines
    assert B.build("earnings_date", "AMD", _results(), asked="when is amd earnings", today=TODAY)["state"] == "none"


def test_a_print_is_posted_on_the_date_route_too():
    out = dict(EDATE, next=dict(EDATE["next"], when="today (Mon Nov 2), before market open "
                                                    "(already released: the results are out)"))
    body = B.build("earnings_date", "AMD", _results(**{R.T_EDATE: out}), fresh=FRESH,
                   asked="did amd beat", today=TODAY)
    assert body["state"] == "print" and body["eps"] == 2.10
    assert body["lines"] == [
        "→ **AMD** reported Nov 2 before the open: EPS **$2.10** beat the **$1.96** estimate by **7.1%** "
        "and rose **75.0%** from a year earlier.",
        "→ Revenue **$13.40B**, up **44.9%** from a year earlier.",
        "→ Trading at **$503.61**, -0.2% today."]


def test_after_a_print_only_a_when_question_gets_the_next_date():
    """2026-11-13 harness: "did amat beat" the day after the report was
    answered with the February date and its consensus under the result."""
    nxt = dict(EDATE, next={"date": "2027-02-02", "when": "in 92 days (Tue Feb 2), time not announced",
                            "eps_estimate": 2.3, "date_status": "projected, not confirmed"})
    did = B.build("earnings_date", "AMD", _results(**{R.T_EDATE: nxt}), fresh=FRESH,
                  asked="did amd beat", today=TODAY)
    assert "upcoming" not in did["kinds"] and did["lines"][-1].startswith("→ Trading at")
    when = B.build("earnings_date", "AMD", _results(**{R.T_EDATE: nxt}), fresh=FRESH,
                   asked="when does amd report next", today=TODAY)
    assert any(ln.startswith("→ Expected to report in 92 days") for ln in when["lines"])


def test_a_view_leads_with_the_price_and_gives_the_next_report_in_one_line():
    results = {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE, R.T_PRIMER: PRIMER, R.T_RESEARCH: RESEARCH,
               R.T_NEWS: NEWS}
    body = B.build("ticker_opinion", "AMD", results, asked="what do you think of amd", today=TODAY)
    assert body["lines"] == [
        QUOTE,
        "→ Reports Tue Nov 3, after the close. Consensus: EPS **$1.96** (+63.3% y/y) on revenue "
        "**$13.09B** (+41.6% y/y)."]
    text = O.build_outline(results, "ticker_opinion", "AMD", asked="what do you think of amd", posted=body)
    assert _slots(text) == ["NEWS", "DRIVER", "DESK"]
    assert "PRICE:" not in text and "UPCOMING" not in text and "POSITIONING" not in text
    assert "The stock's move comes from the posted lines only." in text
    assert "answer it in one more arrow from the blocks above" in text
    # with the earnings feed in the route, its wording of the date is the one posted
    with_feed = B.build("ticker_opinion", "AMD", dict(results, **{R.T_EDATE: EDATE}), today=TODAY)
    assert with_feed["lines"][1].startswith("→ Reports tomorrow (Tue Nov 3), after market close. Consensus: EPS")


def test_a_report_date_already_past_is_not_given_as_coming():
    late = date(2026, 11, 9)
    body = B.build("ticker_opinion", "AMD", {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE}, today=late)
    assert body["lines"] == [QUOTE]


def test_a_report_out_but_not_in_the_feeds_posts_no_result_line():
    pending = {"date": "2026-11-02", "session": "before the open", "actual": None, "estimate": 1.96,
               "symbol": "AMD"}
    results = {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE, R.T_PRIMER: PRIMER, R.T_NEWS: {
        "status": "ok", "digest": "- 2026-11-02: AMD reported revenue of $13.4 billion and EPS of $2.10; "
                                  "guided fourth-quarter revenue to $14 billion (Reuters)"}}
    body = B.build("ticker_opinion", "AMD", results, fresh=pending, today=TODAY)
    assert body["state"] == "pending" and body["lines"] == [QUOTE]
    text = O.build_outline(results, "ticker_opinion", "AMD", fresh=pending, posted=body)
    assert _slots(text) == ["RESULT", "GUIDANCE", "DRIVER"]


def test_a_date_question_in_the_first_hours_after_a_report_still_gets_a_body():
    """2026-10-07: "what time apld report earnings" at 4:20 PM ET. The feeds
    have no figure yet, so the price is posted and the result is the
    model's from the news."""
    pending = {"date": "2026-11-02", "session": "before the open", "actual": None, "estimate": 1.96,
               "symbol": "AMD"}
    out = dict(EDATE, next=dict(EDATE["next"], when="today (Mon Nov 2), before market open "
                                                    "(already released: the results are out)"))
    results = _results(**{R.T_EDATE: out, R.T_NEWS: {
        "status": "ok", "digest": "- 2026-11-02: AMD reported revenue of $13.4 billion (Reuters)"}})
    body = B.build("earnings_date", "AMD", results, fresh=pending, asked="what time does amd report",
                   today=TODAY)
    assert body["lines"] == [QUOTE] and body["state"] == "pending"
    text = O.build_outline(results, "earnings_date", "AMD", fresh=pending, posted=body)
    assert _slots(text) == ["RESULT"]


def test_a_print_the_feeds_carry_without_revenue_still_posts_the_eps():
    """Foreign filers and a late feed: the revenue comes from the news as the
    model's arrow, the EPS line is still code's."""
    no_rev = {k: v for k, v in FRESH.items() if not k.startswith("revenue")}
    results = {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE, R.T_NEWS: {
        "status": "ok", "digest": "- 2026-11-02: AMD reported revenue of $13.4 billion, up 45% (Reuters)"}}
    body = B.build("ticker_opinion", "AMD", results, fresh=no_rev, today=TODAY)
    assert [ln[:14] for ln in body["lines"]] == ["→ **AMD** at *", "→ Reported Nov"]
    assert "revenue" not in body["kinds"] and body["revenue"] is None
    text = O.build_outline(results, "ticker_opinion", "AMD", fresh=no_rev, posted=body)
    assert _slots(text) == ["RESULT DETAIL"] and "$13.4 billion" in text


def test_an_options_question_gets_the_straddles_move_as_a_line():
    chain = {"status": "ok", "summary": {"implied_move_pct": 7.1, "implied_move_dollars": 13.1,
                                         "expiration_iso": "2026-11-06", "live_quotes": True}}
    line = ("→ Options are pricing a move of about **7.1%** (**$13.10**) either way by the Nov 6 "
            "expiration, from the at-the-money straddle.")
    assert B.options_line(chain) == line
    body = B.build("options_chain", "AMD", _results(**{R.T_CHAIN: chain}), today=TODAY)
    assert body["lines"][-1] == line and "options" in body["kinds"]
    # another shape does not carry it
    assert "options" not in B.build("ticker_opinion", "AMD", _results(**{R.T_CHAIN: chain}), today=TODAY)["kinds"]
    closed = {"status": "ok", "summary": {"live_quotes": False}, "quotes_note": "Options are not quoting"}
    assert B.options_line(closed) == "→ Options are not quoting right now, so there is no implied move to read."
    last = dict(chain, as_of="2026-11-02 19:55 UTC", quotes_note="These are the last live quotes")
    assert B.options_line(last).endswith("straddle, on the last live quotes (2026-11-02 19:55 UTC).")
    assert B.options_line({"status": "error"}) == "" and B.options_line(None) == ""
    # a chain that could not be read is the model's to say
    failed = O.build_outline(_results(**{R.T_CHAIN: {"status": "error"}, R.T_PRIMER: PRIMER}), "options_chain",
                             "AMD", posted=B.build("options_chain", "AMD", _results(), today=TODAY))
    assert "OPTIONS: the options chain could not be read" in failed


def test_company_data_is_posted_only_when_the_question_asks_for_it():
    """Owner, 2026-11-13: positioning only when asked."""
    plain = B.build("ticker_opinion", "AMD", _results(), asked="thoughts on amd", today=TODAY)
    assert "company" not in plain["kinds"]
    asked = B.build("ticker_opinion", "AMD", _results(), asked="thoughts on amd, what's the short interest",
                    today=TODAY)
    assert "company" in asked["kinds"]
    line = next(ln for ln in asked["lines"] if ln.startswith("→ **Shares:**"))
    assert "2.4%" in line


def test_a_reply_is_a_narrow_question():
    results = {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE, R.T_PRIMER: PRIMER, R.T_RESEARCH: RESEARCH}
    # no print: nothing is posted unless the reply's words ask for it
    assert B.build("follow_up", "AMD", results, asked="is that priced in", today=TODAY) is None
    for other in ("when will it recover", "what did you mean by that", "did you see the chart"):
        assert B.build("follow_up", "AMD", results, asked=other, today=TODAY) is None, other
    when = B.build("follow_up", "AMD", results, asked="when do they report", today=TODAY)
    assert when["lines"] == ["→ **AMD** reports Tue Nov 3, after the close. Consensus: EPS **$1.96** "
                             "(+63.3% y/y) on revenue **$13.09B** (+41.6% y/y)."]
    did = B.build("follow_up", "AMD", results, asked="did they beat last quarter", today=TODAY)
    assert did["state"] == "last" and did["lines"][0] == QUOTE and "EPS **$1.05** beat" in did["lines"][1]
    # after a print the result is posted, as the print guard used to add it
    after = B.build("follow_up", "AMD", results, fresh=FRESH, asked="is it a buy now", today=TODAY)
    assert after["state"] == "print" and after["lines"][0] == QUOTE and "EPS **$2.10** beat" in after["lines"][1]
    # and the outline asks for no rundown
    text = O.build_outline(results, "follow_up", "AMD", fresh=FRESH, asked="is it a buy now", posted=after)
    assert _slots(text) == [] and "DESK:" not in text


def test_not_a_stock_or_not_a_stock_shape_gets_no_body():
    assert B.build("ticker_opinion", "SPY", {R.T_PRICE: PRICE}, today=TODAY) is None
    assert B.build("ticker_opinion", "BTC", {R.T_PRICE: PRICE}, today=TODAY) is None
    assert B.build("price", "AMD", _results(), today=TODAY) is None
    assert B.build("ticker_opinion", "", _results(), today=TODAY) is None
    assert B.build("ticker_opinion", "AMD", {}, today=TODAY) is None
    # and no outline without a body on a route that never had one
    assert O.build_outline(_results(**{R.T_PRIMER: PRIMER, R.T_RESEARCH: RESEARCH}), "earnings_date", "AMD") == ""


def test_the_snapshots_price_stands_in_when_the_route_fetched_no_quote():
    body = B.build("options_chain", "AMD", {R.T_SNAPSHOT: {**SNAP, "price": 217.95}}, today=TODAY)
    assert body["lines"][0] == "→ **AMD** at **$217.95**."


def test_compose_drops_an_arrow_that_only_restates_the_lines():
    body = B.build("ticker_opinion", "AMD", {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE}, fresh=FRESH, today=TODAY)
    driver = "→ Data Center carried the quarter on demand for AI accelerators."
    target = "→ JPMorgan raised its target to **$600** from **$540** (Barron's)"
    margin = "→ EPS of **$2.10** came with gross margin of **54.5%**"
    answer = ("→ **$AMD** is at **$503.61**, down 0.2%\n\n→ EPS of **$2.10** beat estimates\n\n"
              "→ Revenue of **$13.40B** was up from last year\n\n"
              + driver + "\n\n" + target + "\n\n" + margin)
    out, dropped = B.compose(answer, body)
    # the margin arrow repeats the EPS but carries a figure of its own: kept
    # and each kept arrow ends with a full stop, as the posted lines do
    assert out == "\n\n".join(body["lines"] + [driver, target + ".", margin + "."]) and dropped == 3
    # the bot's own no-answer line is not an arrow to keep
    assert B.compose("→ No response came back (reason: MAX_TOKENS). Try again or rephrase.", body) == (
        "\n\n".join(body["lines"]), 0)


def test_the_old_guards_stand_down_when_the_body_posted_their_lines():
    """First push: the print and implied-move guards stay as the net for a
    turn where the body failed to build, and add nothing when it did."""
    from discord_bot import bot
    body = B.build("ticker_opinion", "AMD", {R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE}, fresh=FRESH, today=TODAY)
    ans = "→ Data Center drove **$7.6B** of it"
    meta = {"guards": [], "route_shape": "ticker_opinion", "route_prefetch": [], "fresh_print": FRESH,
            "stock_body": body}
    assert bot._fresh_print_guard(ans, meta) == ans and meta["guards"] == []
    meta.pop("stock_body")
    assert bot._fresh_print_guard(ans, meta) != ans
    move = {"guards": [], "route_shape": "options_chain",
            "route_prefetch": ["lookup_options_chain", "lookup_earnings_date"],
            "chain_move": {"pct": 7.1, "dollars": 13.1, "expiration": "2026-11-06"}, "stock_body": body}
    assert bot._implied_move_guard(ans, move) == ans
    move.pop("stock_body")
    assert "7.1%" in bot._implied_move_guard(ans, move)


def test_the_printed_eps_is_a_posted_line_on_every_route_the_print_guard_covers():
    led = ("→ **AMD** reported Nov 2 before the open: EPS **$2.10** beat the **$1.96** estimate by **7.1%** "
           "and rose **75.0%** from a year earlier.")
    for shape in sorted(B.SHAPES):
        body = B.build(shape, "AMD", {R.T_SNAPSHOT: SNAP}, fresh=FRESH, today=TODAY)
        assert body["lines"][0] == led and body["state"] == "print", shape
    from discord_bot import bot
    assert bot._FRESH_PRINT_SHAPES == set(B.SHAPES)
