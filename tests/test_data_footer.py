"""Tool-sourced /ask answers name their feeds (2026-09-30)."""
from discord_bot import data_footer as F


def test_feeds_are_listed_once_in_first_use_order():
    trace = [
        {"tool": "lookup_earnings_date", "args": {}, "status": "ok"},
        {"tool": "lookup_market_price", "args": {}, "status": "ok"},
        {"tool": "search_chat_messages", "args": {}, "status": "ok"},
        {"tool": "lookup_earnings_slate", "args": {}, "status": "ok"},   # same feed, no repeat
    ]
    assert F.footer(trace) == (
        "\n\nData: earnings calendar (Finnhub, Nasdaq) · live prices (Finnhub, Binance.US)")
    assert F.footer([{"tool": "search_chat_messages", "status": "ok"}]) == ""


def test_failed_calls_and_unknown_tools_are_left_out():
    trace = [
        {"tool": "lookup_market_price", "status": "error"},
        {"tool": "search_chat_messages", "status": "empty"},
        {"tool": "some_new_tool", "status": "ok"},
        "garbage",
    ]
    assert F.footer(trace) == ""
    assert F.footer(None) == "" and F.footer([]) == ""


def test_backstop_fetch_counts_as_data():
    assert "live prices" in F.footer([{"tool": "lookup_market_price", "status": "backstop-fetch"}])


def test_every_declared_ask_tool_has_a_feed_label():
    import re
    from pathlib import Path
    src = Path("discord_bot/ask_tools.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'^\s+name="((?:lookup|search|query)_\w+)"', src, re.M))
    assert declared, "tool declarations not found"
    assert declared <= set(F.FEEDS) | F.NOT_CITED, declared - set(F.FEEDS) - F.NOT_CITED
