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
