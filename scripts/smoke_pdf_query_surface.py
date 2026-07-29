"""Smoke: PDF research data is queryable by a text-to-SQL bot.

2026-07-29 storage review: of ~28 structured fields the analyzer
extracts per PDF, exactly ONE (priority) was a real column — source,
report_type, title, tickers, trade ideas all locked inside the
analysis_json blob. Consequences: source/type filtering needed
json_extract full scans, ticker search needed an unindexable json_each
cross-join, `published_at` was NULL in all 13,411 rows (only set on
read-back, never at insert), and the append-only MAX(id) dedup was a
correctness landmine (375 PDFs have >1 analysis).

Fix (all idempotent, zero backfill for the columns):
  - generated columns source/report_type/title + indexes
  - indexes on priority and (pdf_file_id, id DESC)
  - `latest_pdf_analyses` VIEW: pre-deduped + joined to pdf_files, so
    the bot never reconstructs the MAX(id) CTE and gets a real date
  - `pdf_entities` child table (ticker-indexed) + backfill
"""

import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _analysis(source, rtype, title, tickers):
    return json.dumps({
        "source": source, "report_type": rtype, "title": title,
        "entities_mentioned": [
            {"name": f"{t} Inc", "ticker": t, "asset_class": "stock"}
            for t in tickers
        ],
        "key_insights": ["something happened"],
    })


def _build_db():
    """A miniature of the real schema, then run the migration on it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE pdf_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT, dropbox_path TEXT,
            dropbox_modified_at TEXT, downloaded_at TEXT, status TEXT
        );
        CREATE TABLE pdf_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_file_id INTEGER NOT NULL REFERENCES pdf_files(id),
            triage_json TEXT, analysis_json TEXT, priority TEXT,
            pages_analyzed INTEGER, total_pages INTEGER,
            input_tokens_used INTEGER, output_tokens_used INTEGER,
            model_used TEXT, analysis_duration_seconds REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    c.execute("INSERT INTO pdf_files (id, file_name, dropbox_modified_at) "
              "VALUES (1, 'gs_note.pdf', '2026-07-20T10:00:00Z')")
    c.execute("INSERT INTO pdf_files (id, file_name, dropbox_modified_at) "
              "VALUES (2, 'jpm_note.pdf', '2026-07-21T10:00:00Z')")
    # pdf 1 analyzed TWICE (the dedup landmine); pdf 2 once
    c.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json, priority)"
              " VALUES (1, ?, 'high')",
              (_analysis("Goldman Sachs", "macro", "Old Take", ["NVDA"]),))
    c.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json, priority)"
              " VALUES (1, ?, 'high')",
              (_analysis("Goldman Sachs", "macro", "New Take", ["NVDA", "AMD"]),))
    c.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json, priority)"
              " VALUES (2, ?, 'medium')",
              (_analysis("JPMorgan", "equity_research", "Chips", ["AMD"]),))
    c.commit()
    return path, c


def test_generated_columns_and_indexes():
    import db as dbmod
    path, c = _build_db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        rows = c.execute(
            "SELECT source, report_type, title FROM pdf_analyses "
            "WHERE source='Goldman Sachs' ORDER BY id").fetchall()
        assert len(rows) == 2, rows
        assert rows[1][2] == "New Take", rows
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        for want in ("idx_pdf_analyses_source", "idx_pdf_analyses_report_type",
                     "idx_pdf_analyses_priority", "idx_pdf_analyses_latest"):
            assert want in idx, f"missing index {want}: {idx}"
    finally:
        c.close(); os.remove(path)
    _ok("generated columns (source/report_type/title) + indexes present")


def test_latest_view_dedupes_and_dates():
    import db as dbmod
    path, c = _build_db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        rows = c.execute(
            "SELECT pdf_file_id, title, file_name, published_at "
            "FROM latest_pdf_analyses ORDER BY pdf_file_id").fetchall()
        assert len(rows) == 2, f"view must dedupe to 1 row per PDF: {rows}"
        assert rows[0][1] == "New Take", f"must keep the LATEST: {rows}"
        assert rows[0][2] == "gs_note.pdf", "view must join pdf_files"
        assert rows[0][3].startswith("2026-07-20"), (
            f"view must expose a real date (analysis_json.published_at is "
            f"always null): {rows}"
        )
    finally:
        c.close(); os.remove(path)
    _ok("latest_pdf_analyses view dedupes, joins files, exposes a date")


def test_entities_backfilled_and_indexed():
    import db as dbmod
    path, c = _build_db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        n = c.execute("SELECT COUNT(*) FROM pdf_entities").fetchone()[0]
        assert n >= 4, f"entities not backfilled: {n}"
        amd = c.execute(
            "SELECT COUNT(*) FROM pdf_entities WHERE ticker='AMD'"
        ).fetchone()[0]
        assert amd >= 2, f"AMD should appear in 2 analyses: {amd}"
        idx = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_pdf_entities_ticker" in idx, idx
    finally:
        c.close(); os.remove(path)
    _ok("pdf_entities backfilled from JSON + ticker-indexed")


def test_migration_idempotent():
    import db as dbmod
    path, c = _build_db()
    try:
        dbmod._migrate_pdf_query_surface(c)
        n1 = c.execute("SELECT COUNT(*) FROM pdf_entities").fetchone()[0]
        dbmod._migrate_pdf_query_surface(c)  # second run must be a no-op
        n2 = c.execute("SELECT COUNT(*) FROM pdf_entities").fetchone()[0]
        assert n1 == n2, f"re-running duplicated entities: {n1} -> {n2}"
    finally:
        c.close(); os.remove(path)
    _ok("migration is idempotent (safe on every boot)")


if __name__ == "__main__":
    print("=== pdf query surface smoke ===")
    test_generated_columns_and_indexes()
    test_latest_view_dedupes_and_dates()
    test_entities_backfilled_and_indexed()
    test_migration_idempotent()
    print("\nALL PDF QUERY SURFACE SMOKE TESTS PASS")
