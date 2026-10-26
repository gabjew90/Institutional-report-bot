"""A single contract off hours is not a current quote (2026-10-26 audit A25)."""
import asyncio

from discord_bot import ask_tools as T
from report import market_data as md


def _raw(bid, ask):
    return {"expiration_dates": ["2026-10-30"], "underlying_spot_price": 200.0,
            "chain": {"expiration_iso": "2026-10-30", "calls": [
                {"strike": 210.0, "bid": bid, "ask": ask, "openInterest": 0,
                 "impliedVolatility": 1e-05, "lastPrice": 2.15, "volume": 900}], "puts": []}}


def test_zero_bid_and_ask_is_not_live(monkeypatch):
    monkeypatch.setattr(md, "_fetch_yahoo_options_chain", lambda s, expiration_iso=None: _raw(0, 0))
    out = asyncio.run(T._execute_options_chain({"symbol": "NVDA", "strike": 210}))
    c = out["contract"]
    assert c["live_quotes"] is False and c["implied_volatility"] is None and c["open_interest"] is None
    assert "not quoting" in out["quotes_note"] and c["last_price"] == 2.15


def test_live_contract_keeps_its_fields(monkeypatch):
    monkeypatch.setattr(md, "_fetch_yahoo_options_chain", lambda s, expiration_iso=None: _raw(2.1, 2.2))
    out = asyncio.run(T._execute_options_chain({"symbol": "NVDA", "strike": 210}))
    assert out["contract"]["implied_volatility"] == 1e-05 and "quotes_note" not in out
