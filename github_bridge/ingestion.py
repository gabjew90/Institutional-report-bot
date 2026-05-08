"""Parallel HIGH-priority ingestion bridge — dump + pull jobs.

Architecture mirrors github_bridge/jobs.py but operates per-PDF instead
of per-pulse:

  Railway worker
    triage HIGH PDF → queue_for_opus_bridge(pdf_file_id)  [orchestrator]
                                                              |
   dump_pending_high_ingestions_job (every 5 min):           |
     reads bridge_ingestion_state WHERE status='pending'      |
     for each:                                                |
       - guardrail check (pages, file size)                   |
       - if oversized → mark fallback_to_gemini               |
       - else commit raw PDF to ingest-pending/<id>.pdf       |
              + sidecar JSON to ingest-pending/<id>.json      |
              update state to 'committed'                     |
                                                              v
                                           Anthropic-side cron Opus routine:
                                             ingest-pending/<id>.{pdf,json} →
                                               run deep-analysis prompt →
                                                 commit ingest-complete/<id>.json
                                                              |
   pull_completed_ingestions_job (every 2 min):               |
     reads ingest-complete/*.json from bridge                 |
     INSERT pdf_analyses, mark state 'completed'              |
     deletes the bridge files to keep the branch slim         <
"""

import json
import logging
from dataclasses import asdict
from pathlib import Path

from config import settings
from github_bridge import client as gh
import db

log = logging.getLogger(__name__)

INGEST_PENDING_DIR = "ingest-pending"
INGEST_COMPLETE_DIR = "ingest-complete"
INGEST_FAILED_DIR = "ingest-failed"


def opus_bridge_enabled() -> bool:
    """The bridge plumbing only runs when configured AND the backend is set
    to opus_bridge. Gemini-only deployments skip the dump/pull/watchdog work
    entirely."""
    return (
        bool(settings.github_token)
        and bool(settings.github_repo)
        and bool(settings.github_bridge_branch)
        and settings.high_ingestion_backend == "opus_bridge"
    )


def _pdf_page_count(local_path: str) -> int | None:
    """Page count from a local PDF file. Returns None on read failure."""
    try:
        import fitz  # PyMuPDF
        with fitz.open(local_path) as doc:
            return len(doc)
    except Exception as e:
        log.warning(f"Bridge: page-count failed for {local_path}: {e}")
        return None


def _build_sidecar(row: dict, page_count: int | None) -> dict:
    """Sidecar JSON metadata committed alongside each PDF. The Opus routine
    reads this to populate `source` / `priority` / etc in its output
    without re-doing triage."""
    return {
        "pdf_file_id": int(row["pdf_file_id"]),
        "file_name": row.get("file_name", ""),
        "dropbox_path": row.get("dropbox_path", ""),
        "dropbox_modified_at": row.get("dropbox_modified_at", ""),
        "file_size_bytes": int(row.get("file_size_bytes") or 0),
        "page_count": page_count,
        "queued_at": row.get("queued_at", ""),
    }


