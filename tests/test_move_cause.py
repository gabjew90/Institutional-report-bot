"""Why a stock moved (discord_bot/move_cause.py, 2026-11-21). The news search
runs in code for every stock named, the answer is the price lines and the
cause from the news lines, and a cause with no source behind it is not
posted."""
from datetime import date

from discord_bot import ask_router as R
from discord_bot import data_footer as F
from discord_bot import move_cause as M

SAT = date(2026, 11, 21)          # the day after the question that started this
PRICE = {"status": "ok", "session": "CLOSED", "quotes": [
    {"symbol": "CRWD", "price": 211.80, "change_pct": -5.8, "regular_session_close": 229.44,
     "extended_hours_change_pct": -7.7, "data_freshness": "live_extended_hours"},
    {"symbol": "ZS", "price": 226.50, "change_pct": -5.3, "regular_session_close": 194.82,
     "extended_hours_change_pct": 16.3, "data_freshness": "live_extended_hours"}]}
ZS_NEWS = {"status": "ok", "symbol": "ZS", "sources": [{"title": "bloomberg.com", "url": "https://b/1"},
                                                       {"title": "stocktitan.net", "url": "https://s/1"}],
           "digest": "2026-11-20: CrowdStrike is in talks to acquire Zscaler, with shares jumping 18% "
                     "after hours (Bloomberg)\n"
                     "2026-11-18: Zscaler announced an AI security partnership (Stock Titan)\n"
                     "2026-11-16: An analyst raised its price target to $240 (MarketBeat)"}
CRWD_NEWS = {"status": "ok", "symbol": "CRWD", "sources": [{"title": "reuters.com", "url": "https://r/1"}],
             "digest": "2026-11-20: CrowdStrike is exploring a purchase of Zscaler (Reuters)"}
OLD_NEWS = {"status": "ok", "symbol": "CRWD", "sources": [],
            "digest": "2026-11-16: CrowdStrike opened a new office (PR Newswire)"}
PLAN = [(R.T_PRICE, {"symbols": ["CRWD", "ZS"]}), (R.T_NEWS, {"symbol": "CRWD"}), (R.T_NEWS, {"symbol": "ZS"})]
# Two stocks with nothing to do with each other: one has news, one has none.
PRICE2 = {"status": "ok", "session": "CLOSED", "quotes": [
    {"symbol": "KO", "price": 96.99, "change_pct": 0.9, "data_freshness": "regular_session_close"},
    PRICE["quotes"][1]]}
KO_OLD = {"status": "ok", "symbol": "KO", "sources": [],
          "digest": "2026-11-16: Coca-Cola opened a bottling plant (PR Newswire)"}
PLAN2 = [(R.T_PRICE, {"symbols": ["KO", "ZS"]}), (R.T_NEWS, {"symbol": "KO"}), (R.T_NEWS, {"symbol": "ZS"})]
KO_LINE = "→ **KO** last close **$96.99** (+0.9% that session)."
CRWD_LINE = "→ **CRWD** closed at **$229.44** (-5.8% on the day), **$211.80** after hours, -7.7%."
ZS_LINE = "→ **ZS** closed at **$194.82** (-5.3% on the day), **$226.50** after hours, +16.3%."


# ---------------------------------------------------------------- the question

def test_a_cause_question_and_one_that_asks_for_more():
    for q in ("what happened with CRWD and ZS stocks right now after hours", "why is NVDA down",
              "why are CRWD and ZS down", "why did googl dump so hard", "explain pltr death",
              "what's going on with PLTR"):
        assert R.asks_cause(q), q
    for q in ("what happened to ZS and why do analysts hate it", "why is NVDA down, should i buy",
              "odds NVDA beats earnings", "why is AMD down and when do they report",
              "what do you think of MU", "when does amd report",
              # further back than the week the search covers
              "what happened to GM in August where the share price went from 72$ to 92$ in about a week",
              "why did NVDA drop last month", "what happened to the us strategic petroleum reserves in 1982"):
        assert not R.asks_cause(q), q


def test_two_or_three_named_stocks_route_as_news_with_a_search_on_each():
    for q in ("what happened with CRWD and ZS stocks right now after hours", "why are CRWD and ZS down",
              "why did NVDA and AMD dump today"):
        r = R.classify(q)
        assert r.shape == R.NEWS_EVENT, q
        assert [t for t, _ in r.prefetch] == [R.T_PRICE, R.T_NEWS, R.T_NEWS], q
        assert [a["symbol"] for t, a in r.prefetch if t == R.T_NEWS] == r.tickers


