"""SQLite database setup and query helpers."""

import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

from config import settings

_conn: sqlite3.Connection | None = None


def get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path = Path(settings.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _init_schema(_conn)
        _migrate_drop_unique_constraints(_conn)
    return _conn


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dropbox_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            cursor TEXT,
            last_poll_at TEXT
        );

        INSERT OR IGNORE INTO dropbox_state (id, cursor, last_poll_at) VALUES (1, NULL, NULL);

        CREATE TABLE IF NOT EXISTS pdf_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dropbox_path TEXT NOT NULL UNIQUE,
            dropbox_rev TEXT,
            file_name TEXT NOT NULL,
            file_size_bytes INTEGER,
            dropbox_modified_at TEXT,
            downloaded_at TEXT,
            local_path TEXT,
            status TEXT NOT NULL DEFAULT 'DOWNLOADED',
            priority TEXT,
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_pdf_files_status ON pdf_files(status);
        CREATE INDEX IF NOT EXISTS idx_pdf_files_downloaded_at ON pdf_files(downloaded_at);

        -- NOTE: no UNIQUE on pdf_file_id — we keep every analysis run as history.
        -- Use ORDER BY id DESC LIMIT 1 to get the latest for a pdf_file_id.
        CREATE TABLE IF NOT EXISTS pdf_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_file_id INTEGER NOT NULL REFERENCES pdf_files(id),
            triage_json TEXT,
            analysis_json TEXT,
            priority TEXT,
            pages_analyzed INTEGER,
            total_pages INTEGER,
            input_tokens_used INTEGER,
            output_tokens_used INTEGER,
            model_used TEXT,
            analysis_duration_seconds REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_pdf_analyses_file_id ON pdf_analyses(pdf_file_id);

        -- NOTE: no UNIQUE on (report_date, report_type) — every pulse run kept as history.
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            report_type TEXT NOT NULL,
            report_json TEXT NOT NULL,
            report_markdown TEXT NOT NULL,
            pdf_count INTEGER,
            input_tokens_used INTEGER,
            output_tokens_used INTEGER,
            discord_sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_daily_reports_type_created ON daily_reports(report_type, created_at);

        CREATE TABLE IF NOT EXISTS processing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pdf_file_id INTEGER REFERENCES pdf_files(id),
            event_type TEXT NOT NULL,
            status TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()


def _migrate_drop_unique_constraints(conn: sqlite3.Connection) -> None:
    """One-time migration: drop UNIQUE(pdf_file_id) and UNIQUE(report_date, report_type).

    SQLite can't DROP an implicit unique index defined in CREATE TABLE. The only way
    is to rebuild the table. Safe because data is preserved, structure is relaxed.
    """
    import logging
    log = logging.getLogger(__name__)

    def has_unique(table: str, col_tuple: tuple[str, ...]) -> bool:
        """Check if a table has a UNIQUE index covering exactly these columns."""
        rows = conn.execute(f"PRAGMA index_list('{table}')").fetchall()
        for r in rows:
            if r["unique"] and r["origin"] == "u":  # 'u' means explicit or auto UNIQUE
                idx_name = r["name"]
                cols = [c["name"] for c in conn.execute(f"PRAGMA index_info('{idx_name}')").fetchall()]
                if tuple(cols) == col_tuple:
                    return True
        return False

    # pdf_analyses: drop UNIQUE(pdf_file_id)
    if has_unique("pdf_analyses", ("pdf_file_id",)):
        log.info("Migrating pdf_analyses: dropping UNIQUE(pdf_file_id) to preserve analysis history")
        conn.executescript("""
            CREATE TABLE pdf_analyses_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pdf_file_id INTEGER NOT NULL REFERENCES pdf_files(id),
                triage_json TEXT,
                analysis_json TEXT,
                priority TEXT,
                pages_analyzed INTEGER,
                total_pages INTEGER,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                model_used TEXT,
                analysis_duration_seconds REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO pdf_analyses_new
                SELECT id, pdf_file_id, triage_json, analysis_json, priority,
                       pages_analyzed, total_pages, input_tokens_used, output_tokens_used,
                       model_used, analysis_duration_seconds, created_at
                FROM pdf_analyses;
            DROP TABLE pdf_analyses;
            ALTER TABLE pdf_analyses_new RENAME TO pdf_analyses;
            CREATE INDEX IF NOT EXISTS idx_pdf_analyses_file_id ON pdf_analyses(pdf_file_id);
        """)
        conn.commit()

    # daily_reports: drop UNIQUE(report_date, report_type)
    if has_unique("daily_reports", ("report_date", "report_type")):
        log.info("Migrating daily_reports: dropping UNIQUE(report_date, report_type) to preserve pulse history")
        conn.executescript("""
            CREATE TABLE daily_reports_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                report_type TEXT NOT NULL,
                report_json TEXT NOT NULL,
                report_markdown TEXT NOT NULL,
                pdf_count INTEGER,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                discord_sent_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            INSERT INTO daily_reports_new
                SELECT id, report_date, report_type, report_json, report_markdown,
                       pdf_count, input_tokens_used, output_tokens_used,
                       discord_sent_at, created_at
                FROM daily_reports;
            DROP TABLE daily_reports;
            ALTER TABLE daily_reports_new RENAME TO daily_reports;
            CREATE INDEX IF NOT EXISTS idx_daily_reports_type_created ON daily_reports(report_type, created_at);
        """)
        conn.commit()


