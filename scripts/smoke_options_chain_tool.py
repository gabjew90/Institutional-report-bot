"""Smoke test: lookup_options_chain tool — schema, executor, prompt
wiring, and aggregation math.

Background: 2026-06-06 21:21-21:22 UTC, Moonsoon asked SPY OI/vol and
Oracle followed with NDX. Bot answered with specific decimals
(SPY OI 248,553 / IV 10.3% / P/C 1.28 ; NDX OI 3,657 / IV 26.05%)
that had no live data source — same family as the NDX $30,500 price
fabrication. Bot had `lookup_market_price` (price + change_pct only)
but no analogous tool for options-chain data. Sourced from Google
Search snippets, which are stale and frequently wrong-symbol.

Fix: ship `lookup_options_chain(symbol, expiration?)` that hits Yahoo's
v7 options endpoint, aggregates total volume + OI + ATM IV + put-call
ratios per expiration, and returns the list of available expirations
so the model can iterate. Update the system prompt to route options
questions there + shrink the unforced-market-data-assertion carve-out
to acknowledge the tool now exists.

This smoke covers:
  - market_data.summarize_options_chain math is correct
  - market_data._fetch_yahoo_options_chain accepts symbol + optional
    expiration_unix
  - bot._build_options_chain_tool returns a valid FunctionDeclaration
  - bot._execute_options_chain handles ok / no_chain / error / bad-date
  - tool is registered in BOTH tools=[] arrays in _answer_with_gemini
  - dispatch branch routes lookup_options_chain → _execute_options_chain
  - system prompt has a Tool #6 section describing the tool
  - system prompt's tool-count header says SIX, not FIVE
  - unforced-market-data assertion rule references the tool's existence
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


# === market_data layer ===

def test_summarize_aggregates_volume_and_oi():
    from report import market_data
    raw = {
        "underlying_symbol": "SPY",
        "underlying_spot_price": 750.0,
        "chain": {
            "expiration_unix": 1718150400,
            "calls": [
                {"strike": 745, "openInterest": 10000, "volume": 5000,
                 "impliedVolatility": 0.22},
                {"strike": 750, "openInterest": 20000, "volume": 8000,
                 "impliedVolatility": 0.24},
                {"strike": 755, "openInterest": 15000, "volume": 7000,
                 "impliedVolatility": 0.26},
            ],
            "puts": [
                {"strike": 745, "openInterest": 12000, "volume": 6000,
                 "impliedVolatility": 0.23},
                {"strike": 750, "openInterest": 25000, "volume": 10000,
                 "impliedVolatility": 0.25},
                {"strike": 755, "openInterest": 18000, "volume": 9000,
                 "impliedVolatility": 0.27},
            ],
        },
        "source": "yahoo",
    }
    s = market_data.summarize_options_chain(raw)
    assert s["call_volume"] == 20000, s
    assert s["put_volume"] == 25000, s
    assert s["call_oi"] == 45000, s
    assert s["put_oi"] == 55000, s
    assert s["put_call_volume_ratio"] == 1.25, s
    # OI ratio = 55000 / 45000 = 1.222...
    assert abs(s["put_call_oi_ratio"] - 1.222) < 0.01, s
    # ATM IV = mean of call.iv + put.iv at strike 750 = (0.24 + 0.25)/2 = 0.245
    assert s["atm_iv"] == 0.245, s
    assert s["num_call_strikes"] == 3 and s["num_put_strikes"] == 3
    _ok("summarize_options_chain: volume + OI sums + P/C ratios + ATM IV "
        "correct")


def test_summarize_handles_empty_chain():
    """Empty calls/puts must not divide by zero or break ATM IV."""
    from report import market_data
    raw = {
        "underlying_symbol": "FOO",
        "underlying_spot_price": 100.0,
        "chain": {"expiration_unix": 0, "calls": [], "puts": []},
        "source": "yahoo",
    }
    s = market_data.summarize_options_chain(raw)
    assert s["call_volume"] == 0 and s["put_volume"] == 0
    assert s["put_call_volume_ratio"] is None, (
        "P/C ratio must be None when call_vol is 0 — not 0/0 nor a "
        "ZeroDivisionError"
    )
    assert s["atm_iv"] is None
    _ok("summarize_options_chain: empty chain returns Nones safely")


def test_fetcher_signature_accepts_optional_expiration():
    from report import market_data
    sig = inspect.signature(market_data._fetch_yahoo_options_chain)
    assert "symbol" in sig.parameters
    assert "expiration_unix" in sig.parameters
    p = sig.parameters["expiration_unix"]
    assert p.default is None, (
        "expiration_unix should default to None so the first call gets "
        "the nearest-expiration chain + available expirations list"
    )
    _ok("_fetch_yahoo_options_chain accepts optional expiration_unix")


# === bot layer ===

def test_tool_builder_returns_function_declaration():
    """The FunctionDeclaration must be valid (name, parameters, required
    args). Without these the genai tool config raises at model-call time,
    not at registration."""
    from discord_bot.bot import _build_options_chain_tool
    tool = _build_options_chain_tool()
    assert tool.function_declarations, "tool must declare functions"
    fd = tool.function_declarations[0]
    assert fd.name == "lookup_options_chain", fd.name
    # Must have a description (otherwise the model has no routing signal)
    assert fd.description and len(fd.description) > 100, (
        "FunctionDeclaration.description must be substantive — the model "
        "uses it to route; an empty/thin description routes badly"
    )
    # symbol must be required
    assert "symbol" in (fd.parameters.required or []), (
        "'symbol' must be a required parameter — calling without it "
        "should fail at the schema level, not at the executor"
    )
    _ok("_build_options_chain_tool returns valid FunctionDeclaration "
        "with symbol required")


def test_executor_handles_no_chain():
    """When Yahoo returns no expirations (symbol has no listed options),
    executor returns status='no_chain' so the model tells the asker
    instead of fabricating."""
    from discord_bot import bot as bot_mod
    raw_response = {
        "underlying_symbol": "ZZZZ",
        "underlying_spot_price": 1.0,
        "expiration_dates": [],
        "chain": {"expiration_unix": 0, "calls": [], "puts": []},
        "source": "yahoo",
    }
    with patch("report.market_data._fetch_yahoo_options_chain",
               return_value=raw_response):
        result = asyncio.run(bot_mod._execute_options_chain(
            {"symbol": "ZZZZ"}
        ))
    assert result["status"] == "no_chain", result
    assert "ZZZZ" in result.get("error", "")
    _ok("executor: status='no_chain' when Yahoo returns no expirations")


def test_executor_handles_fetch_failure():
    """When _fetch_yahoo_options_chain returns None (rate-limit /
    upstream issue), executor returns status='error' so the model
    fall-backs to 'check your broker' — not invents numbers."""
    from discord_bot import bot as bot_mod
    with patch("report.market_data._fetch_yahoo_options_chain",
               return_value=None):
        result = asyncio.run(bot_mod._execute_options_chain(
            {"symbol": "SPY"}
        ))
    assert result["status"] == "error", result
    assert "Yahoo" in result["error"] or "fetch" in result["error"].lower()
    _ok("executor: status='error' when Yahoo fetch returns None")


def test_executor_rejects_bad_date():
    """Malformed expiration ISO must return an error WITHOUT calling
    Yahoo a second time — the available_expirations list comes from
    the first fetch so the model can re-call with a valid date."""
    from discord_bot import bot as bot_mod
    raw_response = {
        "underlying_symbol": "SPY",
        "underlying_spot_price": 750.0,
        "expiration_dates": [1718150400, 1718755200, 1719360000],
        "chain": {"expiration_unix": 1718150400, "calls": [
            {"strike": 750, "openInterest": 1000, "volume": 500,
             "impliedVolatility": 0.24}
        ], "puts": []},
        "source": "yahoo",
    }
    with patch("report.market_data._fetch_yahoo_options_chain",
               return_value=raw_response) as mock_fetch:
        result = asyncio.run(bot_mod._execute_options_chain(
            {"symbol": "SPY", "expiration": "not-a-date"}
        ))
    assert result["status"] == "error", result
    assert "ISO" in result["error"] or "YYYY-MM-DD" in result["error"]
    # available_expirations should be populated so the model can retry
    assert result.get("available_expirations"), (
        "bad-date errors must include available_expirations so the "
        "model can re-call with a valid date in the same turn"
    )
    # And Yahoo was only called ONCE (the initial list-of-expirations
    # fetch); the bad date never triggered a second fetch
    assert mock_fetch.call_count == 1
    _ok("executor: bad date → error + available_expirations + only "
        "one Yahoo call")


def test_executor_ok_path():
    """Happy path: symbol only, no expiration. Returns status='ok' with
    summary + available_expirations + as_of."""
    from discord_bot import bot as bot_mod
    raw_response = {
        "underlying_symbol": "SPY",
        "underlying_spot_price": 750.0,
        "expiration_dates": [1718150400, 1718755200, 1719360000],
        "chain": {
            "expiration_unix": 1718150400,
            "calls": [{"strike": 750, "openInterest": 20000,
                       "volume": 8000, "impliedVolatility": 0.24}],
            "puts": [{"strike": 750, "openInterest": 25000,
                      "volume": 10000, "impliedVolatility": 0.25}],
        },
        "source": "yahoo",
    }
    with patch("report.market_data._fetch_yahoo_options_chain",
               return_value=raw_response):
        result = asyncio.run(bot_mod._execute_options_chain(
            {"symbol": "SPY"}
        ))
    assert result["status"] == "ok", result
    summary = result["summary"]
    assert summary["call_volume"] == 8000
    assert summary["put_volume"] == 10000
    assert summary["atm_iv"] == 0.245
    assert summary["expiration_iso"] is not None
    assert len(result["available_expirations"]) == 3
    assert "as_of" in result
    _ok("executor: ok path returns status='ok' + summary + "
        "available_expirations + as_of")


# === wiring + prompt ===

def test_tool_registered_in_both_tool_arrays():
    """The tool must be added to BOTH tools=[] declarations: the initial
    call AND the retry call. Missing the retry adds a regression where
    the model loses access to options-chain on the Voice-strip retry."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    n_registrations = src.count("_build_options_chain_tool()")
    assert n_registrations >= 2, (
        f"lookup_options_chain must be registered in >=2 tools=[] "
        f"arrays (initial + retry); got {n_registrations}"
    )
    _ok(f"_build_options_chain_tool registered {n_registrations}x in "
        f"_answer_with_gemini (initial + retry)")


