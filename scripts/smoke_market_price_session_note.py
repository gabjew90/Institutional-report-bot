"""Smoke test for lookup_market_price session_note + AH data caveat.

Background: 2026-06-02 evening — Wock asked about TSLA/GTLB after-hours.
Finnhub /quote returns the 4 PM regular-session close, NOT a live
extended-hours print. The previous tool response sent Gemini just
'session=AFTER-HOURS' + price, with no warning that the price WAS
the 4 PM close. Gemini phrased it as 'after-hours at $X' which is
wrong. Especially bad for GTLB tonight — it reported earnings AMC
and the cash close is from BEFORE the print.

Fix: surface the existing _session_label note + add a stock-specific
data-quality caveat for AH / PRE / WEEKEND that warns Gemini the
price is the cash close, not the extended-hours print.
"""

import asyncio
import sys
from datetime import datetime
from unittest.mock import patch, MagicMock


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _stock_quote_returns():
    return {"price": 245.30, "prev_close": 248.10, "change_pct": -1.13}


def test_response_includes_session_note():
    """Top-level `session_note` must be present and non-empty whenever
    session classification succeeded. Previously the note was discarded
    via `_, _note = _session_label(...)` — Wock's AH bug case."""
    import discord_bot.bot as bot_mod
    with patch("report.market_data._fetch_finnhub_quote",
               return_value=_stock_quote_returns()):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["TSLA"]}))
    assert "session_note" in result, f"session_note missing: {list(result.keys())}"
    assert result["session_note"], "session_note should not be empty string"
    _ok("lookup_market_price response includes top-level session_note")


def test_after_hours_caveat_present():
    """During AFTER-HOURS, when Yahoo extended-hours data is NOT
    available (rate-limit, illiquid name, no AH trades), the executor
    falls back to Finnhub /quote and `stock_quote_data_caveat` must
    explicitly tell Gemini the price is the 4 PM cash close.

    Patches Yahoo to return None so the executor exercises the Finnhub
    fallback path (the path Fix A is responsible for)."""
    import discord_bot.bot as bot_mod
    fake_now_et = datetime(2026, 6, 2, 20, 0, 0)

    def fake_session_label(_now_et):
        return ("AFTER-HOURS",
                "Markets CLOSED — after-hours (post 4 PM ET). "
                "Traditional-market % reflects today's full regular session (final).")

    with (
        patch("report.market_data._session_label",
              side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=None),
        patch("report.market_data._fetch_finnhub_quote",
              return_value=_stock_quote_returns()),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["GTLB"]}))
    assert result.get("session") == "AFTER-HOURS", result.get("session")
    caveat = result.get("stock_quote_data_caveat") or ""
    assert "4 PM" in caveat or "regular-session" in caveat.lower(), (
        f"AH caveat missing the '4 PM close' framing: {caveat!r}"
    )
    assert "not" in caveat.lower(), (
        "AH caveat should explicitly tell the model NOT to phrase as 'after-hours at $X'"
    )
    _ok("AFTER-HOURS response carries explicit cash-close caveat for stocks")


def test_pre_market_caveat_present():
    """PRE-MARKET fallback caveat path — Yahoo unavailable, Finnhub returns
    yesterday's regular close."""
    import discord_bot.bot as bot_mod
    fake_now_et = datetime(2026, 6, 3, 7, 0, 0)  # 7 AM ET

    def fake_session_label(_now_et):
        return ("PRE-MARKET",
                "Markets PRE-OPEN (before 9:30 AM ET).")

    with (
        patch("report.market_data._session_label",
              side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=None),
        patch("report.market_data._fetch_finnhub_quote",
              return_value=_stock_quote_returns()),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["NVDA"]}))
    assert result.get("session") == "PRE-MARKET"
    caveat = result.get("stock_quote_data_caveat") or ""
    assert "yesterday" in caveat.lower(), (
        f"PRE-MARKET caveat should reference yesterday's close: {caveat!r}"
    )
    _ok("PRE-MARKET response carries explicit yesterday's-close caveat")


def test_open_session_no_stock_caveat_set():
    """During OPEN, Finnhub /quote IS live — no caveat needed."""
    import discord_bot.bot as bot_mod
    fake_now_et = datetime(2026, 6, 2, 13, 0, 0)  # 1 PM ET

    def fake_session_label(_now_et):
        return ("OPEN", "Markets currently OPEN.")

    with (
        patch("report.market_data._session_label",
              side_effect=fake_session_label),
        patch("report.market_data._fetch_finnhub_quote",
              return_value=_stock_quote_returns()),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["SPY"]}))
    assert result.get("session") == "OPEN"
    # During OPEN the caveat should be empty/None (no data-quality issue)
    assert not result.get("stock_quote_data_caveat"), (
        f"OPEN session should have no stock_quote_data_caveat, got: "
        f"{result.get('stock_quote_data_caveat')!r}"
    )
    _ok("OPEN session: no stock_quote_data_caveat (Finnhub IS live mid-session)")


def test_crypto_still_live_regardless_of_session():
    """Crypto via Binance.US is always live. The caveat targets stocks
    only — it should not falsely imply crypto is stale. Patches
    _fetch_yahoo_extended_hours to None so the test doesn't depend on
    whether there happen to be any stocks in the batch."""
    import discord_bot.bot as bot_mod
    fake_now_et = datetime(2026, 6, 2, 20, 0, 0)  # AH

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED.")

    def fake_binance(symbol):
        return {"price": 68200.5, "change_24h_rolling": 2.4}

    with (
        patch("report.market_data._session_label",
              side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=None),
        patch("report.market_data._fetch_binance_24h", side_effect=fake_binance),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["BTC"]}))
    caveat = result.get("stock_quote_data_caveat") or ""
    assert "crypto" in caveat.lower() and "live" in caveat.lower(), (
        f"AH caveat should explicitly carve out crypto as live: {caveat!r}"
    )
    _ok("AH caveat explicitly carves out crypto as still live (no false-stale)")


def test_tool_description_warns_about_extended_hours():
    """The FunctionDeclaration's description is what Gemini reads when
    choosing whether to call the tool. The 'extended hours not in feed'
    warning must be in there — that's how Gemini learns to disclaim
    AH prints rather than parrot them as live."""
    import discord_bot.bot as bot_mod
    tool = bot_mod._build_market_price_tool()
    decl = tool.function_declarations[0]
    desc = decl.description
    assert "stock_quote_data_caveat" in desc, (
        "tool description should reference the new caveat field by name "
        "so Gemini knows to read it"
    )
    assert "extended-hours" in desc.lower() or "after-hours" in desc.lower(), (
        "tool description should explicitly warn about extended-hours data"
    )
    _ok("tool description references stock_quote_data_caveat + extended-hours warning")


if __name__ == "__main__":
    print("=== lookup_market_price session_note + AH caveat smoke ===")
    test_response_includes_session_note()
    test_after_hours_caveat_present()
    test_pre_market_caveat_present()
    test_open_session_no_stock_caveat_set()
    test_crypto_still_live_regardless_of_session()
    test_tool_description_warns_about_extended_hours()
    print("\nALL MARKET-PRICE SESSION-NOTE SMOKE TESTS PASS")