# --- Dropbox state ---

def get_dropbox_cursor() -> str | None:
    row = get_connection().execute("SELECT cursor FROM dropbox_state WHERE id = 1").fetchone()
    return row["cursor"] if row else None


def update_dropbox_cursor(cursor: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE dropbox_state SET cursor = ?, last_poll_at = ? WHERE id = 1",
        (cursor, datetime.utcnow().isoformat()),
    )
    conn.commit()


# --- PDF files ---

def insert_pdf_file(
    dropbox_path: str,
    file_name: str,
    local_path: str,
    dropbox_rev: str | None = None,
    file_size_bytes: int | None = None,
    dropbox_modified_at: str | None = None,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT OR IGNORE INTO pdf_files
           (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
            dropbox_modified_at, downloaded_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'DOWNLOADED')""",
        (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
         dropbox_modified_at, datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_pdf_by_path(dropbox_path: str) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM pdf_files WHERE dropbox_path = ?", (dropbox_path,)
    ).fetchone()
    return dict(row) if row else None


def get_pending_pdfs(limit: int = 50) -> list[dict]:
    rows = get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'DOWNLOADED'
           ORDER BY file_size_bytes ASC, created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_failed_pdfs_for_retry(max_retries: int = 3) -> list[dict]:
    rows = get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'FAILED' AND retry_count < ?
           ORDER BY created_at ASC""",
        (max_retries,),
    ).fetchall()
    return [dict(r) for r in rows]


def update_pdf_status(pdf_id: int, status: str, error_message: str | None = None) -> None:
    conn = get_connection()
    if error_message:
        conn.execute(
            """UPDATE pdf_files
               SET status = ?, error_message = ?, retry_count = retry_count + 1,
                   updated_at = datetime('now')
               WHERE id = ?""",
            (status, error_message, pdf_id),
        )
    else:
        conn.execute(
            "UPDATE pdf_files SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (status, pdf_id),
        )
    conn.commit()


def update_pdf_priority(pdf_id: int, priority: str) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE pdf_files SET priority = ?, updated_at = datetime('now') WHERE id = ?",
        (priority, pdf_id),
    )
    conn.commit()


def count_pending_queue() -> int:
    """Return how many PDFs are currently in the pending queue."""
    row = get_connection().execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE status IN ('DOWNLOADED', 'PROCESSING')"
    ).fetchone()
    return row["c"] if row else 0


