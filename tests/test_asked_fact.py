"""A fact no feed carries, searched in code (discord_bot/asked_fact.py,
2026-11-21): the clock time of an earnings release and of its call. Code
parses the search's reply, converts the zone, checks the date against the
feeds' report date and renders the line. The model words none of it."""
from datetime import date

from discord_bot import ask_router as R
from discord_bot import asked_fact as A
from discord_bot import stock_body as B

TODAY = date(2026, 11, 17)
FACT = {"status": "ok", "symbol": "NVDA", "kind": "call", "date": "2026-11-17",
        "release": (16, 20), "call": (17, 0), "source": "NVIDIA Investor Relations",
        "sources": [{"title": "nvidia.com", "url": "https://n/1"}]}
DAYS = [("2026-11-17", "after market close")]


# ---------------------------------------------------------------- the question

def test_a_clock_time_asked_of_a_report_or_its_call_is_searched():
    for q, kind in (("what time does nbis report", "release"),
                    ("what time is Nvidia earnings call", "call"),
                    ("what time Nvidia earnings", "release"),
                    ("what time apld report earnings", "release"),
                    ("nvda earnings time", "release")):
        r = R.classify(q)
        fact = [a for t, a in r.prefetch if t == R.T_FACT]
        assert fact and fact[0]["kind"] == kind and fact[0]["symbol"] == r.tickers[0], q


def test_a_date_question_and_a_view_are_not():
    for q in ("when does amd report", "when is nvda earnings", "what do you think of MU",
              "what time does NVDA open", "is NVDA a buy before earnings"):
        assert R.T_FACT not in [t for t, _ in R.classify(q).prefetch], q


def test_parse_needs_a_stock_and_report_words_off_the_date_shape():
    assert A.parse("what time is the call", R.FOLLOW_UP, "NVDA") == {"symbol": "NVDA", "kind": "call"}
    assert A.parse("what time is it", R.FOLLOW_UP, "NVDA") is None
    assert A.parse("what time", R.EARNINGS_DATE, "MU") == {"symbol": "MU", "kind": "release"}
    assert A.parse("what time does spy report", R.EARNINGS_DATE, "SPY") is None


# ------------------------------------------------------------------ the reply

def test_the_stated_zone_is_converted_to_eastern_in_code():
    for text, want in (("4:20 PM ET", "4:20 PM ET"), ("1:20 p.m. PT", "4:20 PM ET"),
                       ("5:00 PM Eastern Time", "5:00 PM ET"), ("2 PM Pacific", "5:00 PM ET"),
                       ("7:30 a.m. CT", "8:30 AM ET"), ("5:00 PM today", "5:00 PM ET"),
                       ("12:00 PM ET", "12:00 PM ET")):
        assert A._clock(A.to_eastern(text, TODAY)) == want, text
    for text in ("NONE", "after market close", "9 a.m. IST", ""):
        assert A.to_eastern(text, TODAY) is None, text


def test_the_reply_is_read_field_by_field():
    got = A.read_reply("DATE: 2026-11-17\nRELEASE: 1:20 PM PT\nCALL: 2:00 PM PT\n"
                       "SOURCE: NVIDIA Investor Relations")
    assert got == {"date": "2026-11-17", "release": (16, 20), "call": (17, 0),
                   "source": "NVIDIA Investor Relations"}
    got = A.read_reply("**DATE:** 2026-11-12\n**RELEASE:** NONE\n**CALL:** 8:00 AM ET\n**SOURCE:** Nebius")
    assert got["release"] is None and got["call"] == (8, 0)
    assert A.read_reply("NOT FOUND") is None
    assert A.read_reply("DATE: 2026-11-12\nRELEASE: NONE\nCALL: NONE\nSOURCE: x") is None
    assert A.read_reply("RELEASE: 4:05 PM ET\nCALL: 5:00 PM ET") is None        # no date, no check


# ------------------------------------------------------------- what holds up

def test_the_date_must_be_the_feeds_report_date():
    assert A.checked(FACT, DAYS, TODAY)["call"] == (17, 0)
    assert A.checked(FACT, [("2026-11-18", "")], TODAY)                 # a day either side
    # last quarter's call, or a usual time under a made-up date
    assert A.checked(FACT, [("2026-08-26", "after market close")], TODAY) is None
    assert A.checked({**FACT, "date": "2026-08-26"}, DAYS, TODAY) is None
    assert A.checked({"status": "no_data"}, DAYS, TODAY) is None
    assert A.checked(None, DAYS, TODAY) is None


