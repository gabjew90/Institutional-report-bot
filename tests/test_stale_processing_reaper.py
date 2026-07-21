"""Stale-PROCESSING reaper — rows caught mid-analysis by a worker
restart never leave PROCESSING (the queue picker only reads DOWNLOADED,
retry only reads FAILED). Found 2026-07-20: 11 zombie rows dating back
to April. The reaper resets them to DOWNLOADED so the normal queue
picks them up, except rows the Opus bridge owns (bridge_ingestion_state
has its own watchdog/sweeper state machine) and rows out of retries,
which go to FAILED.
"""
import db


def _insert_pdf(path: str, status: str, age_hours: float = 0.0,
                retry_count: int = 0) -> int:
    conn = db.get_connection()
    cur = conn.execute(
        """INSERT INTO pdf_files
               (dropbox_path, file_name, status, retry_count,
                created_at, updated_at)
           VALUES (?, ?, ?, ?, datetime('now'),
                   datetime('now', ?))""",
        (path, path.rsplit("/", 1)[-1], status, retry_count,
         f"-{age_hours} hours"),
    )
    conn.commit()
    return cur.lastrowid


def _status(pdf_id: int) -> dict:
    row = db.get_connection().execute(
        "SELECT status, retry_count FROM pdf_files WHERE id = ?", (pdf_id,)
    ).fetchone()
    return dict(row)


def test_stale_processing_resets_to_downloaded():
    pdf_id = _insert_pdf("/t/stale1.pdf", "PROCESSING", age_hours=3)
    reset = db.reset_stale_processing(max_age_hours=2)
    assert pdf_id in [r["id"] for r in reset]
    after = _status(pdf_id)
    assert after["status"] == "DOWNLOADED"
    assert after["retry_count"] == 1


def test_fresh_processing_untouched():
    pdf_id = _insert_pdf("/t/fresh1.pdf", "PROCESSING", age_hours=0)
    db.reset_stale_processing(max_age_hours=2)
    assert _status(pdf_id)["status"] == "PROCESSING"


def test_bridge_queued_rows_exempt():
    pdf_id = _insert_pdf("/t/bridge1.pdf", "PROCESSING", age_hours=48)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO bridge_ingestion_state (pdf_file_id, status) "
        "VALUES (?, 'queued')",
        (pdf_id,),
    )
    conn.commit()
    db.reset_stale_processing(max_age_hours=2)
    assert _status(pdf_id)["status"] == "PROCESSING"


def test_out_of_retries_goes_failed():
    pdf_id = _insert_pdf("/t/retries1.pdf", "PROCESSING", age_hours=3,
                         retry_count=3)
    db.reset_stale_processing(max_age_hours=2, max_retries=3)
    after = _status(pdf_id)
    assert after["status"] == "FAILED"


def test_non_processing_statuses_untouched():
    done = _insert_pdf("/t/done1.pdf", "PROCESSED", age_hours=100)
    failed = _insert_pdf("/t/failed1.pdf", "FAILED", age_hours=100)
    db.reset_stale_processing(max_age_hours=2)
    assert _status(done)["status"] == "PROCESSED"
    assert _status(failed)["status"] == "FAILED"