def clear_pending_queue() -> int:
    """Delete all DOWNLOADED/PROCESSING rows from pdf_files and their local files.

    Returns the number of rows deleted. Caller is responsible for any safety checks.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, local_path FROM pdf_files WHERE status IN ('DOWNLOADED', 'PROCESSING')"
    ).fetchall()
    pending_ids = [r["id"] for r in rows]

    if not pending_ids:
        return 0

    # Remove local PDF files from disk
    for r in rows:
        if r["local_path"]:
            try:
                Path(r["local_path"]).unlink(missing_ok=True)
            except Exception:
                pass

    placeholders = ",".join("?" * len(pending_ids))
    conn.execute(f"DELETE FROM processing_log WHERE pdf_file_id IN ({placeholders})", pending_ids)
    conn.execute(f"DELETE FROM pdf_analyses WHERE pdf_file_id IN ({placeholders})", pending_ids)
    conn.execute(f"DELETE FROM pdf_files WHERE id IN ({placeholders})", pending_ids)
    conn.commit()
    return len(pending_ids)


# --- Analyses ---

def insert_analysis(
    pdf_file_id: int,
    triage_json: str | None,
    analysis_json: str | None,
    priority: str,
    pages_analyzed: int,
    total_pages: int,
    input_tokens: int,
    output_tokens: int,
    model: str,
    duration: float,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO pdf_analyses
           (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
            total_pages, input_tokens_used, output_tokens_used, model_used,
            analysis_duration_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
         total_pages, input_tokens, output_tokens, model, duration),
    )
    conn.commit()
    return cur.lastrowid


# Shared subquery: pick the latest analysis row per pdf_file_id.
# Since a single PDF can have multiple analyses (e.g., reprocessed), we always
# want the most recent one by id.
_LATEST_ANALYSIS_CTE = """
    WITH latest_analyses AS (
        SELECT pa.*
        FROM pdf_analyses pa
        WHERE pa.id = (
            SELECT MAX(id) FROM pdf_analyses
            WHERE pdf_file_id = pa.pdf_file_id
        )
    )
"""


def get_todays_analyses(today: str | None = None) -> list[dict]:
    if today is None:
        today = date.today().isoformat()
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE date(la.created_at) = ?
        ORDER BY la.created_at ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_since(since_time: str) -> list[dict]:
    """Get latest analysis per PDF where the PDF was uploaded to Dropbox after since_time."""
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE pf.dropbox_modified_at > ?
        ORDER BY pf.dropbox_modified_at ASC""",
        (since_time,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_between(start_time: str, end_time: str) -> list[dict]:
    """Get latest analysis per PDF where the PDF was uploaded to Dropbox within a window."""
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE pf.dropbox_modified_at >= ? AND pf.dropbox_modified_at <= ?
        ORDER BY pf.dropbox_modified_at ASC""",
        (start_time, end_time),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Daily reports ---

def insert_daily_report(
    report_date: str,
    report_type: str,
    report_json: str,
    report_markdown: str,
    pdf_count: int,
    input_tokens: int,
    output_tokens: int,
) -> int:
    conn = get_connection()
    cur = conn.execute(
        """INSERT INTO daily_reports
           (report_date, report_type, report_json, report_markdown, pdf_count,
            input_tokens_used, output_tokens_used)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (report_date, report_type, report_json, report_markdown, pdf_count,
         input_tokens, output_tokens),
    )
    conn.commit()
    return cur.lastrowid


def mark_report_sent(report_id: int) -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE daily_reports SET discord_sent_at = datetime('now') WHERE id = ?",
        (report_id,),
    )
    conn.commit()


def get_last_report_time() -> str | None:
    """Get the created_at timestamp of the most recent daily report, any date."""
    row = get_connection().execute(
        """SELECT created_at FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return row["created_at"] if row else None


def get_last_daily_pulse() -> dict | None:
    """Get the full last scheduled pulse row (for cross-day context)."""
    row = get_connection().execute(
        """SELECT report_date, created_at, report_markdown, pdf_count
           FROM daily_reports
           WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


# --- Processing log ---

def log_event(pdf_file_id: int | None, event_type: str, status: str, details: str | None = None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO processing_log (pdf_file_id, event_type, status, details) VALUES (?, ?, ?, ?)",
        (pdf_file_id, event_type, status, details),
    )
    conn.commit()


# --- Stats ---

def get_today_stats(today: str | None = None) -> dict:
    if today is None:
        today = date.today().isoformat()
    conn = get_connection()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE date(created_at) = ?", (today,)
    ).fetchone()["c"]

    processed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE date(created_at) = ? AND status = 'PROCESSED'", (today,)
    ).fetchone()["c"]

    pending = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE date(created_at) = ? AND status IN ('DOWNLOADED', 'PROCESSING')", (today,)
    ).fetchone()["c"]

    failed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE date(created_at) = ? AND status = 'FAILED'", (today,)
    ).fetchone()["c"]

    # Sum tokens across ALL analysis attempts today (re-runs cost real tokens,
    # so counting them is correct for observability).
    tokens = conn.execute(
        """SELECT COALESCE(SUM(input_tokens_used), 0) as input_t,
                  COALESCE(SUM(output_tokens_used), 0) as output_t
           FROM pdf_analyses WHERE date(created_at) = ?""",
        (today,),
    ).fetchone()

    last_report = conn.execute(
        "SELECT report_type, discord_sent_at FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC LIMIT 1",
        (today,),
    ).fetchone()

    return {
        "total": total,
        "processed": processed,
        "pending": pending,
        "failed": failed,
        "input_tokens": tokens["input_t"],
        "output_tokens": tokens["output_t"],
        "last_report_type": last_report["report_type"] if last_report else None,
        "last_report_sent": last_report["discord_sent_at"] if last_report else None,
    }


