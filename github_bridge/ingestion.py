"""Parallel HIGH-priority ingestion bridge — dump + pull jobs.

Architecture mirrors github_bridge/jobs.py but operates per-PDF instead
of per-pulse:

  Railway worker
    triage HIGH PDF → queue_for_opus_bridge(pdf_file_id)  [orchestrator]
                                                              |
   dump_pending_high_ingestions_job (every 5 min):           |
     reads bridge_ingestion_state WHERE status='pending'      |
     for each:                                                |
       - guardrail check (pages, file size, attempt_count)    |
       - if violated → mark fallback_to_gemini                |
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
     reads ingest-complete/*.json + ingest-failed/*.json      |
     INSERT pdf_analyses, mark state 'completed'              |
     deletes the bridge files to keep the branch slim         <

CONSUMER CONTRACT (Anthropic-side cron routine):
- Read directory listing of ingest-pending/. Process every file matching
  `<pdf_file_id>.json` — that's the completion marker. The .json is committed
  AFTER the .pdf, so its presence implies both files are present.
- For each JSON: parse metadata, then Read the matching .pdf via Anthropic's
  native PDF Read tool. Run the deep-analysis system prompt
  (ai_analysis/prompts.py:ANALYSIS_SYSTEM_PROMPT) and produce the
  PdfAnalysis-shaped JSON.
- Commit the result to ingest-complete/<pdf_file_id>.json.
- On routine-side failure (oversized, prompt error, etc.), commit a small
  JSON to ingest-failed/<pdf_file_id>.json with a "reason" field. Railway
  treats that as a Gemini-fallback signal.
- The routine does NOT delete files in ingest-pending/. Railway's pull
  job is the sole pruner: it deletes both the pending pair AND the
  complete/failed marker after a successful DB write.
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

    # Poison-pill threshold: a single PDF that fails N consecutive dump
    # attempts gets routed to Gemini fallback rather than retried forever.
    MAX_DUMP_ATTEMPTS = 5

    for row in pending:
        pdf_file_id = int(row["pdf_file_id"])
        prior_attempts = int(row.get("attempt_count") or 0)
        # Attempt-cap guard: stop retrying poison rows. The fallback sweeper
        # (step 4) will run Gemini on these.
        if prior_attempts >= MAX_DUMP_ATTEMPTS:
            log.warning(
                f"Bridge dump: pdf_file_id={pdf_file_id} hit MAX_DUMP_ATTEMPTS "
                f"({prior_attempts}); marking fallback_to_gemini"
            )
            db.update_bridge_status(
                pdf_file_id,
                status="fallback_to_gemini",
                fallback_reason=f"exceeded {MAX_DUMP_ATTEMPTS} dump attempts",
            )
            fallback += 1
            continue

        # Track the local path we materialized so we can clean up post-commit
        materialized_path: str | None = None
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
            # Track materialized re-downloads for cleanup (only if we created it,
            # i.e., not the original local_path that was already on disk)
            if local_path != (row.get("local_path") or ""):
                materialized_path = local_path

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

            # Read PDF bytes, commit binary + sidecar to the bridge branch.
            # ORDER MATTERS: PDF first, JSON second. The Anthropic routine keys
            # off the JSON sidecar's presence — that way a half-committed pair
            # (PDF lands but JSON commit fails) won't be picked up prematurely.
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
            json_resp = gh.put_file(
                json_path,
                json.dumps(sidecar, indent=2),
                f"Bridge: sidecar metadata for PDF {pdf_file_id} (completion marker)",
            )
            # Use the json commit sha (last commit in the pair = "ready" point)
            commit_sha = (json_resp.get("commit") or {}).get("sha") or \
                         (pdf_resp.get("commit") or {}).get("sha") or ""

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
            # Successful commit — the PDF is now safely on the bridge branch.
            # Clean up the local copy regardless of whether we re-downloaded
            # (orchestrator.process_single_pdf no longer deletes when routed
            # to bridge — that's our responsibility now).
            for path_to_remove in {materialized_path, row.get("local_path") or ""}:
                if path_to_remove:
                    try:
                        Path(path_to_remove).unlink(missing_ok=True)
                    except Exception:
                        pass
        except Exception as e:
            log.error(
                f"Bridge dump: failed for pdf_file_id={pdf_file_id}: {e}",
                exc_info=True,
            )
            # Don't mark 'failed' — let next tick retry, but DO increment
            # attempt_count so the poison-pill guard above can eventually
            # convert this to a Gemini fallback instead of looping forever.
            try:
                db.update_bridge_status(
                    pdf_file_id,
                    status="pending",
                    error_message=f"dump failed: {str(e)[:200]}",
                    increment_attempt=True,
                )
            except Exception:
                pass

    log.info(f"Bridge dump tick complete: {committed} committed, {fallback} fell back to Gemini")


# -----------------------------------------------------------------------------
# Pull job — reads completed analyses from the bridge into the DB
# -----------------------------------------------------------------------------


def _pdf_id_from_bridge_filename(name: str) -> int | None:
    """Routine outputs are named '<pdf_file_id>.json'. Parse robustly."""
    if not name.endswith(".json"):
        return None
    stem = name[: -len(".json")]
    try:
        return int(stem)
    except ValueError:
        return None


def _validate_analysis_payload(payload: dict, pdf_file_id: int) -> str | None:
    """Return error string if the payload is malformed; None if it looks OK.

    The routine is supposed to produce the same JSON schema we extract from
    Gemini deep analysis (PdfAnalysis-shaped dict). Bare minimum: a source,
    a title, a list of key_insights, and the pdf_file_id matching the file.
    """
    if not isinstance(payload, dict):
        return f"payload not a dict (got {type(payload).__name__})"
    if int(payload.get("pdf_file_id") or 0) != pdf_file_id:
        return f"pdf_file_id mismatch (json={payload.get('pdf_file_id')}, expected={pdf_file_id})"
    if not (payload.get("source") or "").strip():
        return "missing source"
    if not isinstance(payload.get("key_insights"), list):
        return "key_insights not a list"
    return None


def _delete_bridge_files(pdf_file_id: int, kinds: list[str]) -> None:
    """Best-effort delete of bridge files for a pdf_file_id. kinds: pdf|json|complete|failed."""
    paths: list[str] = []
    if "pdf" in kinds:
        paths.append(f"{INGEST_PENDING_DIR}/{pdf_file_id}.pdf")
    if "json" in kinds:
        paths.append(f"{INGEST_PENDING_DIR}/{pdf_file_id}.json")
    if "complete" in kinds:
        paths.append(f"{INGEST_COMPLETE_DIR}/{pdf_file_id}.json")
    if "failed" in kinds:
        paths.append(f"{INGEST_FAILED_DIR}/{pdf_file_id}.json")
    for p in paths:
        try:
            gh.delete_file(p, message=f"Bridge: prune {p} after Railway ingestion")
        except Exception as e:
            log.warning(f"Bridge: prune of {p} failed (non-fatal): {e}")


def _ingest_one_completed(item: dict) -> bool:
    """Process one ingest-complete/<id>.json file. Returns True on success."""
    name = item.get("name") or ""
    pdf_file_id = _pdf_id_from_bridge_filename(name)
    if pdf_file_id is None:
        log.warning(f"Bridge pull: skipping unrecognized filename {name}")
        return False

    state = db.get_bridge_state(pdf_file_id)
    if not state:
        # Unknown pdf_file_id — could be a stale artifact from a manual test.
        # Prune to keep the branch clean, but don't crash.
        log.warning(f"Bridge pull: ingest-complete/{name} has no bridge_state row — pruning")
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "complete"])
        return False

    if state.get("status") in ("completed", "fallback_to_gemini", "failed"):
        # Idempotency: already processed this PDF. Prune the residual bridge files.
        log.info(f"Bridge pull: pdf_file_id={pdf_file_id} already terminal — pruning bridge files")
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "complete"])
        return False

    # Fetch the full file body (list_dir gives metadata only)
    raw = gh.get_file_text(f"{INGEST_COMPLETE_DIR}/{name}")
    if raw is None:
        log.warning(f"Bridge pull: failed to fetch {name}")
        return False

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Bridge pull: malformed JSON in {name}: {e}")
        db.update_bridge_status(
            pdf_file_id,
            status="fallback_to_gemini",
            fallback_reason=f"routine returned malformed JSON: {str(e)[:120]}",
        )
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "complete"])
        return False

    err = _validate_analysis_payload(payload, pdf_file_id)
    if err:
        log.error(f"Bridge pull: validation failed for {name}: {err}")
        db.update_bridge_status(
            pdf_file_id,
            status="fallback_to_gemini",
            fallback_reason=f"routine output failed validation: {err}",
        )
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "complete"])
        return False

    # Successful payload → INSERT into pdf_analyses (same schema as Gemini path)
    triage_json_str = json.dumps({
        "priority": payload.get("priority", "high"),
        "report_type": payload.get("report_type", "other"),
        "source": payload.get("source", ""),
        "key_tickers": [],  # not produced by Opus path; downstream tolerates empty
        "summary": "",
    })
    try:
        db.insert_analysis(
            pdf_file_id=pdf_file_id,
            triage_json=triage_json_str,
            analysis_json=json.dumps(payload),
            priority=payload.get("priority", "high"),
            pages_analyzed=int(payload.get("pages_analyzed") or 0),
            total_pages=int(payload.get("total_pages") or 0),
            input_tokens=int(payload.get("input_tokens") or 0),
            output_tokens=int(payload.get("output_tokens") or 0),
            model="opus-bridge",
            duration=0.0,
        )
        # Mark file as PROCESSED so it stops appearing in pending queues
        db.update_pdf_status(pdf_file_id, "PROCESSED")
        db.update_bridge_status(pdf_file_id, status="completed")
        log.info(f"Bridge pull: pdf_file_id={pdf_file_id} → completed via Opus")
        # Prune bridge files now that the analysis is safely in the DB
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "complete"])
        return True
    except Exception as e:
        log.error(f"Bridge pull: DB insert failed for pdf_file_id={pdf_file_id}: {e}", exc_info=True)
        # Don't prune yet — leave the bridge file for the next tick to retry.
        return False


def _process_failed_one(item: dict) -> None:
    """Routine reported a hard failure for this PDF — fall back to Gemini."""
    name = item.get("name") or ""
    pdf_file_id = _pdf_id_from_bridge_filename(name)
    if pdf_file_id is None:
        return
    state = db.get_bridge_state(pdf_file_id)
    if not state or state.get("status") in ("completed", "fallback_to_gemini", "failed"):
        _delete_bridge_files(pdf_file_id, ["pdf", "json", "failed"])
        return
    raw = gh.get_file_text(f"{INGEST_FAILED_DIR}/{name}")
    reason = "routine reported failure"
    if raw:
        try:
            data = json.loads(raw)
            reason = (data.get("reason") or reason)[:200]
        except Exception:
            pass
    db.update_bridge_status(
        pdf_file_id,
        status="fallback_to_gemini",
        fallback_reason=reason,
    )
    _delete_bridge_files(pdf_file_id, ["pdf", "json", "failed"])


def pull_completed_ingestions_job() -> None:
    """Periodic job — pull completed/failed analyses from the bridge into the DB.

    Runs every ~2 min when bridge is enabled. Iterates two directories:
      - ingest-complete/  → INSERT into pdf_analyses, mark state 'completed'
      - ingest-failed/    → mark state 'fallback_to_gemini' with reason

    Idempotent: re-running is safe; a successful insert prunes the bridge
    files, so repeat ticks become no-ops on already-handled PDFs.
    """
    if not opus_bridge_enabled():
        return

    # Completed
    try:
        completed_items = gh.list_dir(INGEST_COMPLETE_DIR)
    except Exception as e:
        log.warning(f"Bridge pull: list {INGEST_COMPLETE_DIR} failed: {e}")
        completed_items = []

    completed_files = [it for it in completed_items if it.get("type") == "file" and (it.get("name") or "").endswith(".json")]
    n_ok = 0
    for item in completed_files:
        if _ingest_one_completed(item):
            n_ok += 1

    # Routine-reported failures
    try:
        failed_items = gh.list_dir(INGEST_FAILED_DIR)
    except Exception as e:
        log.warning(f"Bridge pull: list {INGEST_FAILED_DIR} failed: {e}")
        failed_items = []

    failed_files = [it for it in failed_items if it.get("type") == "file" and (it.get("name") or "").endswith(".json")]
    for item in failed_files:
        _process_failed_one(item)

    if completed_files or failed_files:
        log.info(
            f"Bridge pull tick: {n_ok}/{len(completed_files)} completed ingested, "
            f"{len(failed_files)} routine-failures handled"
        )


# -----------------------------------------------------------------------------
# Timeout watchdog — find committed rows the routine never finished
# -----------------------------------------------------------------------------


def watchdog_timeout_committed_job() -> None:
    """Periodic — convert stale 'committed' rows into 'fallback_to_gemini'.

    A row is stale when committed_at < (now - opus_bridge_timeout_minutes).
    Causes: routine fire missed, exceeded its token budget, sandbox
    network failure, etc. We don't try to retry the bridge — the
    fallback sweeper picks these up and runs Gemini directly.

    Runs every 5 min when bridge is enabled. Idempotent: re-running
    doesn't double-mark since stale-and-already-flipped rows have
    status='fallback_to_gemini' (filtered out by the query).
    """
    if not opus_bridge_enabled():
        return
    from datetime import datetime, timedelta
    timeout_minutes = settings.opus_bridge_timeout_minutes
    cutoff = (datetime.utcnow() - timedelta(minutes=timeout_minutes)).isoformat()
    stale = db.find_timed_out_bridge_committed(cutoff, limit=50)
    if not stale:
        return
    log.warning(
        f"Bridge watchdog: {len(stale)} PDFs committed > {timeout_minutes}min ago "
        f"with no Opus result → routing to Gemini fallback"
    )
    for row in stale:
        pdf_file_id = int(row["pdf_file_id"])
        db.update_bridge_status(
            pdf_file_id,
            status="fallback_to_gemini",
            fallback_reason=f"routine timeout (no result {timeout_minutes}min after commit)",
        )
        # Best-effort prune the orphan bridge files; the routine apparently
        # didn't process them, so they're just consuming branch space.
        _delete_bridge_files(pdf_file_id, ["pdf", "json"])


# -----------------------------------------------------------------------------
# Fallback sweeper — actually run Gemini on rows tagged fallback_to_gemini
# -----------------------------------------------------------------------------


async def fallback_sweeper_job() -> None:
    """Periodic — run Gemini deep-analysis on rows marked fallback_to_gemini.

    A bridge row reaches fallback_to_gemini for several reasons:
      - oversized PDF (guardrails)
      - dump-job poison-pill exceeded MAX_DUMP_ATTEMPTS
      - routine timeout (watchdog)
      - routine reported failure (ingest-failed/<id>.json)
      - routine produced malformed JSON (pull job)

    All of those leave the PDF without a pdf_analyses row. This sweeper
    fixes that by running the existing Gemini deep-analysis path.
    On success: INSERT pdf_analyses, mark state 'completed', update
    pdf_files.status='PROCESSED', clean up the local PDF.

    Runs every 5 min when bridge is enabled. Caps per-tick batch at 5
    so a flood of fallbacks doesn't monopolize the worker.
    """
    if not opus_bridge_enabled():
        return
    rows = db.get_fallback_bridge_pdfs(limit=5)
    if not rows:
        return
    log.info(f"Bridge fallback sweeper: running Gemini on {len(rows)} PDFs")

    # Lazy imports to avoid circular dependency at module load time
    import asyncio
    import json as _json
    from dataclasses import asdict
    from pdf_processing.extractor import extract_pdf
    from ai_analysis.analyzer import triage_pdf, analyze_pdf_deep

    n_ok = 0
    n_failed = 0

    for row in rows:
        pdf_file_id = int(row["pdf_file_id"])
        try:
            local_path = await asyncio.to_thread(_gather_local_path, row)
            if not local_path:
                log.error(
                    f"Bridge fallback: pdf_file_id={pdf_file_id} — local file unavailable, "
                    f"cannot run Gemini fallback"
                )
                db.update_bridge_status(
                    pdf_file_id,
                    status="failed",
                    error_message="fallback failed: PDF unrecoverable from Dropbox",
                )
                db.update_pdf_status(pdf_file_id, "FAILED", "bridge fallback: PDF gone")
                n_failed += 1
                continue

            # Extract pages + full text, triage, then deep-analyze (mirrors
            # process_single_pdf but without the routing fork — we already
            # know we're on the fallback path).
            extraction = await asyncio.to_thread(extract_pdf, local_path, None)
            full_text = extraction.full_text
            file_name = row.get("file_name", "")
            folder_path = (row.get("dropbox_path") or "").rsplit("/", 1)[0]

            triage = await triage_pdf(file_name, full_text, folder_path=folder_path)
            analysis = await analyze_pdf_deep(
                pdf_file_id=pdf_file_id,
                file_name=file_name,
                extraction=extraction,
                priority=triage.priority,
                source=triage.source,
                report_type=triage.report_type,
            )

            # INSERT into pdf_analyses (model tagged 'gemini-fallback' for clarity)
            db.insert_analysis(
                pdf_file_id=pdf_file_id,
                triage_json=_json.dumps(asdict(triage)),
                analysis_json=_json.dumps(asdict(analysis)),
                priority=triage.priority,
                pages_analyzed=analysis.pages_analyzed,
                total_pages=analysis.total_pages,
                input_tokens=analysis.input_tokens + triage.input_tokens,
                output_tokens=analysis.output_tokens + triage.output_tokens,
                model="gemini-fallback",
                duration=0.0,
            )
            db.update_pdf_status(pdf_file_id, "PROCESSED")
            db.update_bridge_status(pdf_file_id, status="completed")
            n_ok += 1
            log.info(
                f"Bridge fallback: pdf_file_id={pdf_file_id} → completed via Gemini "
                f"(reason: {row.get('fallback_reason') or 'n/a'})"
            )

            # Clean up if we re-downloaded
            if local_path != (row.get("local_path") or ""):
                try:
                    Path(local_path).unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            log.error(
                f"Bridge fallback: Gemini path failed for pdf_file_id={pdf_file_id}: {e}",
                exc_info=True,
            )
            db.update_bridge_status(
                pdf_file_id,
                status="failed",
                error_message=f"fallback Gemini failed: {str(e)[:200]}",
            )
            db.update_pdf_status(pdf_file_id, "FAILED", str(e)[:200])
            n_failed += 1

    log.info(
        f"Bridge fallback sweeper tick complete: {n_ok} succeeded via Gemini, "
        f"{n_failed} hard-failed"
    )
