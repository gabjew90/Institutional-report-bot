"""Static smoke test that all 4 new/changed tools are wired into the
tools list of both the main config and the repetition-retry config,
and that lookup_user_ranks is fully gone.

Validates:
  1. _answer_with_gemini source contains all 4 tool-builder calls in
     the main config tools list
  2. lookup_user_ranks references are fully gone from bot.py (except
     db.lookup_user_ranks call inside _execute_user_profile)
  3. The dispatch loop has elif branches for all 3 new tool names
"""

import inspect
import sys
import re

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


SRC = inspect.getsource(bot_mod)


def test_main_config_has_all_tools():
    """The main GenerateContentConfig tools list contains all 4 tools."""
    src = inspect.getsource(bot_mod._answer_with_gemini)
    for builder in (
        "_build_chat_search_tool",
        "_build_user_profile_tool",
        "_build_trade_log_tool",
        "_build_market_price_tool",
    ):
        assert builder in src, f"main config tools list missing {builder}()"
    _ok("main config tools list contains all 4 builders")


def test_no_lookup_user_ranks_tool_surface():
    """The lookup_user_ranks TOOL SURFACE should be fully removed.
    db.lookup_user_ranks (the underlying DB helper) stays - the new
    _execute_user_profile delegates to it."""
    assert "_build_user_ranks_tool" not in SRC, (
        "_build_user_ranks_tool still defined / called"
    )
    assert "_execute_user_ranks" not in SRC, (
        "_execute_user_ranks still defined / called"
    )
    # The dispatch loop's quoted tool name should not include the old.
    assert "'lookup_user_ranks'" not in SRC and '"lookup_user_ranks"' not in SRC, (
        "lookup_user_ranks tool name still referenced in dispatch loop"
    )
    _ok("lookup_user_ranks tool surface fully removed")


def test_dispatch_loop_has_all_new_tools():
    """The tool-call dispatch must route the new tools. Dispatch was
    refactored 2026-06-10 from an if/elif chain to a guarded
    `_tool_executors` map — assert each tool has a map entry (the
    equivalent wiring; a missing entry routes to 'unknown tool')."""
    for tool_name, executor in (
        ("lookup_user_profile", "_execute_user_profile"),
        ("lookup_trade_log", "_execute_trade_log"),
        ("lookup_market_price", "_execute_market_price"),
    ):
        pattern = rf'"{tool_name}":\s*{executor}'
        assert re.search(pattern, SRC), (
            f"dispatch executor map missing entry for {tool_name!r}"
        )
    _ok("dispatch executor map has entries for all 3 new tools")


if __name__ == "__main__":
    print("=== tools-wired static smoke ===")
    test_main_config_has_all_tools()
    test_no_lookup_user_ranks_tool_surface()
    test_dispatch_loop_has_all_new_tools()
    print("\nALL TOOLS-WIRED SMOKE TESTS PASS")