def _gather_local_path(row: dict) -> str | None:
    """Return a usable local PDF path. Re-downloads from Dropbox if the
    original local file was already cleaned up after Gemini processing."""
    candidate = row.get("local_path") or ""
    if candidate and Path(candidate).exists():
        return candidate

    # Re-download from Dropbox to a temp path under the configured download dir
    dropbox_path = row.get("dropbox_path")
    if not dropbox_path:
        return None
    try:
        from dropbox_client.watcher import download_file
        download_dir = Path(settings.pdf_download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        safe_name = (row.get("file_name") or f"bridge-{row['pdf_file_id']}.pdf").replace("/", "_")
        target = download_dir / f"bridge_{row['pdf_file_id']}_{safe_name}"
        download_file(dropbox_path, target)
        if target.exists():
            return str(target)
    except Exception as e:
        log.warning(f"Bridge: re-download failed for pdf_file_id={row['pdf_file_id']}: {e}")
    return None


def _violates_guardrails(file_size_bytes: int, page_count: int | None) -> str | None:
    """Return a fallback reason string if guardrails are violated, else None."""
    max_bytes = settings.opus_bridge_max_size_mb * 1024 * 1024
    if file_size_bytes and file_size_bytes > max_bytes:
        return (
            f"oversized: {file_size_bytes / 1024 / 1024:.1f}MB > "
            f"{settings.opus_bridge_max_size_mb}MB limit"
        )
    if page_count is not None and page_count > settings.opus_bridge_max_pages:
        return f"oversized: {page_count} pages > {settings.opus_bridge_max_pages}-page limit"
    return None


def dump_pending_high_ingestions_job() -> None:
    """Periodic job — package pending HIGH PDFs to the bridge branch.

    Runs every ~5 min (driven by scheduler/jobs.py). Skips silently when
    bridge isn't enabled. Each pending row either:
      a) commits PDF + sidecar JSON to ingest-pending/<id>.{pdf,json}
         and transitions state to 'committed'
      b) marks fallback_to_gemini if the file is missing or oversized
    """
    if not opus_bridge_enabled():
        return

    pending = db.get_pending_bridge_pdfs(limit=50)
    if not pending:
        return

    log.info(f"Bridge dump: {len(pending)} pending HIGH PDFs to commit")
    committed = 0
    fallback = 0

    for row in pending:
        pdf_file_id = int(row["pdf_file_id"])
        try:
            local_path = _gather_local_path(row)
            if not local_path:
                db.update_bridge_status(
                    pdf_file_id,
                    status="fallback_to_gemini",
                    fallback_reason="local PDF unavailable and Dropbox re-download failed",
                    increment_attempt=True,
                )
                fallback += 1
                continue

            page_count = _pdf_page_count(local_path)
            file_size = int(row.get("file_size_bytes") or 0) or Path(local_path).stat().st_size

            violation = _violates_guardrails(file_size, page_count)
            if violation:
                log.info(f"Bridge dump: pdf_file_id={pdf_file_id} → fallback ({violation})")
                db.update_bridge_status(
                    pdf_file_id,
                    status="fallback_to_gemini",
                    fallback_reason=violation,
                    increment_attempt=True,
                )
                fallback += 1
                continue

            # Read PDF bytes, commit binary + sidecar to the bridge branch
            with open(local_path, "rb") as f:
                pdf_bytes = f.read()
            sidecar = _build_sidecar(row, page_count)

            pdf_path = f"{INGEST_PENDING_DIR}/{pdf_file_id}.pdf"
            json_path = f"{INGEST_PENDING_DIR}/{pdf_file_id}.json"

            gh.ensure_branch_exists()
            pdf_resp = gh.put_file_binary(
                pdf_path,
                pdf_bytes,
                f"Bridge: queue PDF {pdf_file_id} for Opus ingestion",
            )
            gh.put_file(
                json_path,
                json.dumps(sidecar, indent=2),
                f"Bridge: sidecar metadata for PDF {pdf_file_id}",
            )
            commit_sha = (pdf_resp.get("commit") or {}).get("sha") or ""

            db.update_bridge_status(
                pdf_file_id,
                status="committed",
                bridge_filename=pdf_path,
                bridge_commit_sha=commit_sha,
                increment_attempt=True,
            )
            committed += 1
            log.info(
                f"Bridge dump: committed pdf_file_id={pdf_file_id} "
                f"({len(pdf_bytes) / 1024:.0f}KB, {page_count or '?'}p) → {pdf_path}"
            )
        except Exception as e:
            log.error(
                f"Bridge dump: failed for pdf_file_id={pdf_file_id}: {e}",
                exc_info=True,
            )
            # Don't mark as 'failed' — leave as 'pending' so next tick retries.
            # The attempt_count we'd want here happens in update_bridge_status,
            # which we couldn't reach. Stamp the error message and move on.
            try:
                db.update_bridge_status(
                    pdf_file_id,
                    status="pending",
                    error_message=f"dump failed: {str(e)[:200]}",
                )
            except Exception:
                pass

    log.info(f"Bridge dump tick complete: {committed} committed, {fallback} fell back to Gemini")
