"""Smoke: OHLC in price history + candlestick guidance for price charts.

2026-07-29: the room's charting bot (Tree Capital, `fc <ticker> <tf>`)
posts TradingView-style candlesticks with a volume panel — the familiar
visual language for traders. Our bot draws its own charts now
(lookup_price_history + code execution), but the tool returned only
close/volume, so a candlestick was not even drawable and price charts
came out as plain lines.

Fix: return full OHLC, and tell the analysis directive that a
PRICE/ticker chart should render as candlesticks with a volume panel
(matplotlib can do it without mplfinance, which the sandbox lacks).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_history_returns_ohlc():
    import inspect
    from report import market_data as md
    src = inspect.getsource(md.fetch_price_history)
    for k in ('"open"', '"high"', '"low"', '"close"', '"volume"'):
        assert k in src, f"price history must carry {k} for candlesticks"
    _ok("fetch_price_history returns full OHLC + volume")


def test_price_move_still_uses_close():
    """Scoring must keep using CLOSE — adding OHLC can't change the
    exit-scoring math the trade board depends on."""
    from unittest.mock import patch
    from report import market_data as md
    bars = [
        {"date": "2026-07-20", "open": 50.0, "high": 51.0, "low": 49.0,
         "close": 49.32, "volume": 100},
        {"date": "2026-07-29", "open": 47.0, "high": 47.5, "low": 46.0,
         "close": 46.74, "volume": 120},
    ]
    with patch.object(md, "fetch_price_history", return_value=bars):
        mv = md.price_move_since("BNO", "2026-07-20")
    assert mv and abs(mv["pct"] - (-5.23)) < 0.05, mv
    _ok("price_move_since still computed from CLOSE (scoring unchanged)")


def test_directive_asks_for_candlesticks():
    from discord_bot.bot import _ASK_ANALYSIS_DIRECTIVE as d
    low = d.lower()
    assert "candlestick" in low, (
        "directive must ask for candlesticks on price/ticker charts"
    )
    assert "volume" in low, "directive must ask for a volume panel"
    _ok("analysis directive requests candlesticks + volume for price charts")


def test_tool_declaration_mentions_ohlc():
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._build_price_history_tool)
    assert "ohlc" in src.lower() or "open" in src.lower(), (
        "tool description should advertise OHLC so the model draws candles"
    )
    _ok("lookup_price_history advertises OHLC")


if __name__ == "__main__":
    print("=== candlestick charts smoke ===")
    test_history_returns_ohlc()
    test_price_move_still_uses_close()
    test_directive_asks_for_candlesticks()
    test_tool_declaration_mentions_ohlc()
    print("\nALL CANDLESTICK SMOKE TESTS PASS")
