"""Margin-call arithmetic in code (2026-10-26 audit A16)."""
import asyncio

from discord_bot import ask_router as R
from discord_bot import ask_tools as T
from discord_bot import margin_math as M

Q = "Will Wock get a margin call if he full-ported 25k$ 4x margin, NBIS$175, and dram $56?"


def test_the_question_parses_and_routes():
    r = R.classify(Q)
    assert r.shape == R.PRICE and r.reason == "margin math"
    assert r.prefetch == [(R.T_MARGIN, {"leverage": 4.0, "account": 25000.0, "positions": [
        {"symbol": "NBIS", "entry": 175.0}, {"symbol": "DRAM", "entry": 56.0}]})]
    assert R.classify("my margin is great").shape == R.UNKNOWN


def test_call_thresholds():
    assert M.call_drop_pct(2, 0.25) == 33.3
    assert M.call_drop_pct(4, 0.25) == 0.0
    assert M.call_drop_pct(4, 0.30) == -7.1


def test_the_executor_prices_and_computes(monkeypatch):
    async def fake_price(args):
        return {"quotes": [{"symbol": "NBIS", "price": 157.0}, {"symbol": "DRAM", "price": 52.0}]}
    monkeypatch.setattr(T, "_execute_market_price", fake_price)
    out = asyncio.run(T._execute_margin_math(R.classify(Q).prefetch[0][1]))
    assert out["equity_now"] == 16286 and out["equity_now_pct_of_position"] == 17.8
    assert out["below_maintenance_now"]["25%"] is True
    assert out["assumption"].startswith("the gross position split equally")


def test_profit_margin_with_a_multiple_is_not_margin_math():
    """Code review 2026-10-26: a valuation question about gross margin."""
    assert R.classify("is MU cheap at 10x with that gross margin").reason != "margin math"
    assert R.classify("margin call on 2x TQQQ").reason == "margin math"


def test_block_lines_give_the_verdict(monkeypatch):
    """The answer body is rendered in code (2026-10-27, answer_block)."""
    async def fake_price(args):
        return {"quotes": [{"symbol": "NBIS", "price": 157.0}, {"symbol": "DRAM", "price": 52.0}]}
    monkeypatch.setattr(T, "_execute_market_price", fake_price)
    out = asyncio.run(T._execute_margin_math(R.classify(Q).prefetch[0][1]))
    lines = M.block_lines(out)
    assert lines[0] == ("→ **The setup:** $25,000 at 4x is a $100,000 position with $75,000 "
                        "borrowed, taking the gross position split equally across NBIS, DRAM "
                        "(the question gives no split).")
    assert "equity $16,286 (17.8% of the position)" in lines[1]
    assert lines[2] == "→ **Margin call:** yes. Equity at 17.8% is under 25%, 30% and 40% maintenance."
    assert lines[3].startswith("→ **The rules:** Federal Reserve Regulation T")
    assert not any("—" in ln or ";" in ln for ln in lines)


def test_block_lines_count_down_to_the_call():
    out = M.compute(2.0, 10000.0, [{"symbol": "AAPL", "entry": 200.0}], {"AAPL": 190.0})
    verdict = M.block_lines(out)[2]
    assert verdict.startswith("→ **Margin call:** not yet. A further drop from here triggers it at ")
    # 2x, down 5%: the 25% call sits at 0.5/0.75 = 0.667 of entry, 29.8% under 0.95
    assert "29.8% for 25%" in verdict


def test_block_lines_without_prices():
    lines = M.block_lines(M.compute(4.0, None, [], {}))
    assert lines[0] == "→ **The setup:** At 4x, equity is 25.0% of the position at entry."
    assert "25%: any drop at all" in lines[1] and "30%: under it from the start" in lines[1]