def test_the_search_is_capped_and_skips_funds():
    r = R.classify("why are NVDA, AMD, AVGO and MRVL down")
    assert sum(t == R.T_NEWS for t, _ in r.prefetch) == R.MAX_CAUSE_SYMBOLS
    r = R.classify("why are NVDA and SPY down")
    assert [a["symbol"] for t, a in r.prefetch if t == R.T_NEWS] == ["NVDA"]


def test_one_stock_keeps_its_route():
    r = R.classify("what happened to ZS")
    assert [t for t, _ in r.prefetch] == [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]


def test_two_stocks_on_an_odds_question_get_no_search():
    r = R.classify("odds NVDA beats and AMD misses earnings")
    assert R.T_NEWS not in [t for t, _ in r.prefetch] or len(r.tickers) == 1


# ------------------------------------------------------------- which news counts

def test_the_cutoff_is_the_trading_day_before_today():
    assert M.cutoff("", SAT) == date(2026, 11, 20)                  # Saturday: Friday
    assert M.cutoff("", date(2026, 11, 23)) == date(2026, 11, 20)   # Monday: Friday's close
    assert M.cutoff("", date(2026, 11, 24)) == date(2026, 11, 23)
    assert M.cutoff("", date(2026, 11, 27)) == date(2026, 11, 25)   # the day after Thanksgiving
    assert M.cutoff("what happened to ZS this week", SAT) == date(2026, 11, 14)


def test_only_lines_dated_since_the_cutoff_are_handed_over():
    assert M.fresh_lines(ZS_NEWS, "", SAT) == [
        "2026-11-20: CrowdStrike is in talks to acquire Zscaler (Bloomberg)"]
    assert len(M.fresh_lines(ZS_NEWS, "what happened to zs this week", SAT)) == 3
    assert M.fresh_lines(OLD_NEWS, "", SAT) == []
    assert M.fresh_lines({"status": "no_data"}, "", SAT) == []
    assert M.fresh_lines(None, "", SAT) == []


def test_the_news_own_account_of_the_move_is_cut_and_a_short_line_is_left_whole():
    assert M._clean("2026-11-20: CrowdStrike is in talks to acquire Zscaler, with shares jumping 18% "
                    "after hours (Bloomberg)") == "2026-11-20: CrowdStrike is in talks to acquire Zscaler (Bloomberg)"
    assert M._clean("2026-11-20: Deal talks, and the stock jumped 18% (Reuters)") == \
        "2026-11-20: Deal talks, and the stock jumped 18% (Reuters)"
    # the move as the event's consequence, and a second date after the first (2026-11-21 probe)
    assert M._clean("2026-11-20: Bloomberg reported that CrowdStrike was in talks to acquire Zscaler, causing "
                    "Zscaler shares to jump over 16% in after-hours trading. (SiliconANGLE)") == \
        "2026-11-20: Bloomberg reported that CrowdStrike was in talks to acquire Zscaler (SiliconANGLE)"
    assert M._clean("- 2026-11-20 (2026-11-21 reports): CrowdStrike was reported to be in ongoing talks to "
                    "acquire cybersecurity provider Zscaler (Bloomberg)") == \
        ("2026-11-20: CrowdStrike was reported to be in ongoing talks to acquire cybersecurity provider "
         "Zscaler (Bloomberg)")
    assert M._clean("2026-11-20: Zscaler on 2026-11-19 sending a proposal to the board (Reuters)") == \
        "2026-11-20: Zscaler on 2026-11-19 sending a proposal to the board (Reuters)"


# ------------------------------------------------------------------- the body

def test_one_event_that_moved_two_stocks_is_one_block_and_one_sentence():
    """Each stock's line names the other company: the talks are one event
    (two of four harness runs on 2026-11-21 wrote it once per stock)."""
    body = M.build(PLAN, [PRICE, CRWD_NEWS, ZS_NEWS], today=SAT)
    assert body["lines"] == [CRWD_LINE, ZS_LINE]
    c = body["cause"]
    assert c["slots"] == 1 and c["missing"] == [] and list(c["news"]) == ["CRWD and ZS"]
    assert c["news"]["CRWD and ZS"] == [
        "2026-11-20: CrowdStrike is exploring a purchase of Zscaler (Reuters)",
        "2026-11-20: CrowdStrike is in talks to acquire Zscaler (Bloomberg)"]
    text = M.outline(body)
    assert CRWD_LINE in text and "NEWS ON CRWD and ZS: 2026-11-20: CrowdStrike is exploring" in text
    assert "Write one arrow, and where a block is on several stocks" in text and "18%" not in text


