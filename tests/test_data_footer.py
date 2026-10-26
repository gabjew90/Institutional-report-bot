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
        "\n\nData: earnings calendar (Finnhub, Nasdaq) · live prices (Finnhub, Binance.US, CoinGecko, Yahoo)")
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


def test_a_probe_query_is_not_a_source_2026_10_11():
    """"Data: research database" was printed for a SELECT 1 probe."""
    from discord_bot.data_footer import footer
    trace = [{"tool": "query_data", "args": {"sql": "SELECT 1"}, "status": "ok"}]
    assert footer(trace, "the top 10 are 38% of the index") == ""
    real = [{"tool": "query_data", "args": {"sql": "SELECT count(*) FROM pdf_files"}, "status": "ok"}]
    assert "research database" in footer(real, "120 notes")


def test_rounded_and_scaled_payload_numbers_count_2026_10_18():
    """Audit A29: "76.9M shares" and "$0.29" came from the snapshot and the
    price feed, and neither was cited."""
    from discord_bot import data_footer as D
    trace = [{"tool": "lookup_ticker_snapshot", "status": "ok", "args": {"symbol": "ACH"},
              "figs": ["76904704"]},
             {"tool": "lookup_market_price", "status": "ok", "args": {"symbols": ["ACH"]},
              "figs": ["0.2912", "-6.9"]}]
    out = D.footer(trace, "→ **76.9M** shares outstanding, priced at **$0.29**.")
    assert "Yahoo" in out or "snapshot" in out.lower() or "company" in out.lower(), out
    assert "price" in out.lower(), out


def test_the_slate_is_cited_on_a_names_only_answer():
    from discord_bot import data_footer as D
    trace = [{"tool": "lookup_earnings_slate", "status": "ok", "args": {"date": "next_week"},
              "figs": ["885149"]}]
    assert D.footer(trace, "→ **Monday**: FITB before the open") != ""


def test_no_footer_on_the_empty_reply_fallback():
    from discord_bot import data_footer as D
    trace = [{"tool": "query_data", "status": "ok", "args": {"sql": "select * from analyst_trades"}}]
    assert D.compose("", "→ No response came back (reason: STOP). Try again or rephrase.", None, trace) == ""


def test_the_models_own_sources_block_is_dropped():
    """Audit F7: #85 printed Sources twice."""
    from discord_bot.data_footer import strip_model_sources
    a = ("→ A **$100 wager** clears **$218,119**.\n\nSources: \n"
         "[actionnetwork.com](<https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZ=>)")
    assert strip_model_sources(a) == "→ A **$100 wager** clears **$218,119**."
    assert strip_model_sources("→ no sources here") == "→ no sources here"


def test_a_model_written_data_line_is_dropped_2026_10_26():
    from discord_bot.data_footer import strip_model_sources
    a = "→ **Netflix** headlines the slate\n\nData: earnings slate (Nasdaq feed)"
    assert strip_model_sources(a) == "→ **Netflix** headlines the slate"
    assert strip_model_sources("→ the Data: line inside text stays") == "→ the Data: line inside text stays"