def test_dispatch_branch_present():
    """The tool-call dispatch loop must have an elif branch for
    'lookup_options_chain' — otherwise calls go to the 'unknown tool'
    fallback and the bot tells the model 'unknown tool' even though
    the FunctionDeclaration registered fine."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert 'fc.name == "lookup_options_chain"' in src, (
        "dispatch loop must have an elif branch matching the tool "
        "name; otherwise model calls route to 'unknown tool'"
    )
    assert "_execute_options_chain(args)" in src, (
        "dispatch branch must call _execute_options_chain(args)"
    )
    _ok("dispatch branch: fc.name=='lookup_options_chain' routes to "
        "_execute_options_chain(args)")


def test_system_prompt_tool_count_updated():
    """The system prompt's TOOLS preamble says 'You have SIX tools'
    (was FIVE). Without this update, the model still thinks there are
    five and may not consider the new one."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    assert "You have SIX tools" in _ASK_SYSTEM_INSTRUCTION, (
        "TOOLS preamble must say 'You have SIX tools' (was FIVE) so "
        "the model recognizes lookup_options_chain in its tool count"
    )
    _ok("system prompt's TOOLS preamble updated to 'SIX tools'")


def test_system_prompt_has_tool_6_section():
    """A dedicated section for the new tool must exist with concrete
    when-to-call examples + response-shape guidance. Without this the
    model only sees the FunctionDeclaration description and routes
    less reliably."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    assert "### 6. `lookup_options_chain" in _ASK_SYSTEM_INSTRUCTION, (
        "system prompt must have a '### 6. lookup_options_chain' section "
        "(matches the numbering convention of the other tool sections)"
    )
    # Concrete when-to-call examples
    assert "OI on SPY next week" in _ASK_SYSTEM_INSTRUCTION
    assert "options volume" in _ASK_SYSTEM_INSTRUCTION.lower()
    assert "put-call ratio" in _ASK_SYSTEM_INSTRUCTION.lower()
    # Response shape (status field)
    assert 'status: "ok"' in _ASK_SYSTEM_INSTRUCTION
    assert 'status: "no_chain"' in _ASK_SYSTEM_INSTRUCTION
    assert 'status: "error"' in _ASK_SYSTEM_INSTRUCTION
    _ok("system prompt section 6 has when-to-call examples + status "
        "field guidance")


def test_lookup_market_price_scope_clarification_added():
    """Tool #5 (lookup_market_price) section must explicitly say it
    does NOT cover options, so the model doesn't try to use it for
    OI/IV/volume questions."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    assert ("Scope note: `lookup_market_price` is PRICE ONLY"
            in _ASK_SYSTEM_INSTRUCTION), (
        "lookup_market_price section must include an explicit PRICE-"
        "ONLY scope note so the model doesn't try to use it for "
        "options-chain questions"
    )
    _ok("lookup_market_price section: PRICE-ONLY scope note present")


