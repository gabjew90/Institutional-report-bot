"""Smoke test: lookup_earnings_date tool + Google-default rule +
NO-self-generated-TA rule (2026-06-10 three-part shipment).

Background — three findings from the 2026-06-10 ask-log QC:

1. EARNINGS-DATE DODGE (19:10 UTC): asked "when does GEO report
   earnings," the model had no data source — the pulse's earnings
   block whitelists MAG7/banks/bellwethers, so non-whitelist tickers
   were invisible — and it answered with last quarter's RESULTS
   instead of the next DATE. Fix: per-symbol Finnhub earnings-calendar
   tool with NO whitelist (a user naming a ticker IS the filter).

2. GOOGLE-AS-LAST-RESORT: when no tool covered a question the model
   substituted adjacent facts rather than just Googling the actual
   question. Fix: binding GOOGLE IS THE DEFAULT rule in the TOOLS
   preamble — no tool / tool no_data / tool error → Google Search →
   answer the actual question or say it couldn't be found.

3. SELF-GENERATED TA (16:23 / 16:26 / 18:31 UTC): "$27 breakout of
   consolidation zone" ($GEO), "as long as ES holds 7293", "$NOW
   breaks $115" — invented levels with no data source; the bot has no
   chart view. Worse, the prompt's own worked examples TAUGHT this
   ("broke $23 flag on volume", "Setup: long above $23.40 ... Stop
   $22.50") and an explicit carve-out said pure chart questions are
   "TA-led from the start." Fix: binding NO SELF-GENERATED TECHNICAL
   ANALYSIS rule (attributed levels only) + scrub the teaching
   examples + invert the chart-question carve-out.

This smoke covers:
  - news_data.fetch_earnings_date_for_symbol: next/last split, timing
    mapping (bmo/amc/dmh/unknown), symbol filtering, None on fetch
    failure, None on missing key/symbol
  - bot._build_earnings_date_tool: valid declaration, symbol required
  - bot._execute_earnings_date: ok / no_data / error / empty-symbol
    paths; no_data + error payloads instruct Google fallback
  - wiring: registered in BOTH tools=[] arrays + executor map;
    executor uses asyncio.to_thread (event-loop safety)
  - prompt: EIGHT tools, §8 section, Google-default rule, routing
    list updated (earnings date + macro entries), NO-TA rule present,
    old TA-teaching examples gone
"""

import asyncio
import inspect
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# === Fetcher ===

def test_fetcher_signature_and_guards():
    from report import news_data
    sig = inspect.signature(news_data.fetch_earnings_date_for_symbol)
    assert "symbol" in sig.parameters
    with patch("config.settings.finnhub_api_key", ""):
        assert news_data.fetch_earnings_date_for_symbol("GEO") is None
    with patch("config.settings.finnhub_api_key", "test_key"):
        assert news_data.fetch_earnings_date_for_symbol("") is None
    _ok("fetcher: takes symbol; None on missing key / empty symbol")


