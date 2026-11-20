"""report/credited_calls.py: which rating and target the pulse may credit a note with.

2026-11-20: the TRADE BOARD published "Natixis $VLVLY · UP" from a morning
briefing that reported a Moody's credit downgrade. The pulse now reads each
market mover through /ask's rule (discord_bot/desk_line.fields), the way
/ask's research lookup hands it a note. The notes below are shaped like the
stored ones behind that week's boards.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ai_analysis.models import EntityMention, MarketMover, PdfAnalysis  # noqa: E402
from report import credited_calls as C  # noqa: E402


def _mover(**kw):
    base = {"ticker": "NVDA", "action": "reiterate", "rating": "Buy", "price_target": "$280",
            "rationale": "Compelling valuation and strong growth outlook.", "conviction": "high"}
    return {**base, **kw}


def test_a_house_keeps_the_call_on_its_own_equity_research():
    kept = C.credit_mover("Bank of America", "equity_research", _mover(price_target="$280.00"))
    assert (kept["rating"], kept["price_target"], kept["action"]) == ("Buy", "$280", "reiterate")


def test_a_credit_downgrade_in_a_briefing_is_no_stock_rating():
    m = _mover(ticker="VLVLY", action="downgrade", rating="Underperform", price_target="N/A",
               rationale="Moody's cut rating to Ba2 from Ba1 due to weaker profitability and rising debt.")
    kept = C.credit_mover("Natixis", "morning_briefing", m,
                          insights=["Moody's downgraded Volvo Cars to Ba2 from Ba1, citing weak profitability."],
                          company="Volvo Cars")
    assert (kept["rating"], kept["price_target"], kept["action"]) == ("", "", "")
    # what the note said is still there for the writer
    assert kept["rationale"].startswith("Moody's cut rating") and kept["ticker"] == "VLVLY"


def test_outside_equity_research_the_note_must_name_the_broker_and_state_it():
    m = _mover(ticker="SPCX", action="price_target_change", rating="Buy", price_target="$280",
               rationale="Successful test flight and increased conviction in the segment outlook.")
    kept = C.credit_mover(
        "Citi", "morning_briefing", m,
        insights=["Space Exploration Technologies (SPCX) completed its test flight, leading Citi to raise "
                  "estimates and price target to $280."],
        company="Space Exploration Technologies")
    # the target is stated by Citi about this stock, the rating is not stated anywhere
    assert (kept["rating"], kept["price_target"], kept["action"]) == ("", "$280", "price_target_change")


def test_a_sentence_about_something_else_credits_no_stock_rating():
    # 9 stored ratings in 21 days: "Overweight" on a stock from a sentence
    # about the house's asset allocation
    m = _mover(rating="Overweight", price_target="N/A", rationale="Favored for AI compute exposure.")
    kept = C.credit_mover(
        "Goldman Sachs", "sales_trading", m,
        insights=["Goldman Sachs remains Overweight equities on a 3m/12m horizon, citing earnings support."],
        company="Nvidia")
    assert kept["rating"] == "" and kept["action"] == ""


def test_another_brokers_action_relayed_is_not_this_notes_call():
    m = _mover(ticker="MRK", action="downgrade", rating="Hold", price_target="N/A",
               rationale="Downgraded to Hold at Deutsche Bank.")
    kept = C.credit_mover("Goldman Sachs", "morning_briefing", m, company="Merck")
    assert (kept["rating"], kept["action"]) == ("", "")


def test_a_newsletter_is_credited_with_no_rating():
    m = _mover(ticker="INTC", rating="Buy", price_target="N/A",
               rationale="The report identifies a capitulation in the shares and expects a bounce.")
    assert C.credit_mover("The Bear Traps Report", "strategy", m, company="Intel")["rating"] == ""


def test_a_target_that_is_not_one_price_is_dropped_everywhere():
    kept = C.credit_mover("Morgan Stanley", "equity_research", _mover(price_target="31% upside"))
    assert kept["rating"] == "Buy" and kept["price_target"] == ""


def _analysis(source, report_type, movers, insights=(), name="Nvidia"):
    return PdfAnalysis(
        pdf_file_id=1, file_name="f.pdf", source=source, title="t", report_type=report_type, priority="high",
        key_insights=list(insights), market_movers=[MarketMover(**m) for m in movers],
        entities_mentioned=[EntityMention(name=name, ticker=movers[0]["ticker"], asset_class="stock")])


def test_credit_changes_copies_and_leaves_the_input_alone():
    a = _analysis("Natixis", "morning_briefing",
                  [_mover(ticker="VLVLY", action="downgrade", rating="Underperform", price_target="N/A",
                          rationale="Moody's cut rating to Ba2 from Ba1.")], name="Volvo Cars")
    b = _analysis("Bank of America", "equity_research", [_mover()])
    out = C.credit([a, b])
    assert a.market_movers[0].rating == "Underperform"          # input untouched
    assert out[0].market_movers[0].rating == "" and out[0].market_movers[0].action == ""
    assert out[1].market_movers[0].rating == "Buy" and out[1].market_movers[0].price_target == "$280"


def test_the_pulse_context_and_the_board_read_the_credited_fields():
    from report import synthesizer as S
    from report.pulse_sections import _hc_call_lines, extract_state_from_ctx
    a = _analysis("Natixis", "morning_briefing",
                  [_mover(ticker="VLVLY", action="downgrade", rating="Underperform", price_target="N/A",
                          rationale="Moody's cut rating to Ba2 from Ba1.")], name="Volvo Cars")
    b = _analysis("Bank of America", "equity_research", [_mover()])
    credited = C.credit([a, b])
    blob = S._analyses_to_json(credited)
    entries = json.loads(blob)
    assert "HIGH_CONVICTION" not in entries[0] and entries[0]["market_movers"][0]["rationale"]
    assert entries[1]["HIGH_CONVICTION"] == ["NVDA: reiterate (Buy)"]
    # the high-conviction block lists calls, and the first is no longer one
    block = S._format_high_conviction_calls(credited)
    assert "$NVDA reiterate Buy PT $280" in block and "VLVLY" not in block
    lines = _hc_call_lines(extract_state_from_ctx({"analyses_json": blob})["hc_calls"])
    assert [ln for ln in lines if ln.startswith("- ")] == [
        "- **Bank of America** $NVDA · Buy, PT $280 · Compelling valuation and strong growth outlook."]


def test_an_action_enum_is_not_printed_on_the_board():
    from report.pulse_sections import _hc_call_lines
    lines = _hc_call_lines([{"source": "Citi", "ticker": "SPCX", "action": "price_target_change",
                             "rating": "", "pt": "$280", "rationale": "Successful test flight."}])
    assert lines[0] == "- **Citi** $SPCX · PT $280 · Successful test flight."
    # a plain action word still stands in for a missing rating
    up = _hc_call_lines([{"source": "Citi", "ticker": "DELL", "action": "upgrade", "rating": "",
                          "pt": "$550", "rationale": "x"}])
    assert up[0].startswith("- **Citi** $DELL · upgrade, PT $550")


def test_no_ticker_matches_no_line():
    import db  # noqa: F401
    from db_parts.pdf import about_matcher
    assert not about_matcher("", "Nvidia")("Nvidia beat estimates")
    about = about_matcher("MU", "Micron Technology")
    assert about("MU guided above") and about("Micron raised capex") and not about("must hold 400")
