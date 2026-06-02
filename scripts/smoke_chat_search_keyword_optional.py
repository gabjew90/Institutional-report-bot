"""Smoke test for the keyword-optional extension of search_chat_messages.

Validates:
  1. Username + days (no keyword, no time window) is now accepted
  2. Channel + days (no keyword, no username) is accepted
  3. Username + keyword still works as before
  4. start_iso + end_iso shape still works as before
  5. Nothing at all (no keyword, no username, no channel, no window) -> error
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


def test_username_days_no_keyword_works():
    """The new shape: 'what has Kyle been crying about today' pattern."""
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "bankerkyle",
         "channel": "stonks-yapping", "content": "fk me"},
    ]
    with patch("db.search_chat_messages_for_ask", return_value=fake_matches) as mock_search:
        result = asyncio.run(bot_mod._execute_chat_search({
            "username": "bankerkyle", "days": 1,
        }))
    assert mock_search.called, "db.search_chat_messages_for_ask not called"
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs.get("username") == "bankerkyle", call_kwargs
    # keyword should be None to signal username-only retrieval
    assert not call_kwargs.get("keyword"), (
        f"expected empty/None keyword, got {call_kwargs.get('keyword')!r}"
    )
    assert "matches" in result, result
    _ok("username + days (no keyword) is accepted and queries by user")


def test_channel_days_no_keyword_works():
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "user1",
         "channel": "test", "content": "msg"},
    ]
    with patch("db.search_chat_messages_for_ask", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "channel_name": "test", "days": 1,
        }))
    assert "matches" in result, result
    _ok("channel_name + days (no keyword, no username) is accepted")


def test_keyword_still_works():
    """Regression: existing shape A (keyword search) still works."""
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "bankerkyle",
         "channel": "stonks-yapping", "content": "TSLA looks good"},
    ]
    with patch("db.search_chat_messages_for_ask", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "keyword": "TSLA", "username": "bankerkyle", "days": 30,
        }))
    assert "matches" in result, result
    _ok("regression: keyword+username shape still works")


def test_time_window_still_works():
    """Regression: existing shape B (time window) still works."""
    fake_matches = [{"posted_at": "2026-06-01T15:30", "content": "msg"}]
    with patch("db.search_chat_messages_for_ask", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "start_iso": "2026-06-01T15:00:00Z",
            "end_iso": "2026-06-01T16:00:00Z",
        }))
    assert "matches" in result, result
    _ok("regression: start_iso + end_iso time-window shape still works")


def test_nothing_at_all_still_errors():
    """No filter of any kind -> error (prevents full-table scan)."""
    result = asyncio.run(bot_mod._execute_chat_search({}))
    assert "error" in result, f"expected error for no-filter call, got {result}"
    _ok("no keyword + no window + no username + no channel -> error")


if __name__ == "__main__":
    print("=== search_chat_messages keyword-optional smoke ===")
    test_username_days_no_keyword_works()
    test_channel_days_no_keyword_works()
    test_keyword_still_works()
    test_time_window_still_works()
    test_nothing_at_all_still_errors()
    print("\nALL CHAT-SEARCH-KEYWORD-OPTIONAL SMOKE TESTS PASS")
