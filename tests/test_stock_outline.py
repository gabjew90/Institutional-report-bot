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
    assert lines[2].startswith("3. DRIVER: Cloud Memory drives growth")
    # desks: calls and high conviction first, one note per bank, previews tagged
    assert lines[3].startswith("4. DESK: JPMorgan (2026-09-29, written before the report: what it expected): "
                               "positive catalyst watch: Guide above Street on HBM4 demand [JPMorgan; this view only]")
    assert lines[4].startswith("5. DESK: Syz Group (2026-09-29, written before the report")
    assert lines[5] == ("6. POSITIONING: short interest 2.5% of float as of 2026-09-15, 16.7% below its "
                        "52-week high [exchange and filing data]")
    assert "a figure from the news or the company report is never a bank's" in text


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


def test_no_outline_for_other_shapes_or_too_little_data():
    assert O.build_outline(_results(), "price", "MU") == ""
    assert O.build_outline({R.T_PRICE: PRICE}, "ticker_opinion", "MU") == ""
    assert O.outline_symbol([(R.T_CHAIN, {"symbol": "ACN"}), (R.T_SNAPSHOT, {"symbol": "acn"})]) == "ACN"
