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
            bridge_commit_sha TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            fallback_reason TEXT,
            FOREIGN KEY (pdf_file_id) REFERENCES pdf_files(id)
        );
        CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_ingestion_state(status);
        CREATE INDEX IF NOT EXISTS idx_bridge_queued_at ON bridge_ingestion_state(queued_at);

        -- Persistent /reanalyze job state. The Discord interaction handler
        -- previously ran reanalyze synchronously, which capped at Discord's
        -- 15-min interaction limit and lost all state on Railway redeploy.
        -- This table moves the job to a scheduler-driven background path
        -- with DB-persisted progress, so a worker restart resumes where
        -- it left off and large reanalyzes run to completion regardless
        -- of Discord interaction expiry.
        CREATE TABLE IF NOT EXISTS reanalyze_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,  -- pending | processing | complete | failed | cancelled
            hours INTEGER NOT NULL,
            priority_filter TEXT,  -- JSON array of priority strings, or NULL for all
            target_pdf_ids TEXT NOT NULL,  -- JSON array of pdf_files.id
            target_count INTEGER NOT NULL,
            processed_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            failed_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            bridge_queued_pdf_ids TEXT NOT NULL DEFAULT '[]',  -- JSON
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            requested_by TEXT,           -- Discord user id who triggered
            discord_channel_id INTEGER,  -- Channel where /reanalyze was issued
            discord_status_message_id INTEGER,  -- Message to update with progress
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            completed_at TEXT,
            last_progress_at TEXT,
            error_message TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_reanalyze_status ON reanalyze_jobs(status);
        CREATE INDEX IF NOT EXISTS idx_reanalyze_created ON reanalyze_jobs(created_at);

        -- Per-user audit log for /ask + @mention Perplexity queries. Used to
        -- enforce a daily-per-user cap. Reset is "since UTC midnight" — see
        -- count_ask_queries_today_for_user.
        CREATE TABLE IF NOT EXISTS ask_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asked_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_ask_queries_user_time ON ask_queries(user_id, asked_at);

        -- Analyst trade log. One row per image attachment posted in the
        -- analyst alerts channel. Populated by analyst_log.watcher via
        -- Gemini vision. is_trade=0 rows are non-trade images (memes,
        -- tweets) — recorded for dedup so we don't re-OCR them.
        -- See analyst_log/ocr.py for the field semantics.
        CREATE TABLE IF NOT EXISTS analyst_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER NOT NULL,
            discord_attachment_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            image_url TEXT,
            caption TEXT,
            is_trade INTEGER NOT NULL DEFAULT 0,
            ticker TEXT,
            contract_type TEXT,
            strike REAL,
            expiry TEXT,           -- YYYY-MM-DD
            action TEXT,           -- open | add | trim | close | viewing | unclear
            gain_pct REAL,
            inferred_status TEXT,  -- e.g. 'expired_unknown' (set by daily cron)
            gemini_json TEXT,      -- raw OCR JSON for forensics
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(discord_message_id, discord_attachment_id)
        );
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_posted ON analyst_trades(posted_at);
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_expiry ON analyst_trades(expiry);
        CREATE INDEX IF NOT EXISTS idx_analyst_trades_ticker ON analyst_trades(ticker);
    """)
    # Idempotent migrations for already-deployed bridge_ingestion_state schemas
    # (the table was first created in step 1 without these columns).
    for col, ddl in [
        ("bridge_commit_sha", "ALTER TABLE bridge_ingestion_state ADD COLUMN bridge_commit_sha TEXT"),
        ("attempt_count", "ALTER TABLE bridge_ingestion_state ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0"),
    ]:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            # "duplicate column name" = already migrated, ignore
            if "duplicate column" not in str(e).lower():
                raise
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
    bridge_commit_sha: str | None = None,
    error_message: str | None = None,
    fallback_reason: str | None = None,
    increment_attempt: bool = False,
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
    if bridge_commit_sha is not None:
        sets.append("bridge_commit_sha = ?")
        params.append(bridge_commit_sha)
    if error_message is not None:
        sets.append("error_message = ?")
        params.append(error_message[:500])
    if fallback_reason is not None:
        sets.append("fallback_reason = ?")
        params.append(fallback_reason[:200])
    if increment_attempt:
        sets.append("attempt_count = attempt_count + 1")
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


def get_committed_bridge_pdfs(limit: int = 100) -> list[dict]:
    """Rows committed to the bridge but not yet processed by the Opus routine.

    Used by the pull job (to check for completed results) and by the
    timeout watchdog (to fall back to Gemini after opus_bridge_timeout_minutes).
    """
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'committed'
           ORDER BY b.committed_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def get_fallback_bridge_pdfs(limit: int = 100) -> list[dict]:
    """Rows marked fallback_to_gemini but not yet completed via Gemini.

    Used by the fallback sweeper to actually run Gemini deep-analysis on
    PDFs that hit guardrails or routine failures. After Gemini succeeds,
    the row is updated to status='completed'.
    """
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path, pf.local_path,
                  pf.dropbox_modified_at, pf.file_size_bytes
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'fallback_to_gemini'
           ORDER BY b.queued_at ASC
           LIMIT ?""",
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]


