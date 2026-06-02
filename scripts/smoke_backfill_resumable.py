"""Smoke test for the backfill script's resume logic."""

import sqlite3
import sys
from unittest.mock import patch

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


def test_checkpoint_read_write():
    """Write a checkpoint, read it back."""
    import scripts.backfill_text_extracted_trades as bf
    c = _make_conn()
    with patch("db.get_connection", return_value=c):
        bf._write_checkpoint(channel="zhawk-thawghts", last_msg_id=12345)
        got = bf._read_checkpoint(channel="zhawk-thawghts")
    assert got == 12345, f"checkpoint round-trip failed: got {got!r}"
    _ok("checkpoint write + read round-trips")


def test_resume_skips_already_processed():
    """If checkpoint says last_msg_id=10, the backfill only processes
    messages with discord_message_id > 10."""
    import scripts.backfill_text_extracted_trades as bf
    c = _make_conn()
    with patch("db.get_connection", return_value=c):
        # Insert messages with ids 5, 15, 25 in zhawk-thawghts
        for mid in (5, 15, 25):
            c.execute(
                "INSERT INTO chat_messages (discord_message_id, channel_id, "
                "channel_name, author_id, author_username, posted_at, "
                "content, posted_at) VALUES (?, 1, '🫦-zhawk-thawghts-🗣', "
                "100, 'u', datetime('now'), 'PURR Leaps 12/18 $14', "
                "datetime('now'))",
                (mid,),
            ) if False else c.execute(
                "INSERT INTO chat_messages (discord_message_id, channel_id, "
                "channel_name, author_id, author_username, posted_at, content) "
                "VALUES (?, 1, '🫦-zhawk-thawghts-🗣', 100, 'u', "
                "datetime('now'), 'PURR Leaps 12/18 $14')",
                (mid,),
            )
        bf._write_checkpoint(channel="🫦-zhawk-thawghts-🗣", last_msg_id=10)
        to_process = bf._iter_messages_to_process("🫦-zhawk-thawghts-🗣")
        ids = [r["discord_message_id"] for r in to_process]
    assert ids == [15, 25], f"expected [15, 25], got {ids}"
    _ok("resume skips messages with discord_message_id <= checkpoint")


def test_skips_messages_with_existing_analyst_row():
    """If analyst_trades already has a row for a discord_message_id, the
    backfill skips that message entirely (saves a Gemini call)."""
    import scripts.backfill_text_extracted_trades as bf
    import asyncio
    c = _make_conn()
    with patch("db.get_connection", return_value=c):
        # Add a chat message
        c.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, posted_at, content) "
            "VALUES (?, 1, '🕰️-member-alerts-🕰️', 200, 'someone', "
            "datetime('now'), 'PURR opened')",
            (999,),
        )
        # Add an existing analyst_trades row for the same message
        c.execute(
            "INSERT INTO analyst_trades (discord_message_id, "
            "discord_attachment_id, author, author_id, posted_at, ticker, "
            "action, is_trade, tracking_mode, extraction_source) "
            "VALUES (999, 1, 'someone', 200, datetime('now'), 'PURR', "
            "'open', 1, 'member', 'image')"
        )
        # Now run the backfill on this single row
        row = c.execute(
            "SELECT * FROM chat_messages WHERE discord_message_id = 999"
        ).fetchone()
        wrote = asyncio.run(bf._process_message(row, "🕰️-member-alerts-🕰️"))
    assert wrote is False, "expected skip (existing analyst row), got write"
    _ok("backfill skips messages with existing analyst_trades row")


if __name__ == "__main__":
    print("=== backfill resume smoke ===")
    test_checkpoint_read_write()
    test_resume_skips_already_processed()
    test_skips_messages_with_existing_analyst_row()
    print("\nALL BACKFILL SMOKE TESTS PASS")
