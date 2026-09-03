"""PDF ingestion: pdf_files, pdf_analyses, processing_log, the Dropbox cursor, pipeline events, reanalysis and bridge state.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
from datetime import datetime, date, timedelta
from pathlib import Path
import logging

import db as _db  # noqa: E402

log = logging.getLogger("db")


def get_dropbox_cursor() -> str | None:
    row = _db.get_connection().execute("SELECT cursor FROM dropbox_state WHERE id = 1").fetchone()
    return row["cursor"] if row else None


def update_dropbox_cursor(cursor: str) -> None:
    conn = _db.get_connection()
    conn.execute(
        "UPDATE dropbox_state SET cursor = ?, last_poll_at = ? WHERE id = 1",
        (cursor, datetime.utcnow().isoformat()),
    )
    conn.commit()


def insert_pdf_file(
    dropbox_path: str,
    file_name: str,
    local_path: str,
    dropbox_rev: str | None = None,
    file_size_bytes: int | None = None,
    dropbox_modified_at: str | None = None,
    status: str = "DOWNLOADED",
) -> int:
    """`status` param (2026-08-20): the watcher registers a FAILED row
    for a download that exhausted its retries, so the cursor can advance
    without silently dropping the file — the retry sweep re-downloads it
    (see process_single_pdf's missing-file recovery)."""
    conn = _db.get_connection()
    cur = conn.execute(
        """INSERT OR IGNORE INTO pdf_files
           (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
            dropbox_modified_at, downloaded_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (dropbox_path, file_name, local_path, dropbox_rev, file_size_bytes,
         dropbox_modified_at, datetime.utcnow().isoformat(), status),
    )
    conn.commit()
    return cur.lastrowid


def get_latest_dropbox_modified_at() -> str | None:
    """Newest Dropbox server_modified we have registered, ISO string, or
    None on an empty table. Used as the floor when a cursor reset forces
    a from-scratch listing (2026-09-01): only entries newer than this
    are new, everything older is history the bot already saw or never
    wanted."""
    conn = _db.get_connection()
    row = conn.execute(
        "SELECT MAX(dropbox_modified_at) AS m FROM pdf_files").fetchone()
    return row["m"] if row and row["m"] else None


def get_pdf_by_path(dropbox_path: str) -> dict | None:
    row = _db.get_connection().execute(
        "SELECT * FROM pdf_files WHERE dropbox_path = ?", (dropbox_path,)
    ).fetchone()
    return dict(row) if row else None


def get_pending_pdfs(limit: int = 50) -> list[dict]:
    rows = _db.get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'DOWNLOADED'
           ORDER BY file_size_bytes ASC, created_at ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_failed_pdfs_for_retry(max_retries: int = 3) -> list[dict]:
    rows = _db.get_connection().execute(
        """SELECT * FROM pdf_files
           WHERE status = 'FAILED' AND retry_count < ?
           ORDER BY created_at ASC""",
        (max_retries,),
    ).fetchall()
    return [dict(r) for r in rows]


def reset_stale_processing(max_age_hours: float = 2.0,
                           max_retries: int = 3) -> list[dict]:
    """Reap pdf_files rows stranded in PROCESSING by a worker restart.

    process_single_pdf flips a row to PROCESSING before analysis; if the
    worker dies mid-flight the row stays there forever — the queue
    picker only reads DOWNLOADED and retry only reads FAILED
    (2026-07-20: 11 zombies dating back to April). Rows the Opus bridge
    owns are exempt: bridge_ingestion_state has its own watchdog +
    fallback-sweeper state machine. Rows already out of retries go to
    FAILED instead of crash-looping. Returns the changed rows.
    """
    conn = _db.get_connection()
    stale = conn.execute(
        """SELECT id, file_name, retry_count FROM pdf_files
           WHERE status = 'PROCESSING'
             AND updated_at < datetime('now', ?)
             AND id NOT IN (SELECT pdf_file_id FROM bridge_ingestion_state)""",
        (f"-{max_age_hours} hours",),
    ).fetchall()
    changed: list[dict] = []
    for row in stale:
        if row["retry_count"] >= max_retries:
            conn.execute(
                """UPDATE pdf_files
                   SET status = 'FAILED',
                       error_message = 'stale PROCESSING reaper: max retries exceeded',
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (row["id"],),
            )
            new_status = "FAILED"
        else:
            conn.execute(
                """UPDATE pdf_files
                   SET status = 'DOWNLOADED',
                       retry_count = retry_count + 1,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (row["id"],),
            )
            new_status = "DOWNLOADED"
        changed.append({
            "id": row["id"], "file_name": row["file_name"],
            "new_status": new_status,
        })
    conn.commit()
    return changed


def update_pdf_status(pdf_id: int, status: str, error_message: str | None = None) -> None:
    conn = _db.get_connection()
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
    conn = _db.get_connection()
    conn.execute(
        "UPDATE pdf_files SET priority = ?, updated_at = datetime('now') WHERE id = ?",
        (priority, pdf_id),
    )
    conn.commit()


def count_pending_queue() -> int:
    """Return how many PDFs are currently in the pending queue."""
    row = _db.get_connection().execute(
        "SELECT COUNT(*) as c FROM pdf_files WHERE status IN ('DOWNLOADED', 'PROCESSING')"
    ).fetchone()
    return row["c"] if row else 0


def clear_pending_queue() -> int:
    """Delete all DOWNLOADED/PROCESSING rows from pdf_files and their local files.

    Returns the number of rows deleted. Caller is responsible for any safety checks.
    """
    conn = _db.get_connection()
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
    conn = _db.get_connection()
    cur = conn.execute(
        """INSERT INTO pdf_analyses
           (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
            total_pages, input_tokens_used, output_tokens_used, model_used,
            analysis_duration_seconds)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (pdf_file_id, triage_json, analysis_json, priority, pages_analyzed,
         total_pages, input_tokens, output_tokens, model, duration),
    )
    analysis_id = cur.lastrowid
    # Keep pdf_entities in step with new analyses so ticker lookups stay
    # an indexed query (the boot backfill only covers pre-existing rows).
    try:
        _db._index_analysis_entities(conn, analysis_id, pdf_file_id, analysis_json)
    except Exception as e:
        log.warning(f"pdf_entities index failed (non-fatal): {e}")
    conn.commit()
    return analysis_id


def _index_analysis_entities(
    conn, analysis_id: int, pdf_file_id: int, analysis_json: str | None
) -> None:
    """Fan `entities_mentioned` out into pdf_entities for one analysis."""
    if not analysis_json:
        return
    import json as _json
    try:
        data = _json.loads(analysis_json) or {}
    except Exception:
        return
    ents = data.get("entities_mentioned") or []
    if not isinstance(ents, list):
        return
    payload, seen = [], set()
    for e in ents:
        if not isinstance(e, dict):
            continue
        tk = (e.get("ticker") or "").strip().upper()
        nm = (e.get("name") or "").strip()
        if not tk and not nm:
            continue
        if tk and tk in seen:
            continue
        seen.add(tk)
        payload.append((
            analysis_id, pdf_file_id, tk or None, nm[:120] or None,
            (e.get("asset_class") or "").strip()[:20] or None,
        ))
    if not payload:
        payload = [(analysis_id, pdf_file_id, None, None, None)]
    conn.executemany(
        "INSERT INTO pdf_entities "
        "(analysis_id, pdf_file_id, ticker, name, asset_class) "
        "VALUES (?, ?, ?, ?, ?)",
        payload,
    )


def get_todays_analyses(today: str | None = None) -> list[dict]:
    if today is None:
        today = date.today().isoformat()
    rows = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
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
    since_time = _db._normalize_ts(since_time)
    rows = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
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
    start_time = _db._normalize_ts(start_time)
    end_time = _db._normalize_ts(end_time)
    rows = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
        SELECT la.*, pf.file_name, pf.dropbox_path, pf.dropbox_modified_at
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE pf.dropbox_modified_at >= ? AND pf.dropbox_modified_at <= ?
        ORDER BY pf.dropbox_modified_at ASC""",
        (start_time, end_time),
    ).fetchall()
    return [dict(r) for r in rows]


def watchdog_already_alerted(kind: str, day: str) -> bool:
    """True if this watchdog already posted for `day` (idempotency guard)."""
    row = _db.get_connection().execute(
        """SELECT 1 FROM processing_log
           WHERE event_type = ? AND details = ? LIMIT 1""",
        (kind, day),
    ).fetchone()
    return row is not None


def get_ingest_feed_last_announced() -> int:
    """Highest pdf_file_id we've already posted to the ingestion feed."""
    row = _db.get_connection().execute(
        "SELECT last_announced_pdf_file_id FROM ingest_feed_state WHERE id = 1"
    ).fetchone()
    return int(row["last_announced_pdf_file_id"]) if row else 0


def set_ingest_feed_last_announced(pdf_file_id: int) -> None:
    conn = _db.get_connection()
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
    row = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
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
    rows = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
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
    row = _db.get_connection().execute(
        _db._LATEST_ANALYSIS_CTE + """
        SELECT COALESCE(MAX(pf.id), 0) AS m
        FROM latest_analyses la
        JOIN pdf_files pf ON la.pdf_file_id = pf.id
        WHERE la.priority IN ('high', 'medium')"""
    ).fetchone()
    return int(row["m"]) if row else 0


def queue_for_opus_bridge(pdf_file_id: int) -> None:
    """Insert (or reset) a bridge-state row for a HIGH PDF.

    Idempotent: if a row already exists for this pdf_file_id (e.g. from a
    prior reanalyze that fell back to Gemini), it's reset to status='pending'
    with a fresh queued_at.
    """
    _db.get_connection().execute(
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
    _db.get_connection().commit()


def get_bridge_state(pdf_file_id: int) -> dict | None:
    row = _db.get_connection().execute(
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
    conn = _db.get_connection()
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
    rows = _db.get_connection().execute(
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
    rows = _db.get_connection().execute(
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
    rows = _db.get_connection().execute(
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
    cutoff = _db._normalize_ts(timeout_iso) or timeout_iso
    rows = _db.get_connection().execute(
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
    cutoff = _db._normalize_ts(cutoff_iso) or cutoff_iso
    rows = _db.get_connection().execute(
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
    cur = _db.get_connection().execute(
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
    _db.get_connection().commit()
    return cur.lastrowid


def get_active_reanalyze_job() -> dict | None:
    """Return the next reanalyze job to work on, or None if nothing active.

    Order: 'processing' first (resume in-flight after a worker restart),
    then 'pending' by oldest. One job at a time — the scheduler processes
    them serially.
    """
    row = _db.get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           WHERE status IN ('processing', 'pending')
           ORDER BY (status = 'processing') DESC, created_at ASC
           LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def get_reanalyze_job(job_id: int) -> dict | None:
    row = _db.get_connection().execute(
        "SELECT * FROM reanalyze_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    return dict(row) if row else None


def get_recent_reanalyze_jobs(limit: int = 5) -> list[dict]:
    """Return the most recent reanalyze jobs (any status), newest first.

    Used by /status to show the active or recently-completed jobs.
    """
    rows = _db.get_connection().execute(
        """SELECT * FROM reanalyze_jobs
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def start_reanalyze_job(job_id: int) -> None:
    """Mark a pending job as processing and record started_at."""
    _db.get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'processing', started_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    _db.get_connection().commit()


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
    _db.get_connection().execute(
        f"UPDATE reanalyze_jobs SET {', '.join(sets)} WHERE id = ?",
        tuple(args),
    )
    _db.get_connection().commit()


def complete_reanalyze_job(job_id: int) -> None:
    """Mark a job as complete and record completed_at."""
    _db.get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'complete', completed_at = datetime('now')
           WHERE id = ?""",
        (job_id,),
    )
    _db.get_connection().commit()


def fail_reanalyze_job(job_id: int, error_message: str) -> None:
    """Mark a job as failed with an error message."""
    _db.get_connection().execute(
        """UPDATE reanalyze_jobs
           SET status = 'failed', completed_at = datetime('now'),
               error_message = ?
           WHERE id = ?""",
        (error_message[:500], job_id),
    )
    _db.get_connection().commit()


def log_event(pdf_file_id: int | None, event_type: str, status: str, details: str | None = None) -> None:
    conn = _db.get_connection()
    conn.execute(
        "INSERT INTO processing_log (pdf_file_id, event_type, status, details) VALUES (?, ?, ?, ?)",
        (pdf_file_id, event_type, status, details),
    )
    conn.commit()


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
    utc_start, utc_end, local_date = _db._today_utc_range()
    conn = _db.get_connection()

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
    conn = _db.get_connection()

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
        cutoff_pulse = _db._normalize_ts(last_daily["created_at"])
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


def record_pipeline_event(
    event_type: str,
    status: str,
    payload: dict | None = None,
) -> None:
    """Append a row to pipeline_events. Best-effort: failures here are
    swallowed since this is observability, never the critical path.

    Common event types:
      - 'profile_refresh' — daily user-profile job summary
      - 'chat_catchup'    — chat_messages scan summary
    Status: 'completed', 'failed', 'partial'.
    Payload: any JSON-serializable dict of run stats.
    """
    import json as _json
    try:
        conn = _db.get_connection()
        conn.execute(
            "INSERT INTO pipeline_events (event_type, status, payload) "
            "VALUES (?, ?, ?)",
            (
                str(event_type)[:64],
                str(status)[:32],
                _json.dumps(payload or {}, default=str)[:8000],
            ),
        )
        conn.commit()
    except Exception:
        # Observability must never break the calling job
        pass


def get_recent_pipeline_events(
    event_type: str | None = None,
    *,
    limit: int = 20,
) -> list[dict]:
    """Read recent pipeline events for a /status command or
    historical trend analysis. Newest-first.
    """
    if event_type:
        rows = _db.get_connection().execute(
            """SELECT id, event_type, status, payload,
                      datetime(created_at) AS created_at
               FROM pipeline_events
               WHERE event_type = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (event_type, int(limit)),
        ).fetchall()
    else:
        rows = _db.get_connection().execute(
            """SELECT id, event_type, status, payload,
                      datetime(created_at) AS created_at
               FROM pipeline_events
               ORDER BY created_at DESC
               LIMIT ?""",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


def run_retention_purge(
    *,
    pdf_analyses_keep_per_pdf: int = 2,
    processing_log_days: int = 30,
    pipeline_events_days: int = 90,
    manual_reports_days: int = 30,
) -> dict[str, int]:
    """Trim append-only tables. Returns {table_name: rows_deleted}.

    Single transaction per table — failures on one don't roll back
    the others. Safe to run while the bot is up: each DELETE uses
    indexed criteria and SQLite's per-statement locking releases
    quickly.

    pdf_analyses: keep latest `pdf_analyses_keep_per_pdf` per
        pdf_file_id (default 2 — current + immediate prior). The
        MAX(id) GROUP BY semantics that synthesis queries rely on
        stays correct because we keep the highest-id rows.
    processing_log: drop > `processing_log_days` days old (default 30).
    pipeline_events: drop > `pipeline_events_days` days old
        (default 90 — covers two QC cycles of history).
    manual_reports_days: drop daily_reports with report_type='manual'
        older than `manual_reports_days` days (default 30). Manual
        /pulse invocations are tests, not production records;
        report_type='daily' stays forever (small volume).
    """
    conn = _db.get_connection()
    results: dict[str, int] = {}

    # pdf_analyses — keep latest N per pdf_file_id
    try:
        cur = conn.execute(
            """DELETE FROM pdf_analyses
                WHERE id NOT IN (
                    SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY pdf_file_id
                                   ORDER BY id DESC
                               ) AS rn
                          FROM pdf_analyses
                    )
                    WHERE rn <= ?
                )""",
            (int(pdf_analyses_keep_per_pdf),),
        )
        conn.commit()
        results["pdf_analyses"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge pdf_analyses failed: {e}"
        )
        results["pdf_analyses"] = -1

    # processing_log — N-day retention
    try:
        cur = conn.execute(
            "DELETE FROM processing_log WHERE created_at < datetime('now', ?)",
            (f"-{int(processing_log_days)} days",),
        )
        conn.commit()
        results["processing_log"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge processing_log failed: {e}"
        )
        results["processing_log"] = -1

    # pipeline_events — N-day retention
    try:
        cur = conn.execute(
            "DELETE FROM pipeline_events WHERE created_at < datetime('now', ?)",
            (f"-{int(pipeline_events_days)} days",),
        )
        conn.commit()
        results["pipeline_events"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge pipeline_events failed: {e}"
        )
        results["pipeline_events"] = -1

    # daily_reports — manual-only N-day retention; daily_reports of
    # report_type='daily' are the production record, kept forever.
    try:
        cur = conn.execute(
            """DELETE FROM daily_reports
                WHERE report_type = 'manual'
                  AND created_at < datetime('now', ?)""",
            (f"-{int(manual_reports_days)} days",),
        )
        conn.commit()
        results["daily_reports_manual"] = cur.rowcount
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f"retention purge daily_reports_manual failed: {e}"
        )
        results["daily_reports_manual"] = -1

    return results


def recently_covered_tickers(days: int = 7) -> set[str]:
    """Tickers a bank wrote EARNINGS content about in the last `days`
    (owner call 2026-09-02, for the calendar's bold rows): the latest
    analysis per PDF whose earnings_insights is non-empty, or whose
    title reads as a preview, contributes every ticker it names in
    entities_mentioned. Best-effort, never raises."""
    import json as _json
    import re as _re
    preview = _re.compile(r"(earnings? preview|preview|into the print|ahead of (?:earnings|results|the print)|what to expect|results? preview)", _re.I)
    out: set[str] = set()
    try:
        rows = _db.get_connection().execute(
            """SELECT a.analysis_json FROM pdf_analyses a
               WHERE a.id IN (SELECT MAX(id) FROM pdf_analyses GROUP BY pdf_file_id)
                 AND a.created_at >= datetime('now', ?)""",
            (f"-{int(days)} days",)).fetchall()
    except Exception as e:
        log.warning(f"recently_covered_tickers query failed: {e}")
        return out
    for r in rows:
        try:
            a = _json.loads(r["analysis_json"] or "{}")
        except Exception:
            continue
        insights = [str(x) for x in (a.get("earnings_insights") or []) if x]
        title = a.get("title") or ""
        # The ticker must be NAMED inside an earnings insight (or in a
        # preview-shaped title), not merely mentioned somewhere in the
        # note: a morning briefing lists forty tickers and one earnings
        # line, and the first cut of this rule bolded 436 names.
        hay = " ".join(insights + ([title] if preview.search(title) else [])).lower()
        if not hay.strip():
            continue
        for ent in a.get("entities_mentioned") or []:
            if not isinstance(ent, dict):
                continue
            t = (ent.get("ticker") or "").strip().upper().lstrip("$")
            name = (ent.get("name") or "").strip().lower()
            if not (1 <= len(t) <= 6) or (ent.get("asset_class") or "stock") not in ("stock", "etf", ""):
                continue
            if _re.search(r"(?<![a-z0-9$])\$?" + _re.escape(t.lower()) + r"(?![a-z0-9])", hay) or \
               (len(name) >= 4 and name in hay):
                out.add(t)
    return out
