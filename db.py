"""SQLite database setup and query helpers."""

import json
import sqlite3
from datetime import datetime, date
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
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(pdf_file_id)
        );

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
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(report_date, report_type)
        );

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
        """INSERT OR REPLACE INTO pdf_analyses
           (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
            total_pages, input_tokens_used, output_tokens_used, model_used,
            analysis_duration_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
         total_pages, input_tokens, output_tokens, model, duration),
    )
    conn.commit()
    return cur.lastrowid


def get_todays_analyses(today: str | None = None) -> list[dict]:
    if today is None:
        today = date.today().isoformat()
    rows = get_connection().execute(
        """SELECT pa.*, pf.file_name, pf.dropbox_path
           FROM pdf_analyses pa
           JOIN pdf_files pf ON pa.pdf_file_id = pf.id
           WHERE date(pa.created_at) = ?
           ORDER BY pa.created_at ASC""",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_since(since_time: str) -> list[dict]:
    """Get analyses where the PDF was uploaded to Dropbox after since_time."""
    rows = get_connection().execute(
        """SELECT pa.*, pf.file_name, pf.dropbox_path
           FROM pdf_analyses pa
           JOIN pdf_files pf ON pa.pdf_file_id = pf.id
           WHERE pf.dropbox_modified_at > ?
           ORDER BY pf.dropbox_modified_at ASC""",
        (since_time,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_analyses_between(start_time: str, end_time: str) -> list[dict]:
    """Get analyses where the PDF was uploaded to Dropbox within a specific time window."""
    rows = get_connection().execute(
        """SELECT pa.*, pf.file_name, pf.dropbox_path
           FROM pdf_analyses pa
           JOIN pdf_files pf ON pa.pdf_file_id = pf.id
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
        """INSERT OR REPLACE INTO daily_reports
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

    # Priority breakdown of analyses
    priority_rows = conn.execute(
        "SELECT priority, COUNT(*) as c FROM pdf_analyses GROUP BY priority"
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
    }