def test_fetcher_splits_next_and_last():
    """Upcoming row (future date, no epsActual) → next; reported row →
    last. Timing hour codes map to plain English."""
    from report import news_data
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    fake = {
        "earningsCalendar": [
            {"symbol": "GEO",
             "date": (today - timedelta(days=20)).isoformat(),
             "hour": "bmo", "epsActual": 0.25, "epsEstimate": 0.22,
             "revenueEstimate": None},
            {"symbol": "GEO",
             "date": (today + timedelta(days=45)).isoformat(),
             "hour": "amc", "epsActual": None, "epsEstimate": 0.28,
             "revenueEstimate": 610000000},
            # Same-window row for a DIFFERENT symbol must be filtered out
            {"symbol": "GEOX",
             "date": (today + timedelta(days=10)).isoformat(),
             "hour": "bmo", "epsActual": None, "epsEstimate": 1.0,
             "revenueEstimate": None},
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake), \
         patch("config.settings.finnhub_api_key", "test_key"):
        result = news_data.fetch_earnings_date_for_symbol("geo")

    assert result["symbol"] == "GEO"
    assert result["next"] is not None, "future no-actual row must be next"
    assert result["next"]["date"] == (today + timedelta(days=45)).isoformat()
    assert result["next"]["timing"] == "after market close"
    assert result["next"]["eps_estimate"] == 0.28
    assert result["last"] is not None
    assert result["last"]["eps_actual"] == 0.25
    assert result["last"]["eps_estimate"] == 0.22
    _ok("fetcher: next/last split + amc->'after market close' + "
        "other-symbol rows filtered + lowercase input normalized")


def test_fetcher_timing_tbd_and_no_upcoming():
    """Unknown hour code → 'timing TBD'; no future rows → next=None
    (executor turns that into the Google-fallback no_data path)."""
    from report import news_data
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    fake = {
        "earningsCalendar": [
            {"symbol": "NVDA",
             "date": (today - timedelta(days=5)).isoformat(),
             "hour": "", "epsActual": 0.61, "epsEstimate": 0.58,
             "revenueEstimate": None},
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake), \
         patch("config.settings.finnhub_api_key", "test_key"):
        result = news_data.fetch_earnings_date_for_symbol("NVDA")
    assert result["next"] is None
    assert result["last"]["eps_actual"] == 0.61

    fake2 = {
        "earningsCalendar": [
            {"symbol": "NVDA",
             "date": (today + timedelta(days=30)).isoformat(),
             "hour": "weird", "epsActual": None, "epsEstimate": None,
             "revenueEstimate": None},
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake2), \
         patch("config.settings.finnhub_api_key", "test_key"):
        result2 = news_data.fetch_earnings_date_for_symbol("NVDA")
    assert result2["next"]["timing"] == "timing TBD"
    _ok("fetcher: no upcoming rows -> next=None; unknown hour -> "
        "'timing TBD'")


def test_fetcher_none_on_fetch_failure():
    from report import news_data
    with patch("report.news_data._fetch_json", return_value=None), \
         patch("config.settings.finnhub_api_key", "test_key"):
        assert news_data.fetch_earnings_date_for_symbol("GEO") is None
    _ok("fetcher: None on _fetch_json failure (caller distinguishes "
        "failure from no-data)")


# === Tool builder + executor ===

def test_tool_builder_returns_valid_declaration():
    from discord_bot.bot import _build_earnings_date_tool
    tool = _build_earnings_date_tool()
    fd = tool.function_declarations[0]
    assert fd.name == "lookup_earnings_date"
    assert fd.description and len(fd.description) > 200
    required = fd.parameters.required or []
    assert "symbol" in required, (
        "symbol must be REQUIRED — a no-arg call has nothing to look up"
    )
    _ok("_build_earnings_date_tool: valid declaration, symbol required")


def test_executor_ok_path():
    from discord_bot import bot as bot_mod
    fake = {
        "symbol": "GEO",
        "next": {"date": "2026-07-29", "timing": "before market open",
                 "eps_estimate": 0.28, "revenue_estimate": 610000000},
        "last": {"date": "2026-05-07", "eps_actual": 0.25,
                 "eps_estimate": 0.22},
    }
    with patch("report.news_data.fetch_earnings_date_for_symbol",
               return_value=fake):
        result = asyncio.run(bot_mod._execute_earnings_date(
            {"symbol": "geo"}
        ))
    assert result["status"] == "ok"
    assert result["symbol"] == "GEO"
    assert result["next"]["date"] == "2026-07-29"
    assert result["last"]["eps_actual"] == 0.25
    assert "as_of" in result
    _ok("executor ok path: ok + next/last + as_of; symbol uppercased")


def test_executor_no_data_instructs_google_fallback():
    """Empty calendar → no_data, and the payload must explicitly route
    the model to Google Search + the ACTUAL date question. This payload
    text is the structural fix for the 19:10 GEO dodge."""
    from discord_bot import bot as bot_mod
    with patch("report.news_data.fetch_earnings_date_for_symbol",
               return_value={"symbol": "ZZZQ", "next": None, "last": None}):
        result = asyncio.run(bot_mod._execute_earnings_date(
            {"symbol": "ZZZQ"}
        ))
    assert result["status"] == "no_data"
    assert "GOOGLE SEARCH" in result["error"]
    assert "dodge" in result["error"].lower() or \
        "adjacent" in result["error"].lower()
    _ok("executor no_data: instructs Google fallback + no-dodge")


def test_executor_error_path_instructs_google_fallback():
    from discord_bot import bot as bot_mod
    with patch("report.news_data.fetch_earnings_date_for_symbol",
               side_effect=RuntimeError("Finnhub 429")):
        result = asyncio.run(bot_mod._execute_earnings_date(
            {"symbol": "GEO"}
        ))
    assert result["status"] == "error"
    assert "GOOGLE SEARCH" in result["error"]
    # Fetcher returning None (fetch failure) takes the same path
    with patch("report.news_data.fetch_earnings_date_for_symbol",
               return_value=None):
        result2 = asyncio.run(bot_mod._execute_earnings_date(
            {"symbol": "GEO"}
        ))
    assert result2["status"] == "error"
    _ok("executor error path: raise AND None both → error + Google "
        "fallback instruction")


def test_executor_empty_symbol():
    from discord_bot import bot as bot_mod
    result = asyncio.run(bot_mod._execute_earnings_date({}))
    assert result["status"] == "error"
    assert "symbol" in result["error"].lower()
    _ok("executor: missing symbol → error asking for a ticker")


def test_executor_uses_to_thread():
    """The fetcher does sync urllib I/O — it must run via
    asyncio.to_thread or it freezes the whole bot event loop."""
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._execute_earnings_date)
    assert "asyncio.to_thread" in src
    _ok("executor wraps fetcher in asyncio.to_thread")


# === Wiring ===

def test_tool_registered_in_both_arrays():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    n = src.count("_build_earnings_date_tool()")
    assert n >= 2, (
        f"_build_earnings_date_tool() must be in both tools=[] arrays "
        f"(initial + repetition retry), got {n}"
    )
    _ok(f"_build_earnings_date_tool() registered {n}x in "
        f"_answer_with_gemini")


def test_dispatch_map_entry_present():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert '"lookup_earnings_date": _execute_earnings_date' in src
    _ok("executor map routes lookup_earnings_date")


# === Prompt: §8 + Google-default + NO-TA ===

def test_prompt_eight_tools_and_section_8():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "You have EIGHT tools" in P
    assert "You have SEVEN tools" not in P
    assert "### 8. `lookup_earnings_date" in P
    assert "lookup_earnings_date" in P
    # §8 must carry the fallback semantics
    sec = P.find("### 8. `lookup_earnings_date")
    window = P[sec:sec + 4000]
    assert "no_data" in window
    assert "Google Search" in window
    assert "confirmed" in window  # confirmed-vs-estimated flagging
    _ok("prompt: EIGHT tools + §8 with Google-fallback semantics")


def test_prompt_google_default_rule():
    """The binding Google-default rule must exist in the TOOLS preamble
    and anchor the observed GEO dodge."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "GOOGLE IS THE DEFAULT" in P
    anchor = P.find("GOOGLE IS THE DEFAULT")
    window = P[anchor:anchor + 1600]
    assert "GEO" in window, "rule must anchor the observed 06-10 dodge"
    assert "dodge" in window.lower()
    assert "actual question" in window.lower() or \
        "ACTUAL question" in window
    _ok("prompt: binding GOOGLE IS THE DEFAULT rule with GEO anchor")


def test_prompt_routing_list_updated():
    """The Type-1 routing list must route earnings dates to the new
    tool and macro print NUMBERS to lookup_economic_calendar (the old
    list still said macro prints → Google, contradicting §7)."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    anchor = P.find("Tool routing — pick the FIRST one that applies")
    assert anchor != -1
    window = P[anchor:anchor + 3000]
    assert "lookup_earnings_date" in window
    assert "lookup_economic_calendar" in window
    # The stale entry routed prints to Google
    assert "data prints (CPI, NFP, retail sales, ISM, PCE), Fed " \
        "statements" not in window
    _ok("prompt: routing list updated (earnings date tool + macro "
        "numbers → calendar tool)")


def test_prompt_no_ta_rule_present():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "NO SELF-GENERATED TECHNICAL ANALYSIS" in P
    anchor = P.find("NO SELF-GENERATED TECHNICAL ANALYSIS")
    window = P[anchor:anchor + 3000]
    # Anchors the three observed fabrications
    assert "7293" in window
    assert "consolidation zone" in window
    assert "$115" in window
    # 2026-06-17 additions: RSI / overbought / pivot now explicitly banned
    # + anchored (the words the new violations actually used)
    assert "RSI" in window
    assert "overbought" in window
    assert "pivot" in window
    assert "30,000 level" in window, "must anchor the 06-17 NDX pivot violation"
    # The permitted path: attributed levels
    assert "attributed" in window
    # The pure-chart-question answer
    assert "chart view" in window
    _ok("prompt: NO-TA rule bans RSI/overbought/pivot + anchors 06-10 "
        "and 06-17 violations")


def test_prompt_corporate_event_search_and_anticonfab():
    """2026-06-17 SPCX fix: corporate-event schedules (unlock/lockup/
    float/ETF tickers) must route to Google, and the bot must say
    'couldn't verify' rather than confabulate specifics when search is
    thin."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    # search-required trigger for corporate-event schedules
    anchor = P.find("Corporate-event schedules for a named security")
    assert anchor != -1, "missing corporate-event search trigger"
    window = P[anchor:anchor + 700]
    assert "lockup" in window.lower() and "unlock" in window.lower()
    assert "SPCX" in window, "must anchor the SPCX confabulation"
    # anti-confabulation rule
    assert "DO NOT CONFABULATE SPECIFICS WHEN SEARCH COMES UP THIN" in P
    ac = P.find("DO NOT CONFABULATE SPECIFICS WHEN SEARCH COMES UP THIN")
    acw = P[ac:ac + 900]
    assert "couldn't" in acw.lower() or "can't find" in acw.lower()
    assert "never invent" in acw.lower() or "NEVER invent" in acw
    _ok("prompt: corporate-event search trigger + anti-confabulation "
        "(SPCX-anchored)")


def test_prompt_ta_teaching_examples_scrubbed():
    """The worked examples + chart-question carve-out used to TEACH
    self-generated TA. They must be gone — a binding rule loses to a
    few-shot example that demonstrates the opposite."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "holding above **$870** breakout" not in P
    assert "Bias long while **$870** holds" not in P
    assert "broke **$23** flag" not in P
    assert "long above **$23.40**" not in P
    assert "Stop **$22.50**" not in P
    assert "TA-led from the start" not in P
    _ok("prompt: TA-teaching worked examples + 'TA-led' carve-out "
        "scrubbed")


if __name__ == "__main__":
    print("=== lookup_earnings_date + Google-default + NO-TA smoke ===")
    test_fetcher_signature_and_guards()
    test_fetcher_splits_next_and_last()
    test_fetcher_timing_tbd_and_no_upcoming()
    test_fetcher_none_on_fetch_failure()
    test_tool_builder_returns_valid_declaration()
    test_executor_ok_path()
    test_executor_no_data_instructs_google_fallback()
    test_executor_error_path_instructs_google_fallback()
    test_executor_empty_symbol()
    test_executor_uses_to_thread()
    test_tool_registered_in_both_arrays()
    test_dispatch_map_entry_present()
    test_prompt_eight_tools_and_section_8()
    test_prompt_google_default_rule()
    test_prompt_routing_list_updated()
    test_prompt_no_ta_rule_present()
    test_prompt_corporate_event_search_and_anticonfab()
    test_prompt_ta_teaching_examples_scrubbed()
    print("\nALL EARNINGS-DATE / GOOGLE-DEFAULT / NO-TA SMOKE TESTS PASS")
