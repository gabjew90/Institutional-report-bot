"""Smoke test: lookup_economic_calendar tool — schema, executor, prompt
wiring, structured-fetch correctness.

Background: 2026-06-05 + 2026-06-08 QC found the bot recycling macro
print numbers (NFP 172k vs 85k expected) that came from Google Search
grounding three days earlier and conflicted with the same day's
pulse (which used Finnhub's ADP Private NFP 120k vs 85k via the
canonical economic_calendar source). Plus 2026-06-08 invented
CPI consensus '+4.2% YoY' for the June 10 print with no Finnhub or
Google attribution.

Root cause: pulse uses fetch_economic_calendar (Finnhub-grounded),
/ask uses Google Search grounding. Two sources, same release, two
numbers — daily reader gets whiplash and can't tell which to trust.

Fix: expose the same Finnhub source to /ask via a structured tool.
Adds:
  - report.news_data.fetch_economic_calendar_structured — returns
    list[dict] of Tier-1 events with consensus / prev / actual /
    status fields
  - bot._build_economic_calendar_tool — FunctionDeclaration with
    optional query + days_window args
  - bot._execute_economic_calendar — handles ok / no_match / error
    paths
  - Tool registered in BOTH tools=[] arrays (initial + Voice-strip
    retry) and dispatched in the tool-call loop
  - System prompt §7 with concrete when-to-call examples + status
    field semantics; tool count updated SIX → SEVEN
  - ZERO UNFORCED MARKET-DATA ASSERTIONS rule extended to reference
    the macro tool

This smoke covers:
  - Structured fetcher: query filter (strict-AND then relaxed-OR),
    status classification (released / scheduled / past_no_data),
    sorted output, empty-on-failure
  - Tool builder returns valid FunctionDeclaration (no required args
    since both are optional)
  - Executor handles: ok / no_match / error / numeric days_window
    coercion / clamping to 1-30 range
  - Tool registered in BOTH tools=[] arrays
  - Dispatch branch matches and routes
  - System prompt: tool count SEVEN, §7 section, routing rule,
    assertion extension references macro tool
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


# === Structured fetcher ===

def test_structured_fetch_signature():
    from report import news_data
    sig = inspect.signature(news_data.fetch_economic_calendar_structured)
    assert "query" in sig.parameters
    assert "days_window" in sig.parameters
    assert sig.parameters["query"].default is None
    assert sig.parameters["days_window"].default == 14
    _ok("fetch_economic_calendar_structured accepts query + days_window "
        "with sensible defaults")


def test_structured_fetch_classifies_status():
    """Released events (actual present) get status='released'; future
    events get 'scheduled'; past events with no actual get
    'past_no_data'. Without correct status classification the model
    can't disambiguate 'last print' vs 'next print' from a single row."""
    from report import news_data
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    fake_finnhub = {
        "economicCalendar": [
            {
                "event": "CPI YoY",
                "country": "US",
                "time": (now + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                "impact": "high",
                "estimate": 4.0,
                "prev": 3.8,
                "actual": None,
                "unit": "%",
            },
            {
                "event": "Nonfarm Payrolls",
                "country": "US",
                "time": (now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"),
                "impact": "high",
                "estimate": 85,
                "prev": 130,
                "actual": 120,
                "unit": "K",
            },
            {
                "event": "PPI MoM",
                "country": "US",
                "time": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"),
                "impact": "medium",
                "estimate": 0.3,
                "prev": 0.4,
                "actual": None,  # past scheduled time, no actual yet
                "unit": "%",
            },
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake_finnhub), \
         patch("config.settings.finnhub_api_key", "test_key"):
        rows = news_data.fetch_economic_calendar_structured(days_window=14)

    by_event = {r["event"]: r for r in rows}
    assert "CPI YoY" in by_event
    assert by_event["CPI YoY"]["status"] == "scheduled", by_event["CPI YoY"]
    assert by_event["Nonfarm Payrolls"]["status"] == "released"
    assert by_event["Nonfarm Payrolls"]["actual"] == 120
    assert by_event["PPI MoM"]["status"] == "past_no_data", by_event["PPI MoM"]
    _ok("structured fetch classifies status (released / scheduled / "
        "past_no_data) correctly")


def test_structured_fetch_query_filter_strict_then_relaxed():
    """Multi-word query first tries strict-AND match; if zero results,
    falls back to relaxed-OR. Prevents 'May payrolls' from returning
    empty just because Finnhub names the event 'Nonfarm Payrolls'."""
    from report import news_data

    fake_finnhub = {
        "economicCalendar": [
            {"event": "Nonfarm Payrolls", "country": "US",
             "time": "2026-06-06 12:30:00", "impact": "high",
             "estimate": 85, "prev": 130, "actual": 120, "unit": "K"},
            {"event": "CPI YoY", "country": "US",
             "time": "2026-06-10 12:30:00", "impact": "high",
             "estimate": 4.0, "prev": 3.8, "actual": None, "unit": "%"},
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake_finnhub), \
         patch("config.settings.finnhub_api_key", "test_key"):
        # "May payrolls" — neither token strict-matches "Nonfarm
        # Payrolls" (only "payrolls" does); relaxed-OR should still
        # return the NFP row.
        rows = news_data.fetch_economic_calendar_structured(query="May payrolls")

    events = [r["event"] for r in rows]
    assert "Nonfarm Payrolls" in events, (
        f"relaxed-OR fallback must catch payrolls query, got {events}"
    )
    assert "CPI YoY" not in events, (
        f"CPI shouldn't match a 'May payrolls' query, got {events}"
    )
    _ok("structured fetch: query 'May payrolls' falls back to relaxed-OR "
        "and matches 'Nonfarm Payrolls'")


def test_structured_fetch_raises_when_all_sources_down():
    """2026-06-11 behavior change: no key (or Finnhub 403) now falls
    back to ForexFactory; when THAT also fails, the fetcher raises
    EconomicCalendarUnavailable instead of returning [] — so /ask says
    'feed down' rather than 'no such event'."""
    from report import news_data
    with patch("config.settings.finnhub_api_key", ""), \
         patch("report.news_data._fetch_json", return_value=None):
        try:
            news_data.fetch_economic_calendar_structured()
            _fail("expected EconomicCalendarUnavailable when both "
                  "sources fail")
        except news_data.EconomicCalendarUnavailable:
            pass
    _ok("structured fetch raises EconomicCalendarUnavailable on dual "
        "source failure (no silent [])")


# === Bot tool builder + executor ===

def test_tool_builder_returns_valid_declaration():
    from discord_bot.bot import _build_economic_calendar_tool
    tool = _build_economic_calendar_tool()
    fd = tool.function_declarations[0]
    assert fd.name == "lookup_economic_calendar"
    assert fd.description and len(fd.description) > 200
    # Both params should be optional (no required field is fine)
    # symbol-style required field would force the model to always pass
    # a query, which we don't want — query is optional.
    required = fd.parameters.required or []
    assert "query" not in required, (
        "query must be OPTIONAL — model should be able to call "
        "lookup_economic_calendar() with no args to get all Tier-1 "
        "events in default window"
    )
    _ok("_build_economic_calendar_tool: valid declaration with optional "
        "query")


def test_executor_ok_path():
    from discord_bot import bot as bot_mod
    fake_events = [
        {
            "event": "CPI YoY", "country": "US",
            "scheduled_iso_utc": "2026-06-10T12:30:00",
            "scheduled_et_human": "06-10 08:30 ET",
            "impact": "high", "consensus": 4.0, "prev": 3.8,
            "actual": None, "unit": "%", "status": "scheduled",
        }
    ]
    with patch("report.news_data.fetch_economic_calendar_structured",
               return_value=fake_events):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "CPI"}
        ))
    assert result["status"] == "ok"
    assert result["query"] == "CPI"
    assert len(result["events"]) == 1
    assert result["events"][0]["event"] == "CPI YoY"
    assert "as_of" in result
    _ok("executor ok path: returns events + query + as_of")


def test_executor_no_match():
    from discord_bot import bot as bot_mod
    with patch("report.news_data.fetch_economic_calendar_structured",
               return_value=[]):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "Empire State Manufacturing"}
        ))
    assert result["status"] == "no_match"
    assert "Tier-1" in result["error"]
    _ok("executor no_match: empty fetcher result → status='no_match' + "
        "explanatory error")


