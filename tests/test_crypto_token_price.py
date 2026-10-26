"""A coin that shares a stock's symbol is priced as the coin when the
asker says token or coin (2026-10-26 audit A5: "$tree token")."""
import asyncio

from discord_bot import ask_router as R
from discord_bot import ask_tools as T
from report import market_data as md


def test_token_routes_to_a_crypto_price():
    r = R.classify("$tree token")
    assert r.shape == R.PRICE and r.prefetch == [(R.T_PRICE, {"symbols": ["TREE"], "crypto": True})]
    assert R.classify("should i buy NVDA or coins").shape != R.PRICE


def test_crypto_flag_prices_the_coin_not_the_stock(monkeypatch):
    monkeypatch.setattr(md, "_fetch_binance_24h", lambda s: None)
    monkeypatch.setattr(md, "_fetch_coingecko_by_symbol", lambda s: {
        "price": 0.1528, "change_24h": -0.08, "market_cap": 13.8e6, "name": "Tree Capital",
        "other_coins_with_symbol": ["Treehouse"]})
    monkeypatch.setattr(md, "_fetch_finnhub_quote", lambda s: {"price": 61.0, "change_pct": 1.0})
    monkeypatch.setattr(md, "_fetch_yahoo_extended_hours", lambda s: None)
    out = asyncio.run(T._execute_market_price({"symbols": ["TREE"], "crypto": True}))
    q = out["quotes"][0]
    assert q["source"] == "coingecko" and q["price"] == 0.1528 and q["name"] == "Tree Capital"
    # without the flag a stock symbol stays the stock, and CoinGecko is never asked
    monkeypatch.setattr(md, "_fetch_coingecko_by_symbol", lambda s: (_ for _ in ()).throw(AssertionError))
    out = asyncio.run(T._execute_market_price({"symbols": ["TREE"]}))
    assert out["quotes"][0]["price"] == 61.0
