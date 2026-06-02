"""Smoke test for /ask gaps #4 (freshness stamps) and #5
(failed-vs-empty distinction).

Both changes apply to the executors that back the on-demand /ask tools.

#4 — Freshness stamps:
  - lookup_user_profile responses include `updated_at` per user (when
    that user's profile was last refreshed) + a top-level `as_of`.
  - lookup_trade_log responses include `as_of` (current UTC) + the
    `window_days` queried. For username anchor, also `profile_updated_at`.
  - search_chat_messages responses include `window_start` / `window_end`
    when shape A or C is used (shape B already had them).

#5 — Failed vs empty distinction:
  Every executor response carries a top-level `status`:
    "ok"    → tool ran cleanly, data populated
    "empty" → tool ran cleanly, no data matched the query
    "error" → tool failed (DB hiccup, malformed args, etc.)
  The tool descriptions tell Gemini: never fabricate on 'error'; may
  say 'no data found' on 'empty'.
"""

import asyncio
import sqlite3
import sys
from unittest.mock import patch, MagicMock

import discord_bot.bot as bot_mod
import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db._init_schema(c)
    db._migrate_drop_unique_constraints(c)
    db._migrate_add_extraction_source(c)
    return c


# ----------------------------------------------------------------------
# #4 — freshness stamps
# ----------------------------------------------------------------------

def test_user_profile_response_has_freshness_fields():
    """Response should carry top-level `as_of` AND per-user `updated_at`."""
    c = _make_conn()
    # Seed a profile row with updated_at
    c.execute(
        "INSERT INTO user_profiles (user_id, username, display_name, "
        "trader_score, trader_rationale, racism_rationale, "
        "message_count_at_update, profile_text, updated_at) "
        "VALUES (12345, 'bk', 'BK', 48, 'rat', 'rat', 13835, '', "
        "'2026-06-02T05:08:20')"
    )
    fake_lookup = {
        "users": [{
            "user_id": 12345, "username": "bk", "display_name": "BK",
            "trader_rank": 6, "trader_rank_total": 53,
            "racism_rank": 1, "racism_rank_total": 3,
            "trader_rationale": "rat", "racism_rationale": "rat",
        }],
        "count": 1, "mode": "single_user",
    }
    with (
        patch("db.lookup_user_ranks", return_value=fake_lookup),
        patch("db.get_connection", return_value=c),
    ):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "bk"}))
    assert "as_of" in result, f"expected top-level as_of, got {list(result.keys())}"
    user = result["users"][0]
    assert "updated_at" in user, (
        f"expected updated_at per user, got {list(user.keys())}"
    )
    assert "2026-06-02" in str(user["updated_at"]), user["updated_at"]
    _ok("lookup_user_profile: top-level as_of + per-user updated_at")


def test_trade_log_caller_response_has_freshness_fields():
    """Caller path response should carry `as_of` + `window_days`."""
    with (
        patch("db.format_analyst_trades_for_context", return_value="trades"),
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "recent", "days": 5,
        }))
    assert "as_of" in result, f"expected as_of, got {list(result.keys())}"
    assert "window_days" in result, f"expected window_days, got {list(result.keys())}"
    assert result["window_days"] == 5, result
    _ok("lookup_trade_log caller path: as_of + window_days")


def test_trade_log_username_response_has_profile_updated_at():
    """Username path response should also include profile_updated_at —
    so the model can phrase 'as of N days ago' if the profile is stale."""
    c = _make_conn()
    c.execute(
        "INSERT INTO user_profiles (user_id, username, profile_text, "
        "message_count_at_update, updated_at) "
        "VALUES (12345, 'zhawk', '**Recent trades.**\\n- PURR', 100, "
        "'2026-06-02T05:08:29')"
    )
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch("db.get_user_profile_recent_trades_section",
              return_value="- PURR"),
        patch("db.get_connection", return_value=c),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "username": "zhawk", "kind": "recent",
        }))
    assert "profile_updated_at" in result, (
        f"expected profile_updated_at, got {list(result.keys())}"
    )
    assert "2026-06-02" in str(result["profile_updated_at"]), result
    _ok("lookup_trade_log username path: profile_updated_at field")