def test_executor_error_path():
    """When the fetcher raises, executor returns status='error' so the
    model falls back to 'no live data' rather than fabricating."""
    from discord_bot import bot as bot_mod
    with patch("report.news_data.fetch_economic_calendar_structured",
               side_effect=RuntimeError("Finnhub 429")):
        result = asyncio.run(bot_mod._execute_economic_calendar(
            {"query": "CPI"}
        ))
    assert result["status"] == "error"
    assert "Finnhub" in result["error"] or "fetch" in result["error"].lower()
    _ok("executor error path: fetcher exception → status='error' + "
        "fallback message")


def test_executor_clamps_days_window():
    """days_window must be coerced to int and clamped to 1-30 range so
    the model can't accidentally pass 500 or -1."""
    from discord_bot import bot as bot_mod
    captured = {}

    def fake_fetch(query=None, days_window=14):
        captured["days_window"] = days_window
        return []

    with patch("report.news_data.fetch_economic_calendar_structured",
               side_effect=fake_fetch):
        asyncio.run(bot_mod._execute_economic_calendar(
            {"days_window": 500}
        ))
        assert captured["days_window"] == 30
        asyncio.run(bot_mod._execute_economic_calendar(
            {"days_window": -5}
        ))
        assert captured["days_window"] == 1
        # String coercion
        asyncio.run(bot_mod._execute_economic_calendar(
            {"days_window": "7"}
        ))
        assert captured["days_window"] == 7
    _ok("executor clamps days_window to [1, 30] + coerces string→int")


