"""Smoke: lookup_market_price resolves crypto dynamically (2026-07-29).

The crypto side used to be a hardcoded 10-coin allowlist (BTC/ETH/SOL/
DOGE/ADA/AVAX/MATIC/XRP/BNB/LINK). Any other coin — SUI, PEPE, WIF,
TON, ARB, OP, new listings — fell through to the STOCK path and
returned nothing, a live miss in a crypto room. Fix: any symbol that
isn't a known-crypto major and isn't a valid US stock gets a Binance.US
{SYM}USDT fallback; if that also misses, a clean 'no live feed' status
instead of an opaque error or a fabricated stock quote.
"""

import asyncio
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_crypto_quote_builds_dict():
    import discord_bot.bot as bot
    from report import market_data as md
    with patch.object(md, "_fetch_binance_24h",
                      return_value={"price": 3.42, "change_24h_rolling": 5.1}):
        q = asyncio.run(bot._crypto_quote("SUI"))
    assert q and q["source"] == "binance" and q["price"] == 3.42, q
    assert q["data_freshness"] == "live_24_7"
    _ok("_crypto_quote builds a proper crypto quote dict")


def test_crypto_quote_none_on_miss():
    import discord_bot.bot as bot
    from report import market_data as md
    with patch.object(md, "_fetch_binance_24h", return_value=None):
        assert asyncio.run(bot._crypto_quote("ZZZZ")) is None
    _ok("_crypto_quote returns None when Binance has no pair")


def _run_price(symbol, *, finnhub, binance):
    import discord_bot.bot as bot
    from report import market_data as md
    with (
        patch.object(md, "_session_label", return_value=("OPEN", "")),
        patch.object(md, "_fetch_finnhub_quote", return_value=finnhub),
        patch.object(md, "_fetch_binance_24h", return_value=binance),
    ):
        return asyncio.run(bot._execute_market_price({"symbols": [symbol]}))


def test_unknown_symbol_falls_back_to_crypto():
    # not a known-crypto major, not a valid stock, but a real Binance pair
    res = _run_price(
        "PEPE", finnhub=None,
        binance={"price": 0.0000012, "change_24h_rolling": 9.0},
    )
    q = res["quotes"][0]
    assert q.get("source") == "binance", f"should resolve as crypto: {q}"
    assert "error" not in q, q
    _ok("unknown coin (not stock, valid Binance pair) resolves as crypto")


def test_truly_unknown_returns_clean_nofeed():
    res = _run_price("ZZZZ", finnhub=None, binance=None)
    q = res["quotes"][0]
    assert "error" in q, q
    assert "live feed" in q["error"].lower() or "not" in q["error"].lower(), (
        f"error must read as a clean no-feed, not opaque: {q}"
    )
    _ok("truly unknown symbol returns a clean no-live-feed status")


def test_known_stock_still_works():
    res = _run_price(
        "AAPL",
        finnhub={"price": 210.0, "change_pct": 1.2, "prev_close": 207.5},
        binance=None,
    )
    q = res["quotes"][0]
    assert q.get("source") == "finnhub" and q["price"] == 210.0, q
    _ok("valid stock still routes to Finnhub, no crypto fallback fired")


if __name__ == "__main__":
    print("=== dynamic crypto resolution smoke ===")
    test_crypto_quote_builds_dict()
    test_crypto_quote_none_on_miss()
    test_unknown_symbol_falls_back_to_crypto()
    test_truly_unknown_returns_clean_nofeed()
    test_known_stock_still_works()
    print("\nALL DYNAMIC CRYPTO SMOKE TESTS PASS")
