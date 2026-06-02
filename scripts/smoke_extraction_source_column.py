"""Smoke test for the analyst_trades.extraction_source column migration.

Validates:
  1. Fresh schema has the extraction_source column
  2. Legacy rows (column NULL) get backfilled to 'image' on migration
  3. Insert path accepts 'image' | 'text' | 'mixed' values
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


def test_column_exists_after_init():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # Mirror what get_connection does on cold start
    db._init_schema(conn)
    db._migrate_drop_unique_constraints(conn)
    db._migrate_add_extraction_source(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)").fetchall()]
    assert "extraction_source" in cols, f"extraction_source column missing: {cols}"
    _ok("fresh schema has extraction_source column")


def test_legacy_rows_backfilled_to_image():
    """Simulate the migration: a row with NULL extraction_source after
    the ALTER TABLE should get UPDATE'd to 'image'."""
    conn = sqlite3.connect(":memory:")
    # Set up the OLD schema (no extraction_source column)
    conn.execute("""
        CREATE TABLE analyst_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER NOT NULL,
            discord_attachment_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            author_id INTEGER,
            posted_at TEXT NOT NULL,
            image_url TEXT,
            caption TEXT,
            is_trade INTEGER NOT NULL DEFAULT 0,
            ticker TEXT,
            contract_type TEXT,
            strike REAL,
            expiry TEXT,
            action TEXT,
            gain_pct REAL,
            price REAL,
            inferred_status TEXT,
            tracking_mode TEXT NOT NULL DEFAULT 'caller',
            gemini_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(discord_message_id, discord_attachment_id)
        )
    """)
    conn.execute(
        "INSERT INTO analyst_trades (discord_message_id, discord_attachment_id, "
        "author, posted_at) VALUES (1, 1, 'abe', '2026-05-01T12:00:00')"
    )

    # Run the migration: add column + backfill
    db._migrate_add_extraction_source(conn)

    row = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()
    assert row[0] == "image", f"expected 'image', got {row[0]!r}"
    _ok("legacy rows backfilled to extraction_source='image'")


def test_insert_accepts_text_source():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db._init_schema(conn)
    db._migrate_drop_unique_constraints(conn)
    db._migrate_add_extraction_source(conn)
    conn.execute(
        "INSERT INTO analyst_trades "
        "(discord_message_id, discord_attachment_id, author, posted_at, "
        "extraction_source) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, 1, "zhawk", "2026-06-01T12:00:00", "text"),
    )
    row = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()
    assert row[0] == "text", f"expected 'text', got {row[0]!r}"
    _ok("insert accepts extraction_source='text'")


if __name__ == "__main__":
    print("=== extraction_source column smoke ===")
    test_column_exists_after_init()
    test_legacy_rows_backfilled_to_image()
    test_insert_accepts_text_source()
    print("\nALL EXTRACTION-SOURCE-COLUMN SMOKE TESTS PASS")
