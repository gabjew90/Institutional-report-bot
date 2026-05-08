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

        -- Single-row state for the real-time ingestion-feed Discord poster.
        -- Tracks the highest pdf_file_id we've already announced so we don't
        -- repost. id = 1 always (the row is upserted in place).
        CREATE TABLE IF NOT EXISTS ingest_feed_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_announced_pdf_file_id INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        INSERT OR IGNORE INTO ingest_feed_state (id, last_announced_pdf_file_id) VALUES (1, 0);

        -- Per-PDF state machine for the parallel Opus routine ingestion bridge.
        -- Only HIGH-priority PDFs land here when settings.high_ingestion_backend
        -- is set to "opus_bridge". Status values:
        --   pending             — queued, not yet committed to the bridge branch
        --   committed           — PDF + sidecar pushed to ingest-pending/ on the bridge
        --   completed           — Opus routine finished; result pulled into pdf_analyses
        --   fallback_to_gemini  — bridge stalled / failed / oversized → Gemini ran instead
        --   failed              — both bridge and Gemini fallback failed
        CREATE TABLE IF NOT EXISTS bridge_ingestion_state (
            pdf_file_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            queued_at TEXT NOT NULL DEFAULT (datetime('now')),
            committed_at TEXT,
            completed_at TEXT,
            bridge_filename TEXT,
            error_message TEXT,
            fallback_reason TEXT,
            FOREIGN KEY (pdf_file_id) REFERENCES pdf_files(id)
        );
        CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_ingestion_state(status);
        CREATE INDEX IF NOT EXISTS idx_bridge_queued_at ON bridge_ingestion_state(queued_at);
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


def _normalize_ts(ts: str | None) -> str | None:
    """Normalize a timestamp string to ISO 'T' format for cross-format comparison.

    SQLite's datetime('now') produces 'YYYY-MM-DD HH:MM:SS' (space separator).
    Python's datetime.isoformat() produces 'YYYY-MM-DDTHH:MM:SS' (T separator).
    Lexicographic TEXT comparison treats these differently (T > space), which
    breaks any cutoff query that mixes the two formats.

    Parse with fromisoformat (handles both) and re-emit as T-format so comparisons
    line up with dropbox_modified_at (which is always T-format).
    """
    if not ts:
        return ts
    try:
        return datetime.fromisoformat(ts.replace(" ", "T", 1)).isoformat()
    except (ValueError, TypeError):
        return ts


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
    since_time = _normalize_ts(since_time)
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
    start_time = _normalize_ts(start_time)
    end_time = _normalize_ts(end_time)
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
    # Explicit ISO 'T'-format created_at so it matches dropbox_modified_at
    # format for lexicographic comparison in later queries.
    created_at = datetime.utcnow().isoformat()
    cur = conn.execute(
        """INSERT INTO daily_reports
           (report_date, report_type, report_json, report_markdown, pdf_count,
            input_tokens_used, output_tokens_used, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (report_date, report_type, report_json, report_markdown, pdf_count,
         input_tokens, output_tokens, created_at),
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


def get_ingest_feed_last_announced() -> int:
    """Highest pdf_file_id we've already posted to the ingestion feed."""
    row = get_connection().execute(
        "SELECT last_announced_pdf_file_id FROM ingest_feed_state WHERE id = 1"
    ).fetchone()
    return int(row["last_announced_pdf_file_id"]) if row else 0


def set_ingest_feed_last_announced(pdf_file_id: int) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE ingest_feed_state
           SET last_announced_pdf_file_id = ?, updated_at = datetime('now')
           WHERE id = 1""",
        (int(pdf_file_id),),
    )
    conn.commit()


def get_next_pdf_to_announce(after_pdf_file_id: int) -> dict | None:
    """Next analyzed HIGH/MEDIUM PDF whose pdf_file_id > after_pdf_file_id.

    Joins pdf_files with the LATEST pdf_analyses row per file (so reanalyses
    are deduped). Skips LOW priority. Returns dict or None if none pending.
    """
    row = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.id AS pf_id, pf.file_name, pf.dropbox_path,
               pf.dropbox_modified_at, pf.file_size_bytes
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')
          AND pf.id > ?
        ORDER BY pf.id ASC
        LIMIT 1""",
        (int(after_pdf_file_id),),
    ).fetchone()
    return dict(row) if row else None