def get_pipeline_stats() -> dict:
    """Full-picture stats across the whole DB, not just today."""
    conn = get_connection()

    total = conn.execute("SELECT COUNT(*) as c FROM pdf_files").fetchone()["c"]

    # Status breakdown
    status_rows = conn.execute(
        "SELECT status, COUNT(*) as c FROM pdf_files GROUP BY status"
    ).fetchall()
    status_counts = {r["status"]: r["c"] for r in status_rows}

    # Priority breakdown: count each PDF once (latest analysis wins if multiple exist)
    priority_rows = conn.execute(
        """SELECT priority, COUNT(*) as c FROM (
             SELECT priority FROM pdf_analyses pa
             WHERE pa.id = (SELECT MAX(id) FROM pdf_analyses WHERE pdf_file_id = pa.pdf_file_id)
           ) GROUP BY priority"""
    ).fetchall()
    priority_counts = {r["priority"]: r["c"] for r in priority_rows}

    # Upload date range (earliest/latest PDF upload in DB)
    date_range = conn.execute(
        """SELECT MIN(dropbox_modified_at) as earliest,
                  MAX(dropbox_modified_at) as latest
           FROM pdf_files WHERE dropbox_modified_at IS NOT NULL"""
    ).fetchone()

    # Token totals (all-time)
    tokens = conn.execute(
        """SELECT COALESCE(SUM(input_tokens_used), 0) as input_t,
                  COALESCE(SUM(output_tokens_used), 0) as output_t
           FROM pdf_analyses"""
    ).fetchone()

    # Last scheduled pulse
    last_daily = conn.execute(
        """SELECT created_at, discord_sent_at, pdf_count
           FROM daily_reports WHERE report_type = 'daily'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    # Last manual pulse
    last_manual = conn.execute(
        """SELECT created_at, discord_sent_at, pdf_count
           FROM daily_reports WHERE report_type = 'manual'
           ORDER BY created_at DESC LIMIT 1"""
    ).fetchone()

    # Dropbox cursor state
    cursor_row = conn.execute("SELECT cursor, last_poll_at FROM dropbox_state WHERE id = 1").fetchone()

    # Last 5 PDFs ingested (by created_at desc)
    recent_rows = conn.execute(
        """SELECT file_name, priority, status, created_at
           FROM pdf_files
           ORDER BY created_at DESC
           LIMIT 5"""
    ).fetchall()
    recent_pdfs = [dict(r) for r in recent_rows]

    # Count uploads by Dropbox upload time (server_modified) — what actually
    # would go into a pulse, regardless of when we ingested them.
    cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    uploads_last_24h = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE dropbox_modified_at > ?",
        (cutoff_24h,),
    ).fetchone()["c"]

    uploads_since_last_scheduled = None
    if last_daily and last_daily["created_at"]:
        # Convert the created_at (already UTC ISO) into the cutoff
        row = conn.execute(
            "SELECT COUNT(*) as c FROM pdf_files WHERE dropbox_modified_at > ?",
            (last_daily["created_at"],),
        ).fetchone()
        uploads_since_last_scheduled = row["c"] if row else 0

    return {
        "total_pdfs": total,
        "status_counts": status_counts,
        "priority_counts": priority_counts,
        "earliest_upload": date_range["earliest"] if date_range else None,
        "latest_upload": date_range["latest"] if date_range else None,
        "input_tokens": tokens["input_t"],
        "output_tokens": tokens["output_t"],
        "last_daily_pulse": dict(last_daily) if last_daily else None,
        "last_manual_pulse": dict(last_manual) if last_manual else None,
        "cursor_set": bool(cursor_row and cursor_row["cursor"]),
        "last_poll_at": cursor_row["last_poll_at"] if cursor_row else None,
        "recent_pdfs": recent_pdfs,
        "uploads_last_24h": uploads_last_24h,
        "uploads_since_last_scheduled": uploads_since_last_scheduled,
    }