def test_a_stock_another_stocks_line_names_is_explained_by_it():
    # CRWD's own search found nothing recent, and ZS's line is about CrowdStrike
    body = M.build(PLAN, [PRICE, OLD_NEWS, ZS_NEWS], today=SAT)
    c = body["cause"]
    assert list(c["news"]) == ["CRWD and ZS"] and c["found_nothing"] == [] and c["missing"] == []


def test_two_unrelated_stocks_keep_a_block_each():
    ko = {"status": "ok", "symbol": "KO", "digest": "2026-11-20: Coca-Cola raised its dividend (Reuters)"}
    body = M.build(PLAN2, [PRICE2, ko, ZS_NEWS], today=SAT)
    assert list(body["cause"]["news"]) == ["KO", "ZS"] and body["cause"]["slots"] == 2
    assert "one arrow per NEWS block below, 2 arrows" in M.outline(body)


def test_a_stock_with_nothing_recent_gets_a_line_and_no_slot():
    body = M.build(PLAN2, [PRICE2, KO_OLD, ZS_NEWS], today=SAT)
    c = body["cause"]
    assert c["slots"] == 1 and c["found_nothing"] == ["KO"]
    assert c["missing"] == ["→ The news search found nothing on **KO** dated Fri Nov 20 or later, "
                            "so no reason for the move is confirmed."]
    assert "NEWS ON KO" not in M.outline(body)


def test_a_search_that_did_not_come_back_is_said_to_have_failed():
    body = M.build(PLAN, [PRICE, None, {"status": "error", "symbol": "ZS"}], today=SAT)
    c = body["cause"]
    assert c["slots"] == 0 and c["failed"] == ["CRWD", "ZS"]
    assert c["missing"] == ["→ The news search on **CRWD** or **ZS** did not come back, "
                            "so no reason for the move is confirmed."]
    assert "Never offer a reason from memory" in M.outline(body)


def test_no_body_without_a_quote_or_a_searched_stock():
    assert M.build(PLAN, [None, CRWD_NEWS, ZS_NEWS], today=SAT) is None
    assert M.build([(R.T_PRICE, {"symbols": ["SPY"]})], [PRICE], today=SAT) is None
    # a coin's search (the sector basket's) is not a stock's
    assert M.build([(R.T_PRICE, {}), (R.T_NEWS, {"symbol": "BTC"})], [PRICE, ZS_NEWS], today=SAT) is None
    assert M.any_fresh([(R.T_NEWS, {"symbol": "BTC"})], [ZS_NEWS], today=SAT)
    # nor is a sector's: "why are semis red" searches its first name only
    r = R.classify("why are semis red")
    assert [a for t, a in r.prefetch if t == R.T_NEWS] == [{"symbol": "NVDA", "basket": True}]
    assert M.build(r.prefetch, [PRICE, ZS_NEWS], today=SAT) is None


# ------------------------------------------------------------------ the answer

CAUSE = "→ CrowdStrike is in talks to acquire Zscaler, Bloomberg reported on Friday."
INVENTED = ("→ Cyber names seeing extreme bifurcated flow and multiple compression as desks de-risk "
            "enterprise software ahead of upcoming December prints")


def test_one_arrow_per_stock_with_news_and_the_rest_dropped():
    body = M.build(PLAN2, [PRICE2, KO_OLD, ZS_NEWS], today=SAT)
    out, info = M.compose(CAUSE + "\n\n" + INVENTED, body)
    assert out == "\n\n".join([KO_LINE, ZS_LINE, CAUSE, body["cause"]["missing"][0]])
    assert info == {"kept": 1, "dropped": 1, "missing": True, "fallback": False}


def test_a_cause_with_no_news_and_no_sources_is_not_posted():
    """The 2026-11-20 answer: both prices right, the reason made up."""
    body = M.build(PLAN, [PRICE, OLD_NEWS, {"status": "no_data", "symbol": "ZS"}], today=SAT)
    out, info = M.compose(INVENTED, body, grounded=False)
    assert "bifurcated" not in out and info["kept"] == 0 and info["missing"]
    assert out.endswith("→ The news search found nothing on **CRWD** or **ZS** dated Fri Nov 20 or "
                        "later, so no reason for the move is confirmed.")


