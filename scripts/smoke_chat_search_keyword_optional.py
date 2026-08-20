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


def test_db_layer_shape_c_returns_rows():
    """REAL db function, no mock. The bot-layer tests above mock
    db.search_chat_messages_for_ask, which is exactly how the shape C
    kill went unseen for 11 weeks (2026-05-31 -> 2026-08-19): the DB
    function's early return fired before the username/channel filters
    applied, so every shape C call returned [] while the mocked tests
    passed. This drives the real query against an in-memory DB."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    import db as db_mod

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE chat_messages (
               discord_message_id TEXT, author_username TEXT,
               author_display TEXT, channel_name TEXT, content TEXT,
               posted_at TEXT, image_ocr_text TEXT)"""
    )
    now = datetime.now(timezone.utc)
    rows = [
        ("1", "bankerkyle", "BK", "fantasy-football",
         "drafting a kicker", (now - timedelta(hours=2)).isoformat(), None),
        ("2", "tulch", "Tulch", "fantasy-football",
         "reroll the order", (now - timedelta(hours=1)).isoformat(), None),
        ("3", "bankerkyle", "BK", "stonks-yapping",
         "chop city", (now - timedelta(days=40)).isoformat(), None),
    ]
    conn.executemany("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?)", rows)

    with patch.object(db_mod, "get_connection", return_value=conn):
        # channel only (the fantasy IQ-board call that returned empty)
        got = db_mod.search_chat_messages_for_ask(
            channel_name="fantasy-football", days=30, limit=10)
        assert len(got) == 2, f"channel-only shape C: {len(got)} rows"
        # username only — trailing-days window must still apply
        got = db_mod.search_chat_messages_for_ask(
            username="bankerkyle", days=30, limit=10)
        assert len(got) == 1, f"username-only shape C: {len(got)} rows"
        assert got[0]["channel_name"] == "fantasy-football"
        # no filters at all still returns nothing
        got = db_mod.search_chat_messages_for_ask(days=30, limit=10)
        assert got == [], "filterless call must stay empty"
    _ok("db layer: shape C (channel/username only) returns real rows")


def test_empty_result_carries_no_fabrication_note():
    """An empty tool result must tell the model an empty search is a
    result to report, not a license to invent (2026-08-19: 12 fabricated
    league verdicts off an empty fantasy-channel lookup)."""
    with patch.object(bot_mod.db, "search_chat_messages_for_ask",
                      return_value=[]):
        res = asyncio.run(
            bot_mod._execute_chat_search(
                {"channel_name": "fantasy-football", "days": 30}))
    assert res["status"] == "empty", res
    note = res.get("note") or ""
    assert "do NOT invent" in note, f"empty-result note missing: {res}"
    _ok("empty tool result carries the no-fabrication note")


if __name__ == "__main__":
    print("=== search_chat_messages keyword-optional smoke ===")
    test_username_days_no_keyword_works()
    test_channel_days_no_keyword_works()
    test_keyword_still_works()
    test_time_window_still_works()
    test_nothing_at_all_still_errors()
    test_db_layer_shape_c_returns_rows()
    test_empty_result_carries_no_fabrication_note()
    print("\nALL CHAT-SEARCH-KEYWORD-OPTIONAL SMOKE TESTS PASS")