def find_timed_out_bridge_committed(timeout_iso: str, limit: int = 100) -> list[dict]:
    """Rows committed to the bridge before `timeout_iso` that haven't
    progressed to 'completed' or 'fallback_to_gemini' or 'failed'.

    These are PDFs the routine apparently never got to (or whose result
    wasn't pulled). The watchdog converts them to fallback_to_gemini so
    the sweeper can run Gemini and produce a pdf_analyses row.

    `timeout_iso` should be normalized to T-format before passing
    (datetime('now') uses space separator — _normalize_ts handles).
    """
    cutoff = _normalize_ts(timeout_iso) or timeout_iso
    rows = get_connection().execute(
        """SELECT b.*, pf.file_name, pf.dropbox_path
           FROM bridge_ingestion_state b
           JOIN pdf_files pf ON pf.id = b.pdf_file_id
           WHERE b.status = 'committed'
             AND b.committed_at IS NOT NULL
             AND b.committed_at < ?
           ORDER BY b.committed_at ASC
           LIMIT ?""",
        (cutoff, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def count_bridge_outcomes_since(cutoff_iso: str) -> dict:
    """Count bridge-ingestion outcomes since a given ISO timestamp.

    Used by /status to surface bridge health (attempted, completed via Opus,
    fell back to Gemini, hard failures).

    Normalizes the cutoff to T-format so it lexically compares correctly
    against queued_at (which is written by SQLite's datetime('now') in
    space-separator format). See _normalize_ts() docstring for the full
    landmine writeup.
    """
    cutoff = _normalize_ts(cutoff_iso) or cutoff_iso
    rows = get_connection().execute(
        """SELECT status, COUNT(*) AS n
           FROM bridge_ingestion_state
           WHERE queued_at > ?
           GROUP BY status""",
        (cutoff,),
    ).fetchall()
    out = {
        "total": 0,
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
        out["total"] += n
    return out


# =============================================================================
# Reanalyze job state (persistent background-processing for /reanalyze).
# =============================================================================


def create_reanalyze_job(
    hours: int,
    target_pdf_ids: list[int],
    priority_filter: list[str] | None,
    requested_by: str | None = None,
    discord_channel_id: int | None = None,
    discord_status_message_id: int | None = None,
) -> int:
    """Insert a new reanalyze job in `pending` state. Returns the job id."""
    import json as _json
    cur = get_connection().execute(
        """INSERT INTO reanalyze_jobs
           (status, hours, priority_filter, target_pdf_ids, target_count,
            requested_by, discord_channel_id, discord_status_message_id)
           VALUES ('pending', ?, ?, ?, ?, ?, ?, ?)""",
        (
            hours,
            _json.dumps(priority_filter) if priority_filter else None,
            _json.dumps(target_pdf_ids),
            len(target_pdf_ids),
            requested_by,
            discord_channel_id,
            discord_status_message_id,
        ),
    )
    get_connection().commit()
    return cur.lastrowid


def get_active_reanalyze_job() -> dict | None:
    """Return the next reanalyze job to work on, or None if nothing active.

    Order: 'processing' first (resume in-flight after a worker restart),
    then 'pending' by oldest. One job at a time — the scheduler processes
    them serially.
    """
    row = get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           WHERE status IN ('processing', 'pending')
           ORDER BY (status = 'processing') DESC, created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_reanalyze_job(job_id: int) -> dict | None:
    row = get_connection().execute(
        "SELECT * FROM reanalyze_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_recent_reanalyze_jobs(limit: int = 5) -> list[dict]:
    """Return the most recent reanalyze jobs (any status), newest first.

    Used by /status to show the active or recently-completed jobs.
    """
    rows = get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def start_reanalyze_job(job_id: int) -> None:
    """Mark a pending job as processing and record started_at."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'processing', started_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    get_connection().commit()


def update_reanalyze_job_progress(
    job_id: int,
    *,
    processed_pdf_ids: list[int] | None = None,
    failed_pdf_ids: list[int] | None = None,
    bridge_queued_pdf_ids: list[int] | None = None,
    input_tokens_delta: int = 0,
    output_tokens_delta: int = 0,
) -> None:
    """Update job progress lists + token totals. Each list parameter, if
    given, REPLACES the column value (the caller passes the full list).
    Token deltas are added incrementally.
    """
    import json as _json
    sets: list[str] = ["last_progress_at = datetime('now')"]
    args: list = []
    if processed_pdf_ids is not None:
        sets.append("processed_pdf_ids = ?")
        args.append(_json.dumps(processed_pdf_ids))
    if failed_pdf_ids is not None:
        sets.append("failed_pdf_ids = ?")
        args.append(_json.dumps(failed_pdf_ids))
    if bridge_queued_pdf_ids is not None:
        sets.append("bridge_queued_pdf_ids = ?")
        args.append(_json.dumps(bridge_queued_pdf_ids))
    if input_tokens_delta:
        sets.append("input_tokens = input_tokens + ?")
        args.append(input_tokens_delta)
    if output_tokens_delta:
        sets.append("output_tokens = output_tokens + ?")
        args.append(output_tokens_delta)
    args.append(job_id)
    get_connection().execute(
        f"UPDATE reanalyze_jobs SET {', '.join(sets)} WHERE id = ?",
        tuple(args),
    )
    get_connection().commit()


def complete_reanalyze_job(job_id: int) -> None:
    """Mark a job as complete and record completed_at."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'complete', completed_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    get_connection().commit()


def fail_reanalyze_job(job_id: int, error_message: str) -> None:
    """Mark a job as failed with an error message."""
    get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'failed', completed_at = datetime('now'),
               error_message = ?
           WHERE id = ?""",
        (error_message[:500], job_id),
    )
    get_connection().commit()


# =============================================================================
# Daily pulse / synthesis history.
# =============================================================================


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

def _today_utc_range() -> tuple[str, str, str]:
    """Compute the UTC range corresponding to "today" in the configured
    display timezone (settings.timezone, default America/New_York).

    The bug we're fixing: previously this used `date.today()` which is
    SERVER-LOCAL. On Railway (UTC), `date.today()` advances at UTC
    midnight. Stats labeled "Today" would suddenly drop to 0 when UTC
    flipped over even though it's still mid-evening for the actual user.
    Fix: compute today's date in the user's configured timezone, then
    convert that local-day's [00:00, 24:00) window to UTC for the
    pdf_files.created_at comparison (which is stored as UTC).

    Returns (utc_start_str, utc_end_str, local_date_iso) — the first two
    in space-separator format that matches SQLite's `datetime('now')`,
    the third for the daily_reports.report_date lookup which is stored
    as a calendar date string in the user's local sense.
    """
    import pytz
    from config import settings
    try:
        tz = pytz.timezone(settings.timezone)
    except Exception:
        tz = pytz.UTC
    now_local = datetime.now(tz)
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    local_tomorrow = local_midnight + timedelta(days=1)
    utc_start = local_midnight.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    utc_end = local_tomorrow.astimezone(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S")
    return utc_start, utc_end, now_local.date().isoformat()


def get_today_stats(today: str | None = None) -> dict:
    """Stats for "today" in the configured display timezone.

    `today` parameter is now ignored when the timezone-aware path is
    available (it's kept for caller-API compatibility but the actual
    comparison uses a UTC range computed from the configured TZ).
    Pass-through of a literal date string is no longer meaningful since
    the comparison is now range-based.
    """
    utc_start, utc_end, local_date = _today_utc_range()
    conn = get_connection()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ?",
        (utc_start, utc_end),
    ).fetchone()["c"]

    processed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status = 'PROCESSED'",
        (utc_start, utc_end),
    ).fetchone()["c"]

    pending = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status IN ('DOWNLOADED', 'PROCESSING')",
        (utc_start, utc_end),
    ).fetchone()["c"]

    failed = conn.execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE created_at >= ? AND created_at < ? AND status = 'FAILED'",
        (utc_start, utc_end),
    ).fetchone()["c"]

    # Sum tokens across ALL analysis attempts today (re-runs cost real tokens,
    # so counting them is correct for observability).
    tokens = conn.execute(
        """SELECT COALESCE(SUM(input_tokens_used), 0) as input_t,
                  COALESCE(SUM(output_tokens_used), 0) as output_t
           FROM pdf_analyses WHERE created_at >= ? AND created_at < ?""",
        (utc_start, utc_end),
    ).fetchone()

    # Daily reports use `report_date` (calendar date string) — match the
    # local calendar date. Both routine-pulse and Gemini-pulse paths
    # store report_date in the same local-day sense.
    last_report = conn.execute(
        "SELECT report_type, discord_sent_at FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC LIMIT 1",
        (local_date,),
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

    # Last scheduled pulse — explicitly EXCLUDE test fires.
    # Test fires are inserted with report_type='daily' (so the bridge
    # worker's flow stays uniform) but they carry a `target_channels`
    # frontmatter value that lands in report_json. Filter those out so
    # /status's "since last scheduled pulse" counter only resets at the
    # actual 13:08 UTC weekday cron firing, not at every test pulse.
    last_daily = conn.execute(
        """SELECT created_at, discord_sent_at, pdf_count
           FROM daily_reports
           WHERE report_type = 'daily'
             AND (
               report_json IS NULL
               OR json_extract(report_json, '$.target_channels') IS NULL
             )
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


# =============================================================================
# /ask Perplexity rate-limit log.
# =============================================================================


def count_ask_queries_today_for_user(user_id: int) -> int:
    """Count this user's /ask queries since UTC midnight."""
    today_utc_midnight = datetime.utcnow().strftime("%Y-%m-%d 00:00:00")
    row = get_connection().execute(
        "SELECT COUNT(*) AS c FROM ask_queries WHERE user_id = ? AND asked_at >= ?",
        (int(user_id), today_utc_midnight),
    ).fetchone()
    return int(row["c"]) if row else 0


def record_ask_query(user_id: int) -> None:
    conn = get_connection()
    conn.execute("INSERT INTO ask_queries (user_id) VALUES (?)", (int(user_id),))
    conn.commit()


# =============================================================================
# Analyst trade log (populated by analyst_log.watcher).
# =============================================================================


def analyst_trade_exists(discord_message_id: int, discord_attachment_id: int) -> bool:
    """Dedup check before OCR'ing an image we've already processed."""
    row = get_connection().execute(
        "SELECT 1 FROM analyst_trades WHERE discord_message_id = ? "
        "AND discord_attachment_id = ?",
        (int(discord_message_id), int(discord_attachment_id)),
    ).fetchone()
    return row is not None


def record_analyst_trade(
    *,
    discord_message_id: int,
    discord_attachment_id: int,
    author: str,
    posted_at: str,
    image_url: str | None,
    caption: str | None,
    is_trade: bool,
    gemini_json: dict | None,
    ticker: str | None = None,
    contract_type: str | None = None,
    strike: float | None = None,
    expiry: str | None = None,
    action: str | None = None,
    gain_pct: float | None = None,
) -> None:
    """Insert (or ignore on dedup) an analyst-trade row. Non-trade images
    are stored with is_trade=0 so we don't re-OCR them on bot restart but
    they don't pollute trade queries.
    """
    import json as _json
    conn = get_connection()
    conn.execute(
        """INSERT OR IGNORE INTO analyst_trades
           (discord_message_id, discord_attachment_id, author, posted_at,
            image_url, caption, is_trade, ticker, contract_type, strike,
            expiry, action, gain_pct, gemini_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            int(discord_message_id),
            int(discord_attachment_id),
            author,
            posted_at,
            image_url,
            caption,
            1 if is_trade else 0,
            ticker,
            contract_type,
            strike,
            expiry,
            action,
            gain_pct,
            _json.dumps(gemini_json) if gemini_json is not None else None,
        ),
    )
    conn.commit()


def get_recent_analyst_trades(hours: int = 24, limit: int = 50) -> list[dict]:
    """Recent trade-tagged rows (is_trade=1) ordered newest first."""
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    rows = get_connection().execute(
        """SELECT * FROM analyst_trades
           WHERE is_trade = 1 AND posted_at > ?
           ORDER BY posted_at DESC
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_current_analyst_positions() -> list[dict]:
    """Currently-open positions: opens minus closes minus trims > 0, expiry
    in the future, and inferred_status not set to expired_unknown.
    """
    rows = get_connection().execute(
        """WITH net AS (
            SELECT ticker, contract_type, strike, expiry,
                   SUM(CASE action
                       WHEN 'open' THEN 1
                       WHEN 'add'  THEN 1
                       WHEN 'close' THEN -1
                       WHEN 'trim'  THEN -1
                       ELSE 0 END) AS net_qty,
                   MAX(posted_at) AS last_activity,
                   MAX(gain_pct) AS last_gain_pct,
                   MIN(posted_at) AS first_alert
            FROM analyst_trades
            WHERE is_trade = 1
              AND action IN ('open', 'add', 'close', 'trim')
              AND (inferred_status IS NULL OR inferred_status != 'expired_unknown')
              AND ticker IS NOT NULL
              AND expiry IS NOT NULL
              AND date(expiry) >= date('now')
            GROUP BY ticker, contract_type, strike, expiry
        )
        SELECT * FROM net WHERE net_qty > 0
        ORDER BY last_activity DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def format_analyst_trades_for_context(hours: int = 168, limit: int = 30) -> str:
    """Render the last N hours of trade-tagged rows as a context block for /ask.

    Intentionally OMITS captions and notes — we don't want the bot to quote
    Abe verbatim. The bot gets ticker/strike/expiry/action/gain only, and
    must paraphrase if the user asks "what did he say."

    Returns "" when there are no trade rows in the window — caller can omit
    the block entirely.
    """
    rows = get_recent_analyst_trades(hours=hours, limit=limit)
    if not rows:
        return ""

    out_lines: list[str] = [
        f"ABE'S RECENT TRADES (last {hours // 24} days, auto-logged from his "
        f"alerts channel — for context only, don't quote captions; he didn't "
        f"share them with you):"
    ]
    # Newest first per get_recent_analyst_trades; reverse so the trader
    # reads them chronologically.
    for r in reversed(rows):
        ticker = r.get("ticker") or "?"
        ct = (r.get("contract_type") or "").lower()
        ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
        strike = r.get("strike")
        strike_str = (
            f"{int(strike) if strike == int(strike) else strike}"
            if strike is not None else "?"
        )
        expiry = r.get("expiry") or ""
        exp_short = expiry[5:] if len(expiry) >= 10 else expiry  # MM-DD
        action = (r.get("action") or "?").lower()
        gain = r.get("gain_pct")
        gain_str = f" ({gain:+.1f}%)" if gain is not None else ""
        posted_at = (r.get("posted_at") or "")[:16].replace("T", " ")

        # Surface expiry status so the bot doesn't claim phantom holdings
        # on contracts past their expiry without a close alert.
        expired_tag = ""
        if r.get("inferred_status") == "expired_unknown":
            if action in ("open", "add"):
                expired_tag = " [expired — no close alert]"
            else:
                expired_tag = " [expired]"

        out_lines.append(
            f"- {posted_at} — {action} {ticker} "
            f"{strike_str}{ct_suffix} {exp_short}{gain_str}{expired_tag}"
        )

    # Also surface currently-open positions explicitly so the bot doesn't
    # have to compute the net itself.
    positions = get_current_analyst_positions()
    if positions:
        out_lines.append("")
        out_lines.append("ABE'S CURRENTLY OPEN POSITIONS (computed from the above log):")
        for p in positions[:10]:
            ticker = p.get("ticker") or "?"
            ct = (p.get("contract_type") or "").lower()
            ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
            strike = p.get("strike")
            strike_str = (
                f"{int(strike) if strike == int(strike) else strike}"
                if strike is not None else "?"
            )
            expiry = p.get("expiry") or ""
            exp_short = expiry[5:] if len(expiry) >= 10 else expiry
            last_gain = p.get("last_gain_pct")
            gain_str = f" — last update {last_gain:+.1f}%" if last_gain is not None else ""
            out_lines.append(
                f"- {ticker} {strike_str}{ct_suffix} {exp_short}{gain_str}"
            )

    return "\n".join(out_lines)


def purge_old_expired_analyst_trades(days_after_expiry: int = 14) -> list[dict]:
    """Hard-delete trade rows whose expiry was more than `days_after_expiry`
    days ago AND have been marked expired_unknown. Called by the weekly
    cron. Returns the rows that were deleted so the cron can announce them.

    Two-stage cleanup keeps the DB bounded:
    1. Daily auto-expire marks past-expiry rows as 'expired_unknown'.
    2. This weekly purge deletes rows that have been expired AND past their
       retention window.

    Set days_after_expiry=0 to disable (returns immediately).
    """
    if days_after_expiry <= 0:
        return []
    conn = get_connection()
    targets = conn.execute(
        f"""SELECT id, ticker, contract_type, strike, expiry, action,
                   gain_pct, posted_at
            FROM analyst_trades
            WHERE expiry IS NOT NULL
              AND inferred_status = 'expired_unknown'
              AND date(expiry, '+{int(days_after_expiry)} days') < date('now')"""
    ).fetchall()
    if not targets:
        return []
    ids = [r["id"] for r in targets]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM analyst_trades WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return [dict(r) for r in targets]


def mark_expired_analyst_positions() -> list[dict]:
    """Daily cron entrypoint. Mark any trade rows whose expiry has passed
    AND have no inferred_status yet as 'expired_unknown'. Returns the rows
    that were newly marked, so the cron can announce them.
    """
    conn = get_connection()
    # Collect what's about to change so we can return it for the announce
    targets = conn.execute(
        """SELECT id, ticker, contract_type, strike, expiry, action,
                  gain_pct, posted_at
           FROM analyst_trades
           WHERE expiry IS NOT NULL
             AND date(expiry) < date('now')
             AND inferred_status IS NULL
             AND is_trade = 1"""
    ).fetchall()
    if not targets:
        return []
    ids = [r["id"] for r in targets]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE analyst_trades SET inferred_status = 'expired_unknown' "
        f"WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()
    return [dict(r) for r in targets]