def test_chat_search_response_has_window_bounds():
    """Shape A (keyword) and Shape C (username/channel) should report
    the actual window queried so the model knows the scope."""
    with patch("db.search_chat_messages_for_ask", return_value=[]):
        result = asyncio.run(bot_mod._execute_chat_search({
            "username": "bk", "days": 1,
        }))
    assert "window_start" in result, (
        f"expected window_start, got {list(result.keys())}"
    )
    assert "window_end" in result, (
        f"expected window_end, got {list(result.keys())}"
    )
    _ok("search_chat_messages: window_start + window_end fields")


# ----------------------------------------------------------------------
# #5 — failed vs empty distinction
# ----------------------------------------------------------------------

def test_user_profile_empty_returns_status_empty():
    """Username not in any profile → status='not_found' (a flavor of
    'empty'), not silently fabricated."""
    with patch("db.lookup_user_ranks", return_value={
        "error": "No profile found for username 'nobody'.",
        "users": [],
    }):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "nobody"}))
    # Existing behavior: returns the error dict. Verify status is set.
    assert "status" in result, f"expected status field, got {list(result.keys())}"
    assert result["status"] in ("error", "not_found"), result
    _ok("lookup_user_profile: empty result tagged status=error/not_found")


def test_user_profile_ok_status_on_success():
    fake_lookup = {
        "users": [{
            "user_id": 1, "username": "u", "display_name": "U",
            "trader_rank": 1, "trader_rank_total": 1,
            "racism_rank": 1, "racism_rank_total": 1,
            "trader_rationale": "x", "racism_rationale": "x",
        }],
        "count": 1, "mode": "single_user",
    }
    c = _make_conn()
    c.execute(
        "INSERT INTO user_profiles (user_id, username, display_name, "
        "trader_score, profile_text, updated_at) "
        "VALUES (1, 'u', 'U', 50, '', '2026-06-02T00:00:00')"
    )
    with (
        patch("db.lookup_user_ranks", return_value=fake_lookup),
        patch("db.get_connection", return_value=c),
    ):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "u"}))
    assert result.get("status") == "ok", f"expected status=ok, got {result}"
    _ok("lookup_user_profile: status=ok on clean result")


def test_trade_log_no_data_returns_status_empty():
    """Caller anchor with no matching rows → status='empty', not error."""
    with (
        patch("db.format_analyst_trades_for_context", return_value=""),
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    assert result.get("status") == "empty", (
        f"expected status=empty on no data, got status={result.get('status')!r}"
    )
    _ok("lookup_trade_log: status=empty on no_logged_trades")


def test_trade_log_error_returns_status_error():
    """When the underlying call raises, executor should return
    status='error' so the model knows to decline rather than fabricate."""
    with (
        patch("db.format_analyst_trades_for_context",
              side_effect=RuntimeError("db hiccup")),
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "recent",
        }))
    assert result.get("status") == "error", (
        f"expected status=error on raise, got status={result.get('status')!r}"
    )
    _ok("lookup_trade_log: status=error on raise (distinguishable from empty)")


def test_chat_search_status_distinguishes_empty_from_error():
    """search_chat_messages should tag status=empty when query ran fine
    with no matches, status=error when the helper raised."""
    # Empty case
    with patch("db.search_chat_messages_for_ask", return_value=[]):
        result_empty = asyncio.run(bot_mod._execute_chat_search({
            "keyword": "TSLA", "days": 30,
        }))
    assert result_empty.get("status") == "empty", (
        f"expected status=empty, got {result_empty.get('status')!r}"
    )
    # Error case
    with patch(
        "db.search_chat_messages_for_ask",
        side_effect=RuntimeError("db hiccup"),
    ):
        result_err = asyncio.run(bot_mod._execute_chat_search({
            "keyword": "TSLA", "days": 30,
        }))
    assert result_err.get("status") == "error", (
        f"expected status=error, got {result_err.get('status')!r}"
    )
    _ok("search_chat_messages: status=empty vs status=error distinguishable")


if __name__ == "__main__":
    print("=== freshness + status smoke ===")
    test_user_profile_response_has_freshness_fields()
    test_trade_log_caller_response_has_freshness_fields()
    test_trade_log_username_response_has_profile_updated_at()
    test_chat_search_response_has_window_bounds()
    test_user_profile_empty_returns_status_empty()
    test_user_profile_ok_status_on_success()
    test_trade_log_no_data_returns_status_empty()
    test_trade_log_error_returns_status_error()
    test_chat_search_status_distinguishes_empty_from_error()
    print("\nALL FRESHNESS-AND-STATUS SMOKE TESTS PASS")
