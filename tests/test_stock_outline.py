"""The /ask stock outline (2026-10-01): slots in order, each fact with its
true source, built in code from the prefetch results."""
from discord_bot import ask_router as R
from discord_bot import stock_outline as O

PRICE = {"status": "ok", "quotes": [{"symbol": "MU", "price": 1045.73, "change_pct": -1.8}]}
SNAP = {"status": "ok", "symbol": "MU", "short_pct_float": 2.5, "short_interest_as_of": "2026-09-15",
        "pct_below_52w_high": 16.7, "offering_filings_12m": [],
        "growth": {"last_report": {"date": "2026-09-30", "session": "after the close", "actual": 33.42,
                                   "estimate": 31.82, "surprise_pct": 5.0},
                   "next_quarter": {"revenue_growth_pct": 349.4, "eps_growth_pct": 695.4, "eps": 38.02},
                   "eps_reports": [{"date": "2026-09-30", "surprise_pct": 5.0}]}}
FRESH = {"date": "2026-09-30", "session": "after the close", "actual": 33.42, "estimate": 31.82,
         "surprise_pct": 5.0, "symbol": "MU"}
NEWS = {"status": "ok", "digest": "- 2026-09-30: Micron revenue $54.23B, +379% y/y, beat (GlobeNewswire)\n"
                                  "- 2026-10-01: Morgan Stanley raised its target to $1,200 (Barron's)"}
PRIMER = {"status": "ok", "primer": "SELLS: memory chips\nSEGMENTS: Cloud Memory ~30%\n"
                                    "DRIVERS: Cloud Memory drives growth on AI server demand for HBM\n"
                                    "WATCHED: gross margin and the next quarter's revenue guide"}
RESEARCH = {"status": "ok", "notes": [
    {"source": "Syz Group", "published": "2026-09-29", "insights": ["Burry raised a short in MU"]},
    {"source": "Goldman Sachs", "published": "2026-09-28", "earnings": ["GS expects upside to consensus"]},
    {"source": "JPMorgan", "published": "2026-09-29",
     "calls": [{"action": "positive_catalyst_watch", "rating": "N/A", "price_target": "N/A",
                "rationale": "Guide above Street on HBM4 demand", "conviction": "high"}]},
    {"source": "JPMorgan", "published": "2026-09-27", "insights": ["older JPM note"]}]}


def _results(**extra):
    base = {R.T_PRICE: PRICE, R.T_SNAPSHOT: SNAP, R.T_NEWS: NEWS, R.T_PRIMER: PRIMER,
            R.T_RESEARCH: RESEARCH}
    base.update(extra)
    return base


def test_a_fresh_print_outline_leads_with_price_then_the_result_with_its_true_source():
    text = O.build_outline(_results(), "ticker_opinion", "mu", fresh=FRESH)
    lines = [ln for ln in text.splitlines() if ln[:2].rstrip(".").isdigit()]
    assert lines[0] == "1. PRICE: MU $1,045.73, -1.8% today [live prices]"
    assert lines[1].startswith("2. RESULT: reported 2026-09-30 after the close: EPS $33.42 vs $31.82 "
                               "expected (+5.0%) [company report]")
    assert "(GlobeNewswire)" in lines[1], "news lines carry their publisher"
    assert "name the publisher each line ends with" in lines[1]
    assert lines[2].startswith("3. DRIVER: Cloud Memory drives growth")
    # an analyst's action after the report, from the news, sits with the desks
    assert lines[3] == ("4. ANALYST ACTION: Morgan Stanley raised its target to $1,200, 2026-10-01 "
                        "(Barron's) [news search]")
    # 2026-11-12 (owner): a note written before the report gets no slot once
    # the result is out, and there is no POSITIONING slot then
    assert len(lines) == 4 and "DESK:" not in text
    assert "POSITIONING:" not in text
    assert "a figure from the news or the company report is never a bank's" in text


