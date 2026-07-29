"""Smoke: query_data — flexible read-only SQL wrangling tool (2026-07-29).

The missing capability for "analyze X over time" / activity / trend
questions: no tool could return aggregates over the 165K-row
chat_messages corpus. query_data lets the model write read-only SELECTs
(schema + semantics in its description) so the analysis directive +
code execution can chart real aggregates. Safety is the whole game:
read-only connection, SELECT/WITH-only, no multi-statement, no write/
DDL/PRAGMA/ATTACH, row cap, timeout.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_validation_allows_select_and_with():
    from discord_bot.bot import _validate_select_sql
    for q in ("SELECT COUNT(*) FROM chat_messages",
              "  select author_username, count(*) from chat_messages group by 1",
              "WITH t AS (SELECT 1 x) SELECT * FROM t"):
        ok, _ = _validate_select_sql(q)
        assert ok, f"should allow: {q!r}"
    _ok("validation allows SELECT / WITH")


def test_validation_blocks_writes_and_tricks():
    from discord_bot.bot import _validate_select_sql
    for q in ("INSERT INTO chat_messages VALUES (1)",
              "UPDATE user_profiles SET trader_score=100",
              "DELETE FROM analyst_trades",
              "DROP TABLE chat_messages",
              "PRAGMA table_info(chat_messages)",
              "ATTACH DATABASE 'x' AS y",
              "SELECT 1; DROP TABLE chat_messages",
              "CREATE TABLE x (a)",
              ""):
        ok, err = _validate_select_sql(q)
        assert not ok, f"MUST block: {q!r} (got ok, err={err})"
    _ok("validation blocks writes / DDL / PRAGMA / ATTACH / multi-statement")


def _temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE chat_messages (id INTEGER, author_username TEXT, "
              "content TEXT, posted_at TEXT)")
    c.executemany(
        "INSERT INTO chat_messages VALUES (?,?,?,?)",
        [(i, f"u{i%3}", "x" * 900, f"2026-07-{10+i%5:02d}") for i in range(20)],
    )
    c.commit()
    c.close()
    return path


def test_select_returns_rows_and_clamps_text():
    import discord_bot.bot as bot
    from config import settings
    path = _temp_db()
    try:
        with patch.object(settings, "db_path", path):
            res = asyncio.run(bot._execute_query_data(
                {"sql": "SELECT author_username, COUNT(*) n "
                        "FROM chat_messages GROUP BY 1 ORDER BY 1"}))
        assert res["status"] == "ok", res
        assert res["columns"] == ["author_username", "n"], res
        assert res["row_count"] == 3, res
        # a wide text field must be clamped so SELECT * can't blow context
        with patch.object(settings, "db_path", path):
            res2 = asyncio.run(bot._execute_query_data(
                {"sql": "SELECT content FROM chat_messages LIMIT 1"}))
        assert len(res2["rows"][0]["content"]) <= 420, "text not clamped"
    finally:
        os.remove(path)
    _ok("SELECT returns rows; wide text fields clamped")


def test_write_blocked_at_execution_readonly():
    import discord_bot.bot as bot
    from config import settings
    path = _temp_db()
    try:
        # bypass the validator to prove the CONNECTION itself is read-only
        with patch.object(settings, "db_path", path), \
             patch.object(bot, "_validate_select_sql",
                          return_value=(True, "DELETE FROM chat_messages")):
            res = asyncio.run(bot._execute_query_data(
                {"sql": "DELETE FROM chat_messages"}))
        assert res["status"] == "error", "read-only conn must reject the write"
        # data still intact
        c = sqlite3.connect(path)
        n = c.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0]
        c.close()
        assert n == 20, f"rows were deleted through a 'read-only' path: {n}"
    finally:
        os.remove(path)
    _ok("read-only connection rejects writes even if validation is bypassed")


def test_declaration_and_wiring():
    import discord_bot.bot as bot
    import inspect
    decl = inspect.getsource(bot._build_query_data_tool)
    assert "query_data" in decl and "chat_messages" in decl, (
        "declaration must name the tool + expose the schema"
    )
    assert "wins-biased" in decl.lower() or "wins-bias" in decl.lower(), (
        "declaration must warn about the wins-biased trade ledger"
    )
    src = inspect.getsource(bot._answer_with_gemini)
    assert '"query_data": _execute_query_data' in src, "not in executor map"
    _ok("query_data declared (schema + caveats) and wired into the loop")


if __name__ == "__main__":
    print("=== query_data smoke ===")
    test_validation_allows_select_and_with()
    test_validation_blocks_writes_and_tricks()
    test_select_returns_rows_and_clamps_text()
    test_write_blocked_at_execution_readonly()
    test_declaration_and_wiring()
    print("\nALL QUERY_DATA SMOKE TESTS PASS")
