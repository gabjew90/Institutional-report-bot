"""The code-built body for price and company-data questions
(discord_bot/price_block.py), from the 2026-11-05 audit."""
from datetime import date

from discord_bot import answer_block, price_block as pb


def _qcom():
    price = {"status": "ok", "session": "REGULAR", "quotes": [
        {"symbol": "QCOM", "price": 164.31, "change_pct": 1.42, "data_freshness": "live_regular_session"}]}
    snap = {"status": "ok", "symbol": "QCOM", "name": "QUALCOMM Incorporated",
            "market_cap": 175.3e9, "price": 164.31, "week52_high": 182.10, "week52_low": 120.80,
            "pct_below_52w_high": 9.8, "total_cash": 8.304e9, "total_debt": 15.3e9,
            "operating_cashflow_ttm": 12.1e9, "capex_ttm": 1.2e9, "ocf_minus_capex_ttm": 10.9e9,
            "cashflow_period_end": "2026-06-28", "offering_filings_12m": [], "as_of": "x"}
    return price, snap


def test_cash_is_labelled_as_what_yahoo_counts():
    # 2026-11-03: "Total cash and equivalents $8.3B"; cash alone was $4.53B
    price, snap = _qcom()
    lines = pb.block_lines(price, snap, "qcom net cash flow 12 months basis and debts")
    assert lines[0] == "→ **QCOM** at **$164.31**, +1.4% today."
    assert lines[1].startswith("→ **Balance sheet:** cash and short-term investments $8.3B, debt $15.3B, "
                               "operating cash flow $12.1B")
    assert "four quarters to 2026-06-28" in lines[1]


def test_a_plain_price_question_gets_cap_and_range():
    price, snap = _qcom()
    assert pb.block_lines(price, snap, "qcom price") == [
        "→ **QCOM** at **$164.31**, +1.4% today.",
        "→ Market cap $175.3B, 9.8% below its 52-week high of $182.10."]


def test_several_quotes_and_coins_and_yields():
    price = {"status": "ok", "session": "AFTER-HOURS", "quotes": [
        {"symbol": "NVDA", "price": 190.0, "change_pct": -1.0, "regular_session_close": 192.0,
         "extended_hours_change_pct": -1.04, "data_freshness": "live_extended_hours"},
        {"symbol": "BTC", "price": 101234.5, "change_pct": 2.31, "source": "binance"},
        {"symbol": "^TNX", "price": 4.12, "yield_pct": 4.12, "change_bps": 3.0, "source": "yahoo"}]}
    assert pb.block_lines(price, None, "nvda btc and the 10 year") == [
        "→ **NVDA** closed at **$192.00** (-1.0% on the day), **$190.00** after hours, -1.0%.",
        "→ **BTC** **$101,234.50**, +2.3% over 24 hours.",
        "→ **10-year Treasury yield** at **4.12%**, +3.0 bps."]
    # an index level is points, under the room's name for it
    spx = {"status": "ok", "session": "REGULAR", "quotes": [
        {"symbol": "^GSPC", "price": 6812.3, "change_pct": 0.41, "source": "yahoo"}]}
    assert pb.block_lines(spx, None, "what's spx at") == ["→ **SPX** at **6,812.30**, +0.4% today."]


def test_what_the_lines_cannot_answer_goes_to_the_model():
    price, snap = _qcom()
    assert pb.block_lines(price, snap, "qcom price and why is it up") == []
    bad = dict(price, quotes=price["quotes"] + [{"symbol": "XYZQ", "error": "no live feed"}])
    assert pb.block_lines(bad, snap, "qcom and xyzq price") == []
    vol = dict(price, quotes=[dict(price["quotes"][0], volume={"today": 1})])
    assert pb.block_lines(vol, snap, "qcom volume") == []
    two = dict(price, quotes=price["quotes"] + [dict(price["quotes"][0], symbol="AVGO")])
    assert pb.block_lines(two, snap, "qcom vs avgo market cap") == []
    # a field the snapshot lacks
    assert pb.block_lines(price, dict(snap, total_cash=None, total_debt=None,
                                      operating_cashflow_ttm=None), "qcom debt") == []


def test_answer_block_builds_it_on_the_price_shape_only():
    price, snap = _qcom()
    ctx = {"shape": "price", "snapshot": snap}
    blk = answer_block.build("lookup_market_price", price, date(2026, 11, 3),
                             question="qcom market cap", context=ctx)
    assert blk["lines"] == ["→ **QCOM** at **$164.31**, +1.4% today.",
                            "→ **Market cap:** $175.3B."]
    assert answer_block.build("lookup_market_price", price, date(2026, 11, 3),
                              question="qcom market cap", context=dict(ctx, shape="ticker_opinion")) is None