def test_with_no_feed_date_the_search_needs_links_and_a_near_date():
    assert A.checked(FACT, [], TODAY)
    assert A.checked({**FACT, "sources": []}, [], TODAY) is None
    assert A.checked({**FACT, "date": "2026-03-01"}, [], TODAY) is None


def test_a_time_on_the_wrong_side_of_the_session_is_dropped():
    # "after market close" and 1:20 PM: the reply did not convert the zone
    got = A.checked({**FACT, "release": (13, 20)}, DAYS, TODAY)
    assert got["release"] is None and got["call"] == (17, 0)
    assert A.checked({**FACT, "release": (13, 20), "call": (14, 0)}, DAYS, TODAY) is None
    got = A.checked({**FACT, "release": (10, 0), "call": (8, 0)}, [("2026-11-17", "before market open")], TODAY)
    assert got["release"] is None and got["call"] == (8, 0)
    # a call before its release
    got = A.checked({**FACT, "release": (16, 20), "call": (16, 5)}, DAYS, TODAY)
    assert got["call"] is None


# -------------------------------------------------------------------- the line

def _line(fact, days=None):
    return A.lines(fact, DAYS if days is None else days, "NVDA", TODAY)


def test_the_line_states_what_was_found_and_where():
    assert _line(FACT) == (["→ **NVDA** earnings call at **5:00 PM ET**, results at **4:20 PM ET**, "
                            "Tue Nov 17 (NVIDIA Investor Relations)."], True)
    assert _line({**FACT, "kind": "release"})[0] == [
        "→ **NVDA** results at **4:20 PM ET**, earnings call at **5:00 PM ET**, Tue Nov 17 "
        "(NVIDIA Investor Relations)."]
    assert _line({**FACT, "release": None})[0] == [
        "→ **NVDA** earnings call at **5:00 PM ET**, Tue Nov 17 (NVIDIA Investor Relations). "
        "No release time was found."]
    assert _line({**FACT, "call": None})[0] == [
        "→ **NVDA** results at **4:20 PM ET**, Tue Nov 17 (NVIDIA Investor Relations). No call time was found."]


def test_the_credit_is_a_site_the_search_returned():
    # the named publisher matches no link: the link's site is credited
    assert _line({**FACT, "source": "MarketBeat"})[0][0].endswith("(nvidia.com).")
    assert _line({**FACT, "sources": []})[0][0].endswith("(NVIDIA Investor Relations).")


def test_nothing_found_and_a_search_that_did_not_come_back_say_so():
    assert _line({"status": "no_data"}) == ([A.not_found("NVDA")], False)
    assert _line({**FACT, "date": "2026-08-26"}) == ([A.not_found("NVDA")], False)
    assert _line({"status": "timeout"}) == ([A.no_search("NVDA")], False)
    assert _line({"status": "error"}) == ([A.no_search("NVDA")], False)


# ------------------------------------------------------------------ the answer

EDATE = {"status": "ok", "symbol": "NVDA", "next": {
    "date": "2026-11-17", "timing": "after market close",
    "when": "today (Tue Nov 17), after market close", "eps_estimate": 2.47, "revenue_estimate": 108.7e9,
    "date_status": "listed by both the calendar feed and Yahoo"}}
SNAP = {"status": "ok", "symbol": "NVDA", "growth": {
    "next_report": {"date": "2026-11-17", "session": "after the close"},
    "next_quarter": {"eps": 2.47, "eps_year_ago": 1.30, "revenue": 108.7e9, "revenue_year_ago": 57.0e9},
    "last_report": {"date": "2026-08-26", "session": "after the close", "actual": 2.22, "estimate": 2.09}}}
PRICE = {"status": "ok", "session": "REGULAR", "quotes": [
    {"symbol": "NVDA", "price": 155.46, "change_pct": -0.4, "data_freshness": "live_regular_session"}]}


def _body(fact, shape="earnings_date", asked="what time is Nvidia earnings call"):
    return B.build(shape, "NVDA", {R.T_EDATE: EDATE, R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE, R.T_FACT: fact},
                   asked=asked, today=TODAY)