# The 2026-11-12 "did amat earnings droo" payloads, 19 minutes after the report.
AMAT_PRICE = {"status": "ok", "quotes": [{"symbol": "AMAT", "price": 383.14, "change_pct": -3.5765,
                                          "extended_hours_change_pct": 0.5749}]}
AMAT_SNAP = {"status": "ok", "symbol": "AMAT", "short_pct_float": 1.8, "short_interest_as_of": "2026-10-30",
             "pct_below_52w_high": 48.5, "offering_filings_12m": [], "growth": {}}
AMAT_NEWS = {"status": "ok", "digest": (
    "- 2026-11-12: Applied Materials reported fourth-quarter fiscal 2026 revenue of $10.41 billion (up 53% "
    "year-over-year) beating the $10.30 billion estimate, and non-GAAP EPS of $4.16 (up 92% year-over-year) "
    "beating the $4.06 estimate; guided fiscal first-quarter revenue to $11.10 billion (plus or minus $500 "
    "million) and non-GAAP EPS to $4.35 (plus or minus $0.20) against estimates, with shares moving lower "
    "following the release\n"
    "- 2026-11-12: HSBC maintained a Buy rating on Applied Materials and set a price target of $641\n"
    "- 2026-11-09: B. Riley Securities maintained a Buy rating on Applied Materials and cut the price target "
    "from $700 to $475")}
AMAT_PRIMER = {"status": "ok", "primer": "SELLS: chip-making equipment\nDRIVERS: Semiconductor Systems drives "
                                         "revenue growth on AI chip manufacturing demand\nWATCHED: gross margin"}
AMAT_RESEARCH = {"status": "ok", "notes": [
    {"source": "Goldman Sachs", "published": "2026-11-12", "calls": [], "earnings": [], "insights": []},
    {"source": "Goldman Sachs", "published": "2026-11-11", "calls": [], "earnings": [],
     "insights": ["(reported) GS Research prefers AMAT for semi cap equipment, using 2027 WFE as a baseline."]},
    {"source": "Goldman Sachs", "published": "2026-11-11", "earnings": ["Applied Materials (AMAT) and Cisco "
                                                                         "(CSCO) report earnings 11/12."],
     "calls": [{"action": "positive_catalyst_watch", "rating": "Neutral", "price_target": "N/A",
                "rationale": "Reporting earnings after the close on Thursday.", "conviction": "low"}]},
    {"source": "JPMorgan", "published": "2026-11-11", "calls": [], "insights": [],
     "earnings": ["AMAT reports earnings Thursday after the close."]}]}
AMAT_FRESH = {"date": "2026-11-12", "session": "after the close", "actual": None, "estimate": None,
              "symbol": "AMAT"}


def _amat(**extra):
    base = {R.T_PRICE: AMAT_PRICE, R.T_SNAPSHOT: AMAT_SNAP, R.T_NEWS: AMAT_NEWS, R.T_PRIMER: AMAT_PRIMER,
            R.T_RESEARCH: AMAT_RESEARCH}
    base.update(extra)
    return base


def _slots(text):
    return [ln.split(". ", 1)[1] for ln in text.splitlines() if ln[:2].rstrip(".").isdigit()]


def test_after_a_print_the_outline_is_the_result_the_guidance_the_driver_and_the_analyst():
    slots = _slots(O.build_outline(_amat(), "ticker_opinion", "AMAT", fresh=AMAT_FRESH))
    assert [s.split(":")[0] for s in slots] == ["PRICE", "RESULT", "GUIDANCE", "DRIVER", "ANALYST ACTION"]
    result, guidance, analyst = slots[1], slots[2], slots[4]
    assert "$10.41 billion" in result and "$4.16" in result and "guided" not in result and "HSBC" not in result
    assert guidance == ("GUIDANCE: guided fiscal first-quarter revenue to $11.10 billion (plus or minus $500 "
                        "million) and non-GAAP EPS to $4.35 (plus or minus $0.20) against estimates [news search]")
    assert analyst == ("ANALYST ACTION: HSBC maintained a Buy rating on Applied Materials and set a price "
                       "target of $641, 2026-11-12 [news search]")
    # no publisher on the line, so the model is not asked to name one
    assert "name the publisher" not in result


