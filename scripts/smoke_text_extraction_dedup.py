"""Smoke test for the two-tier dedup rule in
db.insert_text_extracted_trade_if_not_dup.

Validates:
  Tier 1 (strict): SAME discord_message_id -> always skip
    (catches the live image+text-caption case where image OCR
    already wrote a row for that message)
  Tier 2 (fuzzy): same-fields against image rows within ±5 min
    (catches cross-message case — text post then screenshot of
    same trade 2 min later in a separate message)
  Negative case: same fields beyond ±5 min window are NOT deduped
"""

import sqlite3
import sys
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db._init_schema(c)
    db._migrate_drop_unique_constraints(c)
    db._migrate_add_extraction_source(c)
    return c


def test_dedup_tier1_same_message_id():
    """Tier 1: if a row exists for the same discord_message_id, skip.
    This catches the live case where image OCR already wrote a row for
    a message with screenshot + caption, and the new classifier tries
    to write a second row for the same message."""
    c = _conn()
    # Image row written first (by ocr_attachments_inline path)
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, action, is_trade, tracking_mode, extraction_source) "
        "VALUES (5000, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'open', 1, 'member', 'image')"
    )
    # Now classifier tries to write a row for the SAME message
    # (different ticker even — but message_id collision wins).
    with patch("db.get_connection", return_value=c):
        skipped = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=5000,  # SAME message_id
            posted_at="2026-06-01T12:00:00",
            extracted={
                "is_trade": True, "action": "close", "ticker": "BTC",
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert skipped is False, "same message_id should always skip"
    rows = c.execute(
        "SELECT COUNT(*) FROM analyst_trades WHERE discord_message_id = 5000"
    ).fetchone()
    assert rows[0] == 1, f"expected 1 row, got {rows[0]}"
    _ok("Tier 1 dedup: same discord_message_id skipped (live image+text case)")


def test_text_skipped_when_image_already_exists():
    """Tier 2: image row within ±5 min for same fields, different msg_id."""
    c = _conn()
    # Insert image row first (T=12:00:00, msg_id=1)
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, contract_type, strike, expiry, action, "
        "is_trade, tracking_mode, extraction_source) "
        "VALUES (1, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'call', 14.0, '2026-12-18', 'open', "
        "1, 'member', 'image')"
    )
    # Now try to insert a text row for the same trade T=12:03:00, msg_id=2
    # (3 min later — within 5 min window, DIFFERENT msg_id)
    with patch("db.get_connection", return_value=c):
        inserted = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=2, posted_at="2026-06-01T12:03:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "PURR",
                "contract_type": "call", "strike": 14.0,
                "expiry": "2026-12-18", "price": None, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert inserted is False, f"expected dedup-skip, got insert={inserted}"
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'PURR'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "image", rows
    _ok("Tier 2 dedup: same-fields image row within ±5 min skips text row")


def test_text_inserted_when_no_image_in_window():
    c = _conn()
    # No prior row. Insert text row first.
    with patch("db.get_connection", return_value=c):
        inserted = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=1, posted_at="2026-06-01T12:00:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "BTC",
                "contract_type": "spot", "strike": None,
                "expiry": None, "price": 73906.0, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert inserted is True, f"expected insert, got {inserted}"
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'BTC'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "text", rows
    _ok("text row inserted when no image row exists in window")


def test_beyond_window_not_deduped():
    c = _conn()
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, action, is_trade, tracking_mode, extraction_source) "
        "VALUES (1, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'open', 1, 'member', 'image')"
    )
    # 10 minutes later — beyond window
    with patch("db.get_connection", return_value=c):
        inserted = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=2, posted_at="2026-06-01T12:10:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "PURR",
                "contract_type": None, "strike": None, "expiry": None,
                "price": None, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert inserted is True, "10-min-later row should NOT be deduped"
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'PURR'"
    ).fetchall()
    assert len(rows) == 2, f"expected 2 rows, got {rows}"
    _ok("trades beyond ±5 min window are not deduped (different events)")


if __name__ == "__main__":
    print("=== text-extraction dedup smoke ===")
    test_dedup_tier1_same_message_id()
    test_text_skipped_when_image_already_exists()
    test_text_inserted_when_no_image_in_window()
    test_beyond_window_not_deduped()
    print("\nALL DEDUP SMOKE TESTS PASS")
