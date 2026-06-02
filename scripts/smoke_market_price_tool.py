"""Smoke test for the lookup_market_price tool.

Validates:
  1. Tool definition shape
  2. Routing: known crypto -> Binance; everything else -> Finnhub
  3. Validation: empty symbols list -> error
  4. Validation: > 10 symbols -> truncate with warning
  5. Session field populated from report.market_data._session_label
  6. Per-symbol error doesn't sink the batch
"""

import asyncio
import sys
from unittest.mock import patch

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_definition_shape():
    tool = bot_mod._build_market_price_tool()
    decl = tool.function_declarations[0]
    assert decl.name == "lookup_market_price", f"unexpected name: {decl.name}"
    assert "symbols" in decl.parameters.properties
    _ok("_build_market_price_tool: name + symbols param present")


def test_empty_symbols_error():
    result = asyncio.run(bot_mod._execute_market_price({"symbols": []}))
    assert "error" in result, f"expected error, got {result}"
    _ok("empty symbols -> error")


def test_missing_symbols_error():
    result = asyncio.run(bot_mod._execute_market_price({}))
    assert "error" in result, f"expected error, got {result}"
    _ok("missing symbols -> error")


def test_finnhub_route_for_stock():
    finnhub_data = {"price": 598.42, "prev_close": 596.40, "change_pct": 0.34}
    with (
        patch("report.market_data._fetch_finnhub_quote", return_value=finnhub_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["SPY"]}))
    assert result.get("session") == "OPEN", result
    quote = result["quotes"][0]
    assert quote["symbol"] == "SPY", quote
    assert quote.get("source") == "finnhub", quote
    assert quote.get("price") == 598.42, quote
    _ok("stock symbol routes to Finnhub")


def test_binance_route_for_crypto():
    binance_data = {"price": 109423.10, "change_24h_rolling": -1.2}
    with (
        patch("report.market_data._fetch_binance_24h", return_value=binance_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["BTC"]}))
    quote = result["quotes"][0]
    assert quote["symbol"] == "BTC", quote
    assert quote.get("source") == "binance", quote
    assert quote.get("price") == 109423.10, quote
    _ok("crypto symbol routes to Binance.US")


def test_per_symbol_error_does_not_sink_batch():
    finnhub_data = {"price": 598.42, "prev_close": 596.40, "change_pct": 0.34}
    with (
        # Finnhub returns None for unknown symbol
        patch(
            "report.market_data._fetch_finnhub_quote",
            side_effect=lambda sym: finnhub_data if sym == "SPY" else None,
        ),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({
            "symbols": ["SPY", "ASDF"],
        }))
    quotes = result["quotes"]
    assert len(quotes) == 2, quotes
    assert quotes[0]["symbol"] == "SPY" and "price" in quotes[0]
    assert quotes[1]["symbol"] == "ASDF" and "error" in quotes[1]
    _ok("unknown symbol gets per-symbol error; other symbols still resolve")


def test_truncate_over_ten():
    finnhub_data = {"price": 1.0, "prev_close": 1.0, "change_pct": 0.0}
    with (
        patch("report.market_data._fetch_finnhub_quote", return_value=finnhub_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({
            "symbols": [f"SYM{i}" for i in range(15)],
        }))
    assert len(result["quotes"]) == 10, f"expected 10 quotes, got {len(result['quotes'])}"
    assert result.get("truncated_to") == 10, result
    _ok("> 10 symbols -> truncated to 10 with warning field")


if __name__ == "__main__":
    print("=== lookup_market_price tool smoke ===")
    test_tool_definition_shape()
    test_empty_symbols_error()
    test_missing_symbols_error()
    test_finnhub_route_for_stock()
    test_binance_route_for_crypto()
    test_per_symbol_error_does_not_sink_batch()
    test_truncate_over_ten()
    print("\nALL MARKET-PRICE-TOOL SMOKE TESTS PASS")