def test_the_stocks_move_comes_from_the_live_quote_not_the_news_line():
    text = O.build_outline(_amat(), "ticker_opinion", "AMAT", fresh=AMAT_FRESH)
    assert "shares moving lower" not in text and "+0.6% after the regular close" in text
    # with no live quote the news line's account is all there is
    no_quote = O.build_outline(_amat(**{R.T_PRICE: {"status": "error"}}), "ticker_opinion", "AMAT",
                               fresh=AMAT_FRESH)
    assert "shares moving lower" in no_quote


def test_a_note_that_only_lists_the_report_date_gets_no_desk_slot():
    # after the print: neither bank's notes say anything about the result
    after = O.build_outline(_amat(), "ticker_opinion", "AMAT", fresh=AMAT_FRESH)
    assert "DESK:" not in after
    # before it: the calendar lines still get no slot, and Goldman's slot is
    # its note with a view, not the newer one that only says when AMAT reports
    before = _slots(O.build_outline(_amat(), "ticker_opinion", "AMAT"))
    desks = [s for s in before if s.startswith("DESK:")]
    assert len(desks) == 1 and "prefers AMAT for semi cap equipment" in desks[0]
    assert "JPMorgan" not in " ".join(before)
    assert any(s.startswith("POSITIONING:") for s in before), "positioning stays when no print is out"


def test_after_a_print_only_a_note_written_after_it_gets_a_desk_slot():
    notes = [{"source": "Bernstein", "published": "2026-11-10",
              "calls": [{"action": "reiterate", "rating": "Outperform", "price_target": "$520",
                         "rationale": "WFE share gains", "conviction": "high"}]},
             {"source": "UBS", "published": "2026-11-12", "insights": ["UBS models fiscal Q1 revenue of $11.3B, "
                                                                        "3% above the Street."]},
             {"source": "Citi", "published": "2026-11-13",
              "calls": [{"action": "price_target_change", "rating": "Buy", "price_target": "$450",
                         "rationale": "Guide above the Street on foundry orders", "conviction": "medium"}]}]
    results = _amat(**{R.T_RESEARCH: {"status": "ok", "notes": notes}})
    text = O.build_outline(results, "ticker_opinion", "AMAT", fresh=AMAT_FRESH)
    # Bernstein wrote two days before, UBS the morning of an after-the-close report
    assert "Bernstein" not in text and "UBS" not in text
    assert "DESK: Citi (2026-11-13): price target change, Buy, price target $450" in text
    # with no print out, all three are views
    assert O.build_outline(results, "ticker_opinion", "AMAT").count("DESK:") == O.MAX_DESKS
    # a question about the desks still gets what they expected, tagged
    for q in ("what did bernstein expect from amat", "what were analysts saying into amat earnings",
              "amat price targets?"):
        asked = O.build_outline(results, "ticker_opinion", "AMAT", fresh=AMAT_FRESH, asked=q)
        assert "DESK: Bernstein (2026-11-10, written before the report: what it expected)" in asked, q
    for q in ("did amat earnings drop", "thoughts on amat", "amat?"):
        assert "Bernstein" not in O.build_outline(results, "ticker_opinion", "AMAT", fresh=AMAT_FRESH, asked=q), q


def test_news_lines_are_sorted_by_what_they_are_about():
    parts = O._split_news([
        "- 2026-11-12: Revenue rose 53% to $10.41 billion; the company guided next-quarter revenue to "
        "$11.10 billion, and the stock fell 4% after hours (Reuters)",
        "- 2026-11-13: Morgan Stanley raised its target to $450 and expects stronger foundry orders (Barron's)",
        "- 2026-11-09: B. Riley cut its price target to $475 (TipRanks)"], cut_moves=True, since="2026-11-12")
    assert parts["result"] == ["Revenue rose 53% to $10.41 billion (Reuters)"]
    assert parts["guidance"] == ["the company guided next-quarter revenue to $11.10 billion (Reuters)"]
    # the analyst's own expectation stays with the analyst, and a rating
    # dated before the report is not a reaction to it
    assert parts["analyst"] == ["Morgan Stanley raised its target to $450 and expects stronger foundry "
                                "orders, 2026-11-13 (Barron's)"]


