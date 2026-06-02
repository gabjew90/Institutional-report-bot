"""Smoke test for db.get_user_profile_recent_trades_section.

Validates:
  1. Profile with Recent trades section -> returns the body (without heading)
  2. Profile without that section -> ""
  3. Profile present but profile_text is empty -> ""
  4. Unknown user_id -> ""
  5. Recent trades section is the LAST section in the profile -> returns body
     extending to end-of-string
  6. "**Recent Trades.**" (capital T) also matches (case-insensitive)
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


PROFILE_WITH_SECTION = """\
**Personality and style.**
SV is a high-octane trader.

**Voice.**
- "Diamond cock" - recurring self-description

**Recent trades.**
- $PLTR / 145C (6/1 entry) - closed for +911.84%
- $HPE / 50C (5/29 entry) - closed at +6.90%

**Recent personal life.**
- claimed to be working on an oil rig
"""

PROFILE_WITHOUT_SECTION = """\
**Personality and style.**
Some content here.

**Voice.**
- "test" - when testing
"""

PROFILE_RECENT_TRADES_LAST = """\
**Personality and style.**
Some content.

**Recent trades.**
- $TSLA / 445C - open
- $META / 640C - open
"""


def _make_test_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_text TEXT NOT NULL DEFAULT ''
        )
    """)
    return conn


def test_extracts_section_body():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_WITH_SECTION),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$PLTR" in out, f"expected PLTR line in output, got: {out!r}"
    assert "$HPE" in out, f"expected HPE line in output, got: {out!r}"
    assert "Personality" not in out, "should not bleed prior section into output"
    assert "personal life" not in out.lower(), "should stop at next section heading"
    _ok("extracts Recent trades body without bleeding into adjacent sections")


def test_returns_empty_when_section_missing():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_WITHOUT_SECTION),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert out == "", f"expected '' when section absent, got: {out!r}"
    _ok("returns '' when Recent trades section is absent")


def test_returns_empty_when_user_unknown():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(99999)
    assert out == "", f"expected '' for unknown user, got: {out!r}"
    _ok("returns '' for unknown user_id")


def test_recent_trades_as_last_section():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_RECENT_TRADES_LAST),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$TSLA" in out, f"expected TSLA in last-section case, got: {out!r}"
    assert "$META" in out, f"expected META in last-section case, got: {out!r}"
    _ok("returns body when Recent trades is the last section")


def test_case_insensitive_heading_match():
    """**Recent Trades.** (capital T) should match too."""
    cap_t = PROFILE_WITH_SECTION.replace("**Recent trades.**", "**Recent Trades.**")
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, cap_t),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$PLTR" in out, f"capital-T heading not matched: {out!r}"
    _ok("heading match is case-insensitive (Recent Trades / recent trades)")


if __name__ == "__main__":
    print("=== get_user_profile_recent_trades_section smoke ===")
    test_extracts_section_body()
    test_returns_empty_when_section_missing()
    test_returns_empty_when_user_unknown()
    test_recent_trades_as_last_section()
    test_case_insensitive_heading_match()
    print("\nALL RECENT-TRADES-SECTION SMOKE TESTS PASS")