def test_the_time_leads_the_turn():
    body = _body(FACT)
    assert body["lines"][0] == ("→ **NVDA** earnings call at **5:00 PM ET**, results at **4:20 PM ET**, "
                                "Tue Nov 17 (NVIDIA Investor Relations).")
    # then the report's day, and nothing nobody asked for: no consensus, last report or price
    assert body["lines"][1:] == ["→ Reports today (Tue Nov 17), after market close."]
    assert body["fact"] and body["fact_found"] and not body["fact_failed"] and body["fact_only"]


def test_no_time_found_is_a_line_and_the_day_still_follows():
    body = _body({"status": "no_data"})
    assert body["lines"][0] == A.not_found("NVDA") and not body["fact_found"]
    assert body["lines"][1:] == ["→ Reports today (Tue Nov 17), after market close."]
    assert _body({"status": "timeout"})["fact_failed"]


def test_a_turn_that_did_not_ask_has_no_fact_line():
    body = B.build("earnings_date", "NVDA", {R.T_EDATE: EDATE, R.T_SNAPSHOT: SNAP, R.T_PRICE: PRICE},
                   asked="when does nvda report", today=TODAY)
    assert not body["fact"] and all("clock time" not in ln for ln in body["lines"])


def test_a_reply_asking_the_time_gets_the_line_alone():
    body = _body(FACT, shape="follow_up", asked="What time is the call")
    assert body["lines"] == ["→ **NVDA** earnings call at **5:00 PM ET**, results at **4:20 PM ET**, "
                             "Tue Nov 17 (NVIDIA Investor Relations)."]


HABIT = ("→ Numbers typically cross the wire between 4:05 and 4:15 PM ET, with the conference call "
         "following shortly after")
FINE = "→ Management hosts the webcast on the investor relations site at 5 p.m. ET"
SEP = chr(10) * 2


def test_a_clock_time_question_is_answered_by_its_lines_alone():
    out, dropped = B.compose(HABIT + SEP + FINE, _body(FACT))
    assert out.count("→") == 2 and "4:15" not in out and "webcast" not in out and dropped == 2


def test_a_second_question_joined_on_keeps_the_rundown_and_the_models_arrows():
    asked = "what time is nvda earnings and what other main earnings we have"
    body = _body(FACT, asked=asked)
    assert not body["fact_only"] and len(body["lines"]) > 2
    out, dropped = B.compose("→ Dell and Deere also report this week, both on the earnings calendar", body)
    assert "Dell and Deere" in out and dropped == 0


def test_beside_a_view_an_arrow_with_a_time_the_line_does_not_carry_is_dropped():
    """The clock time asked inside a broader question: the model's other
    arrows stay, one that gives a time of its own does not."""
    body = _body(FACT, shape="ticker_opinion", asked="what do you think of nvda and what time is the call")
    assert not body["fact_only"] and body["lines"][0].startswith("→ **NVDA** earnings call at **5:00 PM ET**")
    out, dropped = B.compose(HABIT + SEP + FINE, body)
    assert "4:15" not in out and "webcast on the investor relations site" in out and dropped == 1
    # nothing found: no clock time at all from the model
    body = _body({"status": "no_data"}, shape="ticker_opinion",
                 asked="what do you think of nvda and what time is the call")
    out, dropped = B.compose(HABIT + SEP + FINE, body)
    assert "4:15" not in out and "5 p.m." not in out and dropped == 2


def test_the_models_own_search_stands_in_only_for_a_search_that_failed():
    sourced = "→ The company's notice puts the conference call at 5:00 PM ET on the investor site"
    out, _ = B.compose(sourced, _body({"status": "timeout"}), grounded=True)
    assert "5:00 PM ET" in out
    out, _ = B.compose(sourced, _body({"status": "timeout"}), grounded=False)
    assert "5:00 PM ET" not in out
    out, _ = B.compose(sourced, _body({"status": "no_data"}), grounded=True)
    assert "5:00 PM ET" not in out


def test_clock_times_are_compared_whatever_their_spelling():
    posted = "→ **NVDA** earnings call at **5:00 PM ET**, results at **4:20 PM ET**, Tue Nov 17."
    assert not A.unposted_clock("the call is at 5 p.m. ET and results at 4:20pm", posted)
    assert A.unposted_clock("results usually land at 4:05 PM", posted)
    assert not A.unposted_clock("no time in this arrow, just 4 quarters and 20 percent", posted)