def test_what_counts_as_a_desks_figure():
    for yes in ("target $641", "up 12%", "15x 2027E EPS", "25 bps of margin", "revenue of 11.3B"):
        assert O._FIGURE_RE.search(yes), yes
    for no in ("report earnings 11/12", "4Q preview", "using 2027 WFE as a baseline"):
        assert not O._FIGURE_RE.search(no), no


def test_without_a_print_the_outline_gives_the_upcoming_report_and_consensus_growth():
    snap = {**SNAP, "growth": {"next_report": {"date": "2026-10-01", "session": "before the open"},
                               "next_quarter": {"revenue_growth_pct": 2.5, "eps_growth_pct": 5.1, "eps": 3.18},
                               "eps_reports": [{"date": "2026-06-18", "surprise_pct": 2.5}]}}
    chain = {"status": "ok", "summary": {"implied_move_pct": 7.1, "implied_move_dollars": 13.1,
                                         "expiration_iso": "2026-10-02", "live_quotes": True}}
    text = O.build_outline(_results(**{R.T_SNAPSHOT: snap, R.T_CHAIN: chain, R.T_NEWS: {"status": "no_data"}}),
                           "options_chain", "ACN")
    assert ("UPCOMING: next report 2026-10-01 before the open, consensus revenue +2.5% y/y, consensus EPS "
            "+5.1% y/y ($3.18), last report 2026-06-18 beat by +2.5% [consensus data]") in text
    assert "OPTIONS: the at-the-money straddle prices about 7.1% ($13.10) either way by 2026-10-02 [options chain]" in text
    assert "written before the report" not in text


def test_a_desk_call_carries_the_desks_figures_and_options_answers_get_a_price():
    note = {"source": "JPMorgan", "published": "2026-09-29",
            "calls": [{"action": "positive_catalyst_watch", "rationale": "Guide above Street"}],
            "earnings": ["F1Q27 Street $56.3B revenue / $35.71 EPS; JPM expects a raise"]}
    assert O._desk_text(note) == ("positive catalyst watch: Guide above Street. Its figures: "
                                  "F1Q27 Street $56.3B revenue / $35.71 EPS; JPM expects a raise")
    text = O.build_outline({R.T_SNAPSHOT: {**SNAP, "price": 217.95}, R.T_RESEARCH: RESEARCH},
                           "options_chain", "MU", fresh=FRESH)
    assert "1. PRICE: MU $217.95 [company and trading data]" in text
    assert "The RESULT or UPCOMING slot keeps its own arrow" in text


def test_options_not_quoting_and_last_live_quotes_are_said_so():
    closed = {"status": "ok", "summary": {"live_quotes": False}, "quotes_note": "Options are not quoting"}
    assert "options are not quoting right now" in O._options_slot(closed)
    last = {"status": "ok", "as_of": "2026-09-30 19:55 UTC", "quotes_note": "These are the last live quotes",
            "summary": {"implied_move_pct": 7.1, "implied_move_dollars": 13.1, "expiration_iso": "2026-10-02",
                        "live_quotes": True}}
    assert "(last live quotes, 2026-09-30 19:55 UTC)" in O._options_slot(last)