def test_the_models_own_search_supplies_the_cause_only_when_the_systems_failed():
    sourced = "→ CrowdStrike is in talks to buy ZS, per Bloomberg."
    # ZS's search did not come back: a reply with Google sources stands in for it
    body = M.build(PLAN2, [PRICE2, KO_OLD, None], today=SAT)
    out, info = M.compose(sourced, body, grounded=True)
    assert "in talks to buy ZS" in out and info["kept"] == 1 and "did not come back" not in out
    assert out.endswith("→ The news search found nothing on **KO** dated Fri Nov 20 or later, "
                        "so no reason for the move is confirmed.")
    out, info = M.compose(sourced, body, grounded=False)
    assert "in talks" not in out and "did not come back" in out
    # the search ran and found nothing: that is the answer, sources or not
    # ("why is KO down" got "valuation resets and profit-taking" with links, 2026-11-21 harness)
    body = M.build(PLAN, [PRICE, OLD_NEWS, {"status": "no_data", "symbol": "ZS"}], today=SAT)
    out, info = M.compose(sourced, body, grounded=True)
    assert "in talks" not in out and info["kept"] == 0 and info["missing"]
    assert "No reported cause" in M.outline(body) and "search the web now" not in M.outline(body)


def test_the_models_search_is_forced_only_when_none_ran_or_one_failed():
    assert not M.needs_search(PLAN, [PRICE, CRWD_NEWS, ZS_NEWS], today=SAT)        # a line can explain it
    assert not M.needs_search(PLAN, [PRICE, OLD_NEWS, {"status": "no_data"}], today=SAT)   # ran, nothing
    assert M.needs_search(PLAN, [PRICE, OLD_NEWS, None], today=SAT)                # one timed out
    assert M.needs_search(PLAN, [PRICE, OLD_NEWS, {"status": "error"}], today=SAT)
    assert M.needs_search([(R.T_PRICE, {"symbols": ["SPY"]})], [PRICE], today=SAT)  # a fund: none ran
    # a coin's or a sector's search that found nothing is weak evidence: the model's is forced
    assert M.needs_search([(R.T_NEWS, {"symbol": "BTC"})], [OLD_NEWS], today=SAT)
    assert M.needs_search([(R.T_NEWS, {"symbol": "NVDA", "basket": True})], [OLD_NEWS], today=SAT)
    assert not M.needs_search([(R.T_NEWS, {"symbol": "NVDA", "basket": True})], [ZS_NEWS], today=SAT)


def test_a_news_line_is_posted_as_written_when_the_model_wrote_no_cause():
    body = M.build(PLAN, [PRICE, CRWD_NEWS, ZS_NEWS], today=SAT)
    out, info = M.compose("", body)
    # one event, so one line: the first of the block
    assert info["fallback"] and info["kept"] == 1
    assert out.endswith("→ Nov 20: CrowdStrike is exploring a purchase of Zscaler (Reuters).")


def test_the_no_answer_line_is_never_an_arrow():
    body = M.build(PLAN, [PRICE, CRWD_NEWS, ZS_NEWS], today=SAT)
    out, info = M.compose("→ Thought myself in circles and ran out of room. Try asking it more directly.", body)
    assert "circles" not in out and info["fallback"]


# ------------------------------------------------------------------ the footer

def test_both_searches_links_are_kept_one_from_each_first():
    news = M.merge_news(M.merge_news(None, ZS_NEWS), CRWD_NEWS)
    assert [s["url"] for s in news["sources"]] == ["https://b/1", "https://r/1", "https://s/1"]
    assert "Reuters" in news["digest"] and "Bloomberg" in news["digest"]


def test_a_cause_from_the_news_lines_cites_them_named_publisher_first():
    news = M.merge_news(M.merge_news(None, ZS_NEWS), CRWD_NEWS)
    answer = ZS_LINE + "\n\n→ CrowdStrike is exploring a purchase of Zscaler (Reuters)."
    assert "Sources:" not in F.compose("", answer, news, [])           # no figure of the news in it
    cited = F.compose("", answer, news, [], cite_news=True)
    # two links as everywhere else, the publisher the answer names first
    assert cited.index("reuters.com") < cited.index("bloomberg.com")
    assert cited.count("](<") == 2 and "stocktitan" not in cited