def count_pending_announcements(after_pdf_file_id: int) -> dict:
    """Count HIGH/MEDIUM PDFs queued for announcement after the given id.

    Used at startup to decide whether to trickle or post a backfill summary.
    Returns {'high': N, 'medium': M, 'total': N+M}.
    """
    rows = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT la.priority, COUNT(*) as n
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')
          AND pf.id > ?
        GROUP BY la.priority""",
        (int(after_pdf_file_id),),
    ).fetchall()
    out = {"high": 0, "medium": 0, "total": 0}
    for r in rows:
        out[r["priority"]] = int(r["n"])
        out["total"] += int(r["n"])
    return out


def max_announceable_pdf_file_id() -> int:
    """Current MAX(pdf_file_id) among HIGH/MEDIUM analyzed PDFs.

    Used at startup to fast-forward last_announced past a backfilled batch.
    """
    row = get_connection().execute(
        _LATEST_ANALYSIS_CTE + """
        SELECT COALESCE(MAX(pf.id), 0) AS m
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')"""
    ).fetchone()
    return int(row["m"]) if row else 0


# ---------------------------------------------------------------------------
# Opus-bridge HIGH ingestion state machine
# ---------------------------------------------------------------------------

def queue_for_opus_bridge(pdf_file_id: int) -> None:
    """Insert (or reset) a bridge-state row for a HIGH PDF.

    Idempotent: if a row already exists for this pdf_file_id (e.g. from a
    prior reanalyze that fell back to Gemini), it's reset to status='pending'
    with a fresh queued_at.
    """
    get_connection().execute(
        """INSERT INTO bridge_ingestion_state
             (pdf_file_id, status, queued_at)
           VALUES (?, 'pending', datetime('now'))
           ON CONFLICT(pdf_file_id) DO UPDATE SET
             status = 'pending',
             queued_at = datetime('now'),
             committed_at = NULL,
             completed_at = NULL,
             bridge_filename = NULL,
             error_message = NULL,
             fallback_reason = NULL""",
        (int(pdf_file_id),),
    )
    get_connection().commit()


def get_bridge_state(pdf_file_id: int) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM bridge_ingestion_state WHERE pdf_file_id = ?",
        (int(pdf_file_id),),
    ).fetchone()
    return dict(row) if row else None


def update_bridge_status(
    pdf_file_id: int,
    status: str,
    bridge_filename: str | None = None,
    error_message: str | None = None,
    fallback_reason: str | None = None,
) -> None:
    """Update a bridge state row. Auto-stamps committed_at/completed_at by status."""
    conn = get_connection()
    now = "datetime('now')"
    sets = ["status = ?"]
    params: list = [status]
    if status == "committed":
        sets.append(f"committed_at = {now}")
    if status in ("completed", "fallback_to_gemini", "failed"):
        sets.append(f"completed_at = {now}")
    if bridge_filename is not None:
        sets.append("bridge_filename = ?")
        params.append(bridge_filename)
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message[:500])
    if fallback_reason is not None:
        sets.append("fallback_reason = ?")
        params.append(fallback_reason[:200])
    params.append(int(pdf_file_id))
    conn.execute(
        f"UPDATE bridge_ingestion_state SET {', '.join(sets)} WHERE pdf_file_id = ?",
        params,
    )
    conn.commit()


def get_pending_bridge_pdfs(limit: int = 50) -> list[dict]:
    """Bridge-state rows in 'pending' status — not yet committed to GitHub."""
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path, pf.local_path,
                  pf.dropbox_modified_at, pf.file_size_bytes
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'pending'
           ORDER BY b.queued_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_committed_bridge_pdfs() -> list[dict]:
    """Rows committed to the bridge but not yet processed by the Opus routine.

    Used by the pull job (to check for completed results) and by the
    timeout watchdog (to fall back to Gemini after opus_bridge_timeout_minutes).
    """
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'committed'
           ORDER BY b.committed_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def count_bridge_outcomes_since(cutoff_iso: str) -> dict:
    """Count bridge-ingestion outcomes since a given ISO timestamp.

    Used by /status to surface bridge health (attempts, completed via Opus,
    fell back to Gemini, hard failures).
    """
    rows = get_connection().execute(
        """SELECT status, COUNT(*) AS n
           FROM bridge_ingestion_state
           WHERE queued_at > ?
           GROUP BY status""",
        (cutoff_iso,),
    ).fetchall()
    out = {
        "attempted": 0,
        "pending": 0,
        "committed": 0,
        "completed": 0,
        "fallback_to_gemini": 0,
        "failed": 0,
    }
    for r in rows:
        s = r["status"]
        n = int(r["n"])
        if s in out:
            out[s] = n
        out["attempted"] += n
    return out


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
        # Normalize the cutoff to match dropbox_modified_at's ISO 'T' format.
        # SQLite's datetime('now') uses space separator which lexically compares
        # less than 'T'-format strings, inflating counts without normalization.
        cutoff_pulse = _normalize_ts(last_daily["created_at"])
        row = conn.execute(
            "SELECT COUNT(*) as c FROM pdf_files WHERE dropbox_modified_at > ?",
            (cutoff_pulse,),
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