# === Wiring + prompt ===

def test_tool_registered_in_both_arrays():
    """Initial + Voice-strip retry both need the tool, otherwise filter-
    trip retries lose access to macro lookups."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    n = src.count("_build_economic_calendar_tool()")
    assert n >= 2, (
        f"_build_economic_calendar_tool() must be registered in both "
        f"tools=[] arrays, got {n}"
    )
    _ok(f"_build_economic_calendar_tool() registered {n}x in "
        f"_answer_with_gemini (initial + retry)")


def test_dispatch_branch_present():
    # Dispatch refactored 2026-06-10 to a guarded `_tool_executors` map.
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert '"lookup_economic_calendar": _execute_economic_calendar' in src
    _ok("dispatch executor map routes lookup_economic_calendar to "
        "_execute_economic_calendar")


def test_system_prompt_tool_count_updated():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    # Count bumped again 2026-06-10 when lookup_earnings_date shipped
    # (SEVEN → EIGHT). This test guards that the preamble count moved
    # FORWARD, not that it froze at seven.
    assert "You have EIGHT tools" in _ASK_SYSTEM_INSTRUCTION, (
        "TOOLS preamble must say 'EIGHT tools' (SIX → SEVEN when the "
        "macro tool shipped, SEVEN → EIGHT when lookup_earnings_date "
        "shipped)"
    )
    assert "You have SIX tools" not in _ASK_SYSTEM_INSTRUCTION
    assert "You have SEVEN tools" not in _ASK_SYSTEM_INSTRUCTION
    _ok("system prompt tool count current (EIGHT)")


def test_system_prompt_section_7_present():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    assert "### 7. `lookup_economic_calendar" in _ASK_SYSTEM_INSTRUCTION
    # Concrete examples
    assert "May CPI" in _ASK_SYSTEM_INSTRUCTION
    assert "ECB" in _ASK_SYSTEM_INSTRUCTION
    assert "payrolls" in _ASK_SYSTEM_INSTRUCTION.lower()
    # Status field semantics
    assert '"released"' in _ASK_SYSTEM_INSTRUCTION
    assert '"scheduled"' in _ASK_SYSTEM_INSTRUCTION
    assert '"past_no_data"' in _ASK_SYSTEM_INSTRUCTION
    # Hard routing rule for macro questions
    assert "HARD ROUTING RULE for macro print questions" in _ASK_SYSTEM_INSTRUCTION
    _ok("system prompt §7 has when-to-call + status semantics + macro "
        "routing rule")


def test_system_prompt_anchors_2026_06_05_06_08_failures():
    """The §7 section + assertion rule extension must reference the
    concrete observed failures (06-05 NFP cross-source conflict + 06-08
    recycled 172k three days later) so the model recognizes the
    pattern."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    assert "2026-06-05" in _ASK_SYSTEM_INSTRUCTION
    assert "2026-06-08" in _ASK_SYSTEM_INSTRUCTION
    assert "172k" in _ASK_SYSTEM_INSTRUCTION
    assert "120k" in _ASK_SYSTEM_INSTRUCTION
    _ok("system prompt anchors the 06-05 NFP cross-source conflict + "
        "06-08 recycling failure")


def test_assertion_rule_references_macro_tool():
    """The existing ZERO UNFORCED MARKET-DATA ASSERTIONS rule must now
    reference lookup_economic_calendar — otherwise it still reads as
    'no tool for macro' which would conflict with the new routing."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    rule_anchor = _ASK_SYSTEM_INSTRUCTION.find(
        "ZERO UNFORCED MARKET-DATA ASSERTIONS"
    )
    rule_window = _ASK_SYSTEM_INSTRUCTION[rule_anchor:rule_anchor + 3500]
    assert "lookup_economic_calendar" in rule_window, (
        "the existing market-data assertion rule must now reference "
        "lookup_economic_calendar so the routing is consistent with "
        "the new tool's existence"
    )
    # And reference macro print categories
    assert "macro print" in rule_window or "macro" in rule_window
    _ok("ZERO UNFORCED MARKET-DATA ASSERTIONS rule now references the "
        "macro tool")


if __name__ == "__main__":
    print("=== lookup_economic_calendar tool smoke ===")
    test_structured_fetch_signature()
    test_structured_fetch_classifies_status()
    test_structured_fetch_query_filter_strict_then_relaxed()
    test_structured_fetch_raises_when_all_sources_down()
    test_tool_builder_returns_valid_declaration()
    test_executor_ok_path()
    test_executor_no_match()
    test_executor_error_path()
    test_executor_clamps_days_window()
    test_tool_registered_in_both_arrays()
    test_dispatch_branch_present()
    test_system_prompt_tool_count_updated()
    test_system_prompt_section_7_present()
    test_system_prompt_anchors_2026_06_05_06_08_failures()
    test_assertion_rule_references_macro_tool()
    print("\nALL ECONOMIC-CALENDAR TOOL SMOKE TESTS PASS")