def test_the_source_tags_never_reach_the_reader():
    """The first outline answers ended arrows with '[live prices]', '[JPMorgan]'."""
    live = ("→ **$MU $1,076.15**, **+1.0%** session-to-date [live prices]\n\n"
            "→ Driven by Cloud Memory [company background]\n\n"
            "→ JPMorgan expected a beat [JPMorgan]\n\n"
            "→ Short interest 2.5% [exchange and filing data]\n\n"
            "→ See [the filing](<https://sec.gov/x>) and the [Q4] slide")
    out = O.strip_source_tags(live, {"JPMorgan"})
    assert "[live prices]" not in out and "[JPMorgan]" not in out
    assert "[company background]" not in out and "[exchange and filing data]" not in out
    assert "[the filing](<https://sec.gov/x>)" in out, "links survive"
    assert "the (Q4) slide" in out, "other bracketed names read as parentheses"
    assert O.strip_source_tags("→ revenue $18.68B [Stock Titan]", set()) == "→ revenue $18.68B (Stock Titan)"
    assert out.startswith("→ **$MU $1,076.15**, **+1.0%** session-to-date\n")
    assert O.strip_source_tags("→ BofA expects a beat [BofA-ML]", {"BofA-ML"}) == "→ BofA expects a beat"


def test_no_outline_for_other_shapes_or_too_little_data():
    assert O.build_outline(_results(), "price", "MU") == ""
    assert O.build_outline({R.T_PRICE: PRICE}, "ticker_opinion", "MU") == ""
    assert O.outline_symbol([(R.T_CHAIN, {"symbol": "ACN"}), (R.T_SNAPSHOT, {"symbol": "acn"})]) == "ACN"


def test_a_feed_tag_in_lowercase_is_dropped():
    """2026-10-31 harness: "down -3.7% for the session [finnhub]"."""
    from discord_bot import stock_outline as S
    out = S.strip_source_tags("→ **$ELMT $23.19**, down **-3.7%** for the session [finnhub]\n"
                              "→ Supplies the F-35 [Yahoo]", set())
    assert out == "→ **$ELMT $23.19**, down **-3.7%** for the session\n→ Supplies the F-35 (Yahoo)"


def test_an_outline_label_in_parentheses_is_dropped_and_a_publisher_kept():
    """2026-10-31 harness: "... semiconductor industries (Company Background)"."""
    from discord_bot import stock_outline as S
    out = S.strip_source_tags("→ Makes components for semiconductor industries (Company Background)\n"
                              "→ A $36M contract (GlobeNewswire), short interest 27.8% (Yahoo)", set())
    assert out == ("→ Makes components for semiconductor industries\n"
                   "→ A $36M contract (GlobeNewswire), short interest 27.8% (Yahoo)")


def test_a_share_count_is_not_the_stocks_move_and_a_view_is_not_a_calendar_line():
    parts = O._split_news(["- 2026-11-12: The company reported revenue of $10.41 billion and will buy back "
                           "shares up to $10 billion, and guided revenue to $11.10 billion, though the stock "
                           "slipped 2% (Reuters)"], cut_moves=True)
    assert parts["result"] == ["The company reported revenue of $10.41 billion and will buy back shares up "
                               "to $10 billion (Reuters)"]
    assert parts["guidance"] == ["guided revenue to $11.10 billion (Reuters)"]
    # a results line that also names an analyst stays a result
    both = O._split_news(["- 2026-11-12: Applied reported revenue of $10.41 billion and HSBC raised its price "
                          "target to $641"], cut_moves=True)
    assert both["analyst"] == [] and len(both["result"]) == 1
    assert O._is_schedule_only(["Reporting earnings after the close on Thursday.",
                                "Applied Materials (AMAT) and Cisco (CSCO) report earnings 11/12."])
    assert O._is_schedule_only(["AMAT is due to report tonight."])
    assert not O._is_schedule_only(["Per the report, AMAT is the desk's top pick in semi cap equipment."])
    assert not O._is_schedule_only(["AMAT reports Thursday and the desk expects a guide above the Street."])


def test_the_header_names_the_positioning_slot_only_when_there_is_one():
    after = O.build_outline(_amat(), "ticker_opinion", "AMAT", fresh=AMAT_FRESH)
    before = O.build_outline(_amat(), "ticker_opinion", "AMAT")
    assert "POSITIONING" not in after and "The stock's move comes from the PRICE slot only." in after
    assert "drop the POSITIONING slot if it does not bear on the question" in before

