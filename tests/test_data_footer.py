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
        "\n\nData: earnings calendar (Finnhub, Nasdaq) · live prices (Finnhub, Binance.US, Yahoo)")
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


# ---- 2026-10-08 audit ----
from discord_bot import data_footer as _DF


def test_grounded_answers_keep_the_data_line():
    trace = [{"tool": "lookup_price_history", "status": "ok", "figs": ["212.4"]}]
    out = _DF.compose("\n\nSources: [1] trefis.com", "AMZN is at $212.40, up 81% in five years",
                      None, trace)
    assert out.startswith("\n\nSources: [1] trefis.com") and "Data: price history (Yahoo)" in out


def test_a_figure_feed_is_named_only_when_its_numbers_are_used():
    trace = [{"tool": "lookup_market_price", "status": "ok", "figs": ["405.42", "1.4"]},
             {"tool": "lookup_research", "status": "ok", "figs": ["12.5"]}]
    used = _DF.footer(trace, "WDC closed at $405 down 1.4%")
    assert "live prices" in used and "bank research" in used
    unused = _DF.footer(trace, "kloh says gamma flips at 6500")
    assert "live prices" not in unused and "bank research" in unused
    # entries without recorded numbers keep the old behavior
    assert "live prices" in _DF.footer([{"tool": "lookup_market_price", "status": "ok"}], "no figures")


def test_a_chat_query_is_not_the_research_database():
    trace = [{"tool": "query_data", "status": "ok",
              "args": {"sql": "SELECT content FROM chat_messages WHERE ..."}}]
    assert _DF.footer(trace, "Owen and spockbones argued") == ""


def test_a_league_record_counts_as_used():
    trace = [{"tool": "lookup_fantasy_league", "status": "ok",
              "figs": sorted(_DF.payload_figs('{"record": "1-3", "fpts": 488.0}'))}]
    assert "Sleeper league" in _DF.footer(trace, "DeepFried is 1-3 this season")
    assert _DF.footer(trace, "Owen and spockbones argued about APLD") == ""
