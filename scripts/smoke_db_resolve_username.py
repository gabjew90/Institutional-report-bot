"""Smoke test for db.resolve_username_to_user_id.

Validates:
  1. Username with a profile row -> returns user_id from user_profiles
  2. Username with no profile but chat history -> returns user_id from chat_messages
  3. Unknown username -> None
  4. Empty string / None / whitespace -> None
  5. Case-insensitive matching
"""

import sys
import sqlite3
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_test_conn() -> sqlite3.Connection:
    """In-memory DB with the two tables we touch."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER UNIQUE,
            author_id INTEGER NOT NULL,
            author_username TEXT,
            posted_at TEXT NOT NULL
        );
    """)
    return conn


def test_resolves_via_user_profiles():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name) "
        "VALUES (?, ?, ?)",
        (12345, "bankerkyle", "BK"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("bankerkyle")
    assert uid == 12345, f"expected 12345, got {uid!r}"
    _ok("resolves via user_profiles (lowercase exact)")


def test_case_insensitive_via_user_profiles():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name) "
        "VALUES (?, ?, ?)",
        (12345, "bankerkyle", "BK"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("BankerKyle")
    assert uid == 12345, f"expected 12345 for case-insensitive match, got {uid!r}"
    _ok("user_profiles match is case-insensitive")


def test_falls_back_to_chat_messages():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO chat_messages "
        "(discord_message_id, author_id, author_username, posted_at) "
        "VALUES (?, ?, ?, ?)",
        (1001, 67890, "newuser", "2026-06-01T15:00:00Z"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("newuser")
    assert uid == 67890, f"expected 67890 (chat_messages fallback), got {uid!r}"
    _ok("falls back to chat_messages when no profile")


def test_unknown_returns_none():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("nobody")
    assert uid is None, f"expected None for unknown user, got {uid!r}"
    _ok("unknown username -> None")


def test_empty_input_returns_none():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        assert db.resolve_username_to_user_id("") is None, "empty string should return None"
        assert db.resolve_username_to_user_id(None) is None, "None should return None"
        assert db.resolve_username_to_user_id("   ") is None, "whitespace should return None"
    _ok("empty / whitespace / None input -> None")


def test_strips_at_prefix():
    """@-prefix is common in Discord — strip it before matching."""
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name) "
        "VALUES (?, ?, ?)",
        (12345, "bankerkyle", "BK"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("@bankerkyle")
    assert uid == 12345, f"expected 12345 (after stripping @), got {uid!r}"
    _ok("strips leading @ before matching")


if __name__ == "__main__":
    print("=== resolve_username_to_user_id smoke ===")
    test_resolves_via_user_profiles()
    test_case_insensitive_via_user_profiles()
    test_falls_back_to_chat_messages()
    test_unknown_returns_none()
    test_empty_input_returns_none()
    test_strips_at_prefix()
    print("\nALL RESOLVE-USERNAME SMOKE TESTS PASS")
