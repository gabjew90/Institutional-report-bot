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
    # A real legacy row came from the image-OCR pipeline, so it HAS an
    # image_url. That is what makes it image-sourced — not the absence
    # of a label.
    conn.execute(
        "INSERT INTO analyst_trades (discord_message_id, discord_attachment_id, "
        "author, posted_at, image_url) "
        "VALUES (1, 1, 'abe', '2026-05-01T12:00:00', 'https://cdn/x.png')"
    )

    # Run the migration: add column + backfill
    db._migrate_add_extraction_source(conn)

    row = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()
    assert row[0] == "image", f"expected 'image', got {row[0]!r}"
    _ok("legacy rows backfilled to extraction_source='image'")


def test_boot_backfill_does_not_stamp_text_rows_as_image():
    """2026-07-30: the label said 31,356 July rows were image-sourced.

    record_analyst_trade never wrote extraction_source, so every row it
    inserted was NULL — and this migration runs on EVERY boot and
    blanket-stamped NULL -> 'image'. Plain-text chat messages therefore
    came back as screenshots. Investigating abe's win count, the column
    claimed text extraction had been dead since 2026-06-01 when it was
    in fact producing 369 trades a month.

    A row with no image_url cannot have come from the image pipeline.
    """
    conn = sqlite3.connect(":memory:")
    db._init_schema(conn)
    db._migrate_add_extraction_source(conn)
    conn.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, posted_at, caption, image_url) "
        # attachment id 0 = no attachment, i.e. a plain chat message
        "VALUES (5, 0, 'abe', '2026-07-15T12:00:00', 'Sold DELL', NULL)"
    )
    db._migrate_add_extraction_source(conn)  # simulate the next boot
    got = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()[0]
    assert got == "text", (
        f"a row with no image_url must not be labelled image-sourced, "
        f"got {got!r}"
    )
    _ok("boot backfill derives from image_url, not a blanket 'image'")


def test_repairs_historically_mislabeled_rows():
    """The 31K rows already stamped 'image' in prod must be repaired."""
    conn = sqlite3.connect(":memory:")
    db._init_schema(conn)
    db._migrate_add_extraction_source(conn)
    conn.executemany(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, posted_at, image_url, "
        "extraction_source) VALUES (?, ?, 'abe', '2026-07-15T12:00:00', "
        "?, ?)",
        [(10, 0, None, "image"),             # mislabeled text message
         (11, 11, "https://cdn/y.png", "image"),   # genuine screenshot
         (12, 0, None, "text"),              # already correct
         (13, 0, None, "mixed"),             # explicit, leave alone
         # Ambiguous: carries an attachment id but no stored url. 3 such
         # rows exist in prod. An attachment is image evidence, so don't
         # flip these to 'text' on the strength of a missing url.
         (14, 99, None, "image")],
    )
    db._migrate_add_extraction_source(conn)
    rows = dict(conn.execute(
        "SELECT discord_message_id, extraction_source FROM analyst_trades"
    ).fetchall())
    assert rows[10] == "text", f"mislabeled row not repaired: {rows}"
    assert rows[11] == "image", f"genuine screenshot clobbered: {rows}"
    assert rows[12] == "text", f"correct row disturbed: {rows}"
    assert rows[13] == "mixed", (
        f"explicit 'mixed' must survive — it is set deliberately by the "
        f"classifier when both modalities contributed: {rows}"
    )
    assert rows[14] == "image", (
        f"a row with an attachment id has image evidence and must not be "
        f"flipped to 'text' just because the url is missing: {rows}"
    )
    _ok("mislabeled history repaired; genuine + explicit labels untouched")


def test_record_analyst_trade_tags_its_own_modality():
    """Fix the source, not just the backfill: the insert must say which
    modality produced the row instead of leaving NULL for a migration to
    guess at."""
    conn = sqlite3.connect(":memory:")
    db._init_schema(conn)
    db._migrate_add_extraction_source(conn)
    orig = db.get_connection
    db.get_connection = lambda: conn
    try:
        db.record_analyst_trade(
            discord_message_id=20, discord_attachment_id=0,
            author="abe", posted_at="2026-07-15T12:00:00",
            image_url=None, caption="Sold DELL way too early smh",
            is_trade=False, gemini_json=None)
        db.record_analyst_trade(
            discord_message_id=21, discord_attachment_id=1,
            author="abe", posted_at="2026-07-15T12:01:00",
            image_url="https://cdn/z.png", caption=None,
            is_trade=True, gemini_json=None, ticker="NVDA", action="close")
    finally:
        db.get_connection = orig
    rows = dict(conn.execute(
        "SELECT discord_message_id, extraction_source FROM analyst_trades"
    ).fetchall())
    assert rows[20] == "text", f"text message mistagged: {rows}"
    assert rows[21] == "image", f"screenshot mistagged: {rows}"
    _ok("record_analyst_trade tags modality at insert time")


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
    test_boot_backfill_does_not_stamp_text_rows_as_image()
    test_repairs_historically_mislabeled_rows()
    test_record_analyst_trade_tags_its_own_modality()
    test_insert_accepts_text_source()
    print("\nALL EXTRACTION-SOURCE-COLUMN SMOKE TESTS PASS")
