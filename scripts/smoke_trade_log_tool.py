"""Smoke test for the lookup_trade_log tool.

Validates:
  1. Tool definition has correct name + parameters
  2. _execute_trade_log error modes:
     - no anchor -> error
     - both caller and username -> error
     - invalid kind -> error
  3. Caller anchor:
     - kind="open" calls format_analyst_trades_for_context with kind="open"
     - empty data -> status="no_logged_trades"
  4. Username anchor:
     - resolves username -> user_id, returns profile snippet
     - unresolved -> error
     - empty profile section -> status="no_logged_trades"
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
    tool = bot_mod._build_trade_log_tool()
    decls = tool.function_declarations
    assert len(decls) == 1
    decl = decls[0]
    assert decl.name == "lookup_trade_log", f"unexpected name: {decl.name}"
    props = decl.parameters.properties
    for arg in ("caller", "username", "kind", "days"):
        assert arg in props, f"missing parameter {arg!r}"
    _ok("_build_trade_log_tool: name + all 4 parameters present")


def test_no_anchor_error():
    result = asyncio.run(bot_mod._execute_trade_log({}))
    assert "error" in result, f"expected error, got {result}"
    _ok("no anchor -> error")


def test_both_anchors_error():
    result = asyncio.run(bot_mod._execute_trade_log({
        "caller": "abe", "username": "bankerkyle",
    }))
    assert "error" in result, f"expected error, got {result}"
    assert "exactly one" in result["error"].lower(), result["error"]
    _ok("both caller and username set -> error")


def test_invalid_kind_error():
    result = asyncio.run(bot_mod._execute_trade_log({
        "caller": "abe", "kind": "bogus",
    }))
    assert "error" in result, f"expected error, got {result}"
    assert "kind" in result["error"].lower(), result["error"]
    _ok("invalid kind -> error")


def test_caller_anchor_kind_passthrough():
    """The executor passes kind directly through to format_analyst_trades_for_context."""
    fake_text = "ABE'S CURRENTLY OPEN POSITIONS:\n- META 640C 06-05 @6.40"
    with (
        patch(
            "db.format_analyst_trades_for_context",
            return_value=fake_text,
        ) as fmt,
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    assert fmt.called, "format_analyst_trades_for_context not called"
    call_kwargs = fmt.call_args.kwargs
    assert call_kwargs.get("caller") == "abe", call_kwargs
    assert call_kwargs.get("kind") == "open", call_kwargs
    assert result.get("data_quality") == "caller", result
    assert result.get("trades_text"), result
    _ok("caller anchor passes kind='open' through; data_quality='caller'")


def test_caller_anchor_empty_returns_status():
    with (
        patch("db.format_analyst_trades_for_context", return_value=""),
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    # Status was renamed "no_logged_trades" -> "empty" so the
    # gap-#5 status taxonomy is uniform across all executors.
    assert result.get("status") == "empty", result
    _ok("caller anchor with empty data -> status=empty")


def test_username_anchor_returns_profile_snippet():
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch(
            "db.get_user_profile_recent_trades_section",
            return_value="- $PLTR / 145C - closed +911%",
        ),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "username": "theorb_18574", "kind": "recent",
        }))
    assert result.get("data_quality") == "member", result
    assert "PLTR" in (result.get("profile_recent_trades") or ""), result
    _ok("username anchor returns profile snippet, data_quality='member'")


def test_username_unresolved_error():
    with patch("db.resolve_username_to_user_id", return_value=None):
        result = asyncio.run(bot_mod._execute_trade_log({"username": "nobody"}))
    assert "error" in result, f"expected error, got {result}"
    assert "not found" in result["error"].lower(), result["error"]
    # Status taxonomy: unknown username = not_found (clean query, no
    # match), not "error" (runtime failure). Lets the model distinguish.
    assert result.get("status") == "not_found", result
    _ok("username not found -> status=not_found + error message")


def test_username_anchor_empty_returns_status():
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch("db.get_user_profile_recent_trades_section", return_value=""),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({"username": "newjoiner"}))
    # Status was renamed "no_logged_trades" -> "empty" so the
    # gap-#5 status taxonomy is uniform across all executors.
    assert result.get("status") == "empty", result
    _ok("username anchor with empty data -> status=empty")


if __name__ == "__main__":
    print("=== lookup_trade_log tool smoke ===")
    test_tool_definition_shape()
    test_no_anchor_error()
    test_both_anchors_error()
    test_invalid_kind_error()
    test_caller_anchor_kind_passthrough()
    test_caller_anchor_empty_returns_status()
    test_username_anchor_returns_profile_snippet()
    test_username_unresolved_error()
    test_username_anchor_empty_returns_status()
    print("\nALL TRADE-LOG-TOOL SMOKE TESTS PASS")
