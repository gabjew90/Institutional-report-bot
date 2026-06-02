"""Smoke test for the lookup_user_profile tool.

Validates:
  1. Tool definition has correct name + parameters in schema
  2. _execute_user_profile error modes:
     - no anchor -> error
     - both username and metric -> error
     - leaderboard mode + include_profile=True -> error
  3. _execute_user_profile success modes:
     - username only -> rank + rationales
     - username + include_profile=True -> above + dossier
     - metric + rank_position -> user at that rank
     - metric only -> top 5
  4. Old lookup_user_ranks is gone from the module
"""

import asyncio
import inspect
import sys
from unittest.mock import patch

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_definition_shape():
    tool = bot_mod._build_user_profile_tool()
    decls = tool.function_declarations
    assert len(decls) == 1, "expected exactly one FunctionDeclaration"
    decl = decls[0]
    assert decl.name == "lookup_user_profile", f"unexpected name: {decl.name}"
    props = decl.parameters.properties
    for arg in ("username", "metric", "rank_position", "include_profile"):
        assert arg in props, f"missing parameter {arg!r} in schema"
    _ok("_build_user_profile_tool: name + all 4 parameters present in schema")


def test_no_anchor_error():
    result = asyncio.run(bot_mod._execute_user_profile({}))
    assert "error" in result, f"expected error key, got {result}"
    _ok("no anchor -> error")


def test_both_anchors_error():
    result = asyncio.run(bot_mod._execute_user_profile({
        "username": "foo", "metric": "trader",
    }))
    assert "error" in result, f"expected error key, got {result}"
    assert "exactly one" in result["error"].lower(), (
        f"error should mention 'exactly one anchor', got: {result['error']!r}"
    )
    _ok("both username and metric set -> error")


def test_leaderboard_plus_include_profile_error():
    result = asyncio.run(bot_mod._execute_user_profile({
        "metric": "trader", "include_profile": True,
    }))
    assert "error" in result, f"expected error key, got {result}"
    assert ("leaderboard" in result["error"].lower()
            or "profile" in result["error"].lower()), (
        f"error should reject include_profile in leaderboard mode, got: {result['error']!r}"
    )
    _ok("leaderboard mode + include_profile=True -> error")


def test_username_mode_returns_rank():
    fake_lookup = {
        "users": [{
            "user_id": 12345, "username": "bankerkyle", "display_name": "BK",
            "trader_rank": 1, "trader_total": 53,
            "racism_rank": 1, "racism_total": 3,
            "trader_rationale": "BK reads as a high-octane gambler",
            "racism_rationale": "BK uses racial slurs",
        }],
        "count": 1,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "bankerkyle"}))
    assert "users" in result and len(result["users"]) == 1, f"expected one user, got {result}"
    user = result["users"][0]
    assert user.get("username") == "bankerkyle", f"unexpected user: {user}"
    assert "trader_rationale" in user, f"missing trader_rationale: {user}"
    assert "profile_text" not in user, (
        "profile_text should NOT be in result when include_profile=False"
    )
    _ok("username mode returns rank + rationales (no profile_text)")


def test_username_mode_with_include_profile():
    fake_lookup = {
        "users": [{
            "user_id": 12345, "username": "bankerkyle", "display_name": "BK",
            "trader_rank": 1, "trader_total": 53,
            "racism_rank": 1, "racism_total": 3,
            "trader_rationale": "rat",
            "racism_rationale": "rat",
        }],
        "count": 1,
    }
    fake_dossier = "**Personality and style.** BK is loud."
    with (
        patch("db.lookup_user_ranks", return_value=fake_lookup),
        patch("db.format_user_profiles_for_context", return_value=fake_dossier),
    ):
        result = asyncio.run(bot_mod._execute_user_profile({
            "username": "bankerkyle", "include_profile": True,
        }))
    user = result["users"][0]
    assert "profile_text" in user, f"profile_text missing: {user}"
    assert "Personality" in user["profile_text"], "dossier not surfaced"
    _ok("username mode + include_profile=True returns dossier")


def test_metric_and_rank_position_mode():
    fake_lookup = {
        "users": [{
            "user_id": 67890, "username": "abullish_xyz", "display_name": "abe",
            "trader_rank": 2, "trader_total": 53,
            "racism_rank": 2, "racism_total": 3,
            "trader_rationale": "abe is the engine",
            "racism_rationale": "abe weaves slurs",
        }],
        "count": 1,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({
            "metric": "trader", "rank_position": 2,
        }))
    assert len(result.get("users", [])) == 1, f"expected one user, got {result}"
    _ok("metric + rank_position returns one user at that rank")


def test_metric_only_leaderboard_mode():
    fake_lookup = {
        "users": [
            {"user_id": i, "username": f"u{i}", "display_name": f"U{i}",
             "trader_rank": i, "trader_total": 53,
             "trader_rationale": f"r{i}", "racism_rationale": f"rr{i}"}
            for i in range(1, 6)
        ],
        "count": 5,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({"metric": "trader"}))
    assert len(result.get("users", [])) == 5, f"expected 5 users, got {result}"
    _ok("metric-only mode returns top 5 leaderboard")


def test_old_lookup_user_ranks_removed():
    """The old tool def and executor should be fully gone from bot.py."""
    assert not hasattr(bot_mod, "_build_user_ranks_tool"), (
        "_build_user_ranks_tool should be removed (replaced by _build_user_profile_tool)"
    )
    assert not hasattr(bot_mod, "_execute_user_ranks"), (
        "_execute_user_ranks should be removed (replaced by _execute_user_profile)"
    )
    _ok("old _build_user_ranks_tool / _execute_user_ranks are gone")


if __name__ == "__main__":
    print("=== lookup_user_profile tool smoke ===")
    test_tool_definition_shape()
    test_no_anchor_error()
    test_both_anchors_error()
    test_leaderboard_plus_include_profile_error()
    test_username_mode_returns_rank()
    test_username_mode_with_include_profile()
    test_metric_and_rank_position_mode()
    test_metric_only_leaderboard_mode()
    test_old_lookup_user_ranks_removed()
    print("\nALL USER-PROFILE-TOOL SMOKE TESTS PASS")