def test_unforced_market_data_rule_acknowledges_tool():
    """The ZERO UNFORCED MARKET-DATA ASSERTIONS rule must now reference
    lookup_options_chain — otherwise the rule still says 'no tool
    exists' for options-data, which would block legitimate tool use."""
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION
    rule_anchor = _ASK_SYSTEM_INSTRUCTION.find(
        "ZERO UNFORCED MARKET-DATA ASSERTIONS"
    )
    assert rule_anchor != -1, "rule header missing"
    rule_window = _ASK_SYSTEM_INSTRUCTION[rule_anchor:rule_anchor + 2500]
    assert "lookup_options_chain" in rule_window, (
        "the rule must now reference lookup_options_chain (the tool now "
        "exists; the rule was previously a no-tool blocker)"
    )
    _ok("ZERO UNFORCED MARKET-DATA ASSERTIONS rule now references the "
        "lookup_options_chain tool")


if __name__ == "__main__":
    print("=== lookup_options_chain tool smoke ===")
    test_summarize_aggregates_volume_and_oi()
    test_summarize_handles_empty_chain()
    test_fetcher_signature_accepts_optional_expiration()
    test_tool_builder_returns_function_declaration()
    test_executor_handles_no_chain()
    test_executor_handles_fetch_failure()
    test_executor_rejects_bad_date()
    test_executor_ok_path()
    test_tool_registered_in_both_tool_arrays()
    test_dispatch_branch_present()
    test_system_prompt_tool_count_updated()
    test_system_prompt_has_tool_6_section()
    test_lookup_market_price_scope_clarification_added()
    test_unforced_market_data_rule_acknowledges_tool()
    print("\nALL OPTIONS-CHAIN TOOL SMOKE TESTS PASS")
