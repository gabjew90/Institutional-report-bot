"""End-to-end pipeline coordination.

Handles the full flow: Dropbox → PDF processing → AI analysis → report → Discord.
"""

import asyncio
import json
import logging
import time
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from ai_analysis.analyzer import triage_pdf, analyze_pdf_deep
from ai_analysis.models import PdfAnalysis
from dropbox_client.watcher import list_folder_files, download_file, _get_client
from pdf_processing.extractor import extract_text_per_page, extract_pdf
from report.synthesizer import synthesize_daily_pulse
from report.models import DailyReport
from config import settings
import db

log = logging.getLogger(__name__)


async def process_single_pdf(pdf_data: dict) -> PdfAnalysis | None:
    """Process a single PDF through the full pipeline.

    extract text → triage (Gemini text-only) → deep analysis (Gemini text-only,
    full document). LOW-priority PDFs skip deep analysis and store the triage
    summary as the analysis result.
    """
    pdf_id = pdf_data["id"]
    file_name = pdf_data["file_name"]
    local_path = pdf_data["local_path"]
    dropbox_path = pdf_data.get("dropbox_path") or ""
    folder_path = str(Path(dropbox_path).parent) if dropbox_path else ""

    if not local_path or not Path(local_path).exists():
        # Missing-file recovery (2026-08-20): re-download from Dropbox
        # instead of burning a retry on "file not found". This is the
        # retry path for downloads that failed at poll time (the watcher
        # registers those as FAILED rows so the cursor can advance) and
        # for local files lost to a volume wipe.
        if dropbox_path and local_path:
            try:
                from dropbox_client.watcher import download_file
                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(
                    download_file, dropbox_path, Path(local_path))
                db.log_event(pdf_id, "download", "completed",
                             "re-downloaded by retry sweep")
                log.info(f"Re-downloaded missing PDF: {file_name}")
            except Exception as e:
                log.error(f"Re-download failed for {dropbox_path}: {e}")
                db.update_pdf_status(
                    pdf_id, "FAILED", f"re-download failed: {str(e)[:200]}")
                return None
        if not local_path or not Path(local_path).exists():
            log.error(f"PDF file not found: {local_path}")
            db.update_pdf_status(pdf_id, "FAILED", "Local file not found")
            return None

    try:
        db.update_pdf_status(pdf_id, "PROCESSING")
        db.log_event(pdf_id, "process", "started")
        start_time = time.time()

        # Step 1: Extract text from all pages (blocking I/O — run in thread)
        pages = await asyncio.to_thread(extract_text_per_page, local_path)
        full_text = "\n".join(p.text for p in pages)

        # Step 2: Triage with Gemini (folder_path lets it identify source reliably)
        triage = await triage_pdf(file_name, full_text, folder_path=folder_path)
        db.update_pdf_priority(pdf_id, triage.priority)
        log.info(f"Triaged {file_name}: {triage.priority} ({triage.report_type})")

        # Step 3: Based on priority, do deep analysis or skip
        if triage.priority == "low":
            # Store triage result directly as the analysis.
            # Use triage.source (Gemini-extracted) so LOW items show up under
            # their bank in /status and pulse footer stats, not "Unknown".
            analysis = PdfAnalysis(
                pdf_file_id=pdf_id,
                file_name=file_name,
                source=triage.source or "Unknown",
                title=file_name,
                report_type=triage.report_type,
                priority="low",
                key_insights=[triage.summary] if triage.summary else [],
                total_pages=len(pages),
                input_tokens=triage.input_tokens,
                output_tokens=triage.output_tokens,
            )
        else:
            # HIGH-priority routing fork: when settings.high_ingestion_backend
            # is set to "opus_bridge", route to the parallel Opus routine via
            # the GitHub bridge instead of running Gemini deep analysis here.
            # The dump job (github_bridge/ingestion.py) commits the PDF to
            # the bridge on its 5-min tick; the pull job ingests the result
            # back. The fallback sweeper handles cases where Opus times out
            # or the routine reports failure. See bridge_ingestion_state
            # state machine for the full flow.
            if (
                triage.priority == "high"
                and settings.high_ingestion_backend == "opus_bridge"
            ):
                db.queue_for_opus_bridge(pdf_id)
                db.log_event(
                    pdf_id, "bridge", "queued",
                    f"HIGH → opus_bridge ({triage.source} / {triage.report_type})",
                )
                log.info(
                    f"Bridge: queued pdf_file_id={pdf_id} for Opus deep-analysis "
                    f"({file_name}, source={triage.source}, type={triage.report_type})"
                )
                # Status stays PROCESSING — will flip to PROCESSED when the
                # pull job ingests the Opus result OR the fallback sweeper
                # produces a Gemini analysis.
                # Local file stays on disk — dump job needs it; cleanup
                # happens after successful bridge commit.
                return None

            # Deep analysis — text-only by default; multimodal triggers
            # selectively for top-bank equity research / vol / derivatives
            # via _should_run_multimodal() in analyzer.py
            # pages from the triage extraction are reused — extract_pdf
            # skips its own re-extraction when they're passed (2026-08-20)
            extraction = await asyncio.to_thread(
                extract_pdf, local_path, None, pages)
            analysis = await analyze_pdf_deep(
                pdf_file_id=pdf_id,
                file_name=file_name,
                extraction=extraction,
                priority=triage.priority,
                source=triage.source,
                report_type=triage.report_type,
            )

        duration = time.time() - start_time

        # Store in database
        db.insert_analysis(
            pdf_file_id=pdf_id,
            triage_json=json.dumps(asdict(triage)),
            analysis_json=json.dumps(asdict(analysis)),
            priority=triage.priority,
            pages_analyzed=analysis.pages_analyzed,
            total_pages=analysis.total_pages,
            input_tokens=analysis.input_tokens + triage.input_tokens,
            output_tokens=analysis.output_tokens + triage.output_tokens,
            model=settings.gemini_model,
            duration=duration,
        )

        db.update_pdf_status(pdf_id, "PROCESSED")
        db.log_event(pdf_id, "process", "completed", f"Duration: {duration:.1f}s")

        # Clean up local PDF file to save disk space
        try:
            Path(local_path).unlink(missing_ok=True)
        except Exception:
            pass

        log.info(f"Processed {file_name} in {duration:.1f}s (priority={triage.priority})")
        return analysis

    except Exception as e:
        log.error(f"Failed to process {file_name}: {e}", exc_info=True)
        db.update_pdf_status(pdf_id, "FAILED", str(e)[:500])
        db.log_event(pdf_id, "process", "failed", str(e)[:500])
        return None


async def process_pending_queue() -> list[PdfAnalysis]:
    """Process all pending (DOWNLOADED) PDFs in the queue.

    Processes with priority ordering: smaller files first for faster coverage.
    Also retries eligible failed PDFs.
    """
    # Reap zombies first: rows stranded in PROCESSING by a worker
    # restart re-enter the queue as DOWNLOADED (bridge-owned rows are
    # exempt — their own watchdog handles them).
    try:
        reaped = db.reset_stale_processing(
            max_age_hours=2, max_retries=settings.max_retry_count
        )
        if reaped:
            log.warning(
                f"Stale-PROCESSING reaper: reset {len(reaped)} row(s): "
                + ", ".join(
                    f"{r['file_name']}→{r['new_status']}" for r in reaped[:10]
                )
            )
    except Exception as e:
        log.error(f"Stale-PROCESSING reaper failed: {e}", exc_info=True)

    # Get pending PDFs
    pending = db.get_pending_pdfs(limit=50)
    retryable = db.get_failed_pdfs_for_retry(max_retries=settings.max_retry_count)

    all_to_process = pending + retryable
    if not all_to_process:
        return []

    log.info(f"Processing queue: {len(pending)} pending, {len(retryable)} retrying")

    analyses = []
    for pdf_data in all_to_process:
        result = await process_single_pdf(pdf_data)
        if result:
            analyses.append(result)

    log.info(f"Queue processing complete: {len(analyses)}/{len(all_to_process)} succeeded")
    return analyses


def _load_analyses_from_db(rows: list[dict]) -> list[PdfAnalysis]:
    """Reconstruct PdfAnalysis objects from database rows."""
    analyses = []
    for row in rows:
        try:
            data = json.loads(row["analysis_json"])
            # Handle both dict-based and dataclass-based serialization
            if isinstance(data, dict) and "pdf_file_id" in data:
                from ai_analysis.models import (
                    MarketMover, SectorView, MacroIndicator, TradeIdea,
                    EntityMention, KeyDataPoint, TensionPoint, ThemeStance,
                )
                from ai_analysis.analyzer import _safe_dataclass

                def _build_list(cls, raw):
                    out = []
                    for item in raw or []:
                        if isinstance(item, dict):
                            obj = _safe_dataclass(cls, item)
                            if obj is not None:
                                out.append(obj)
                    return out

                # theme_stances: prefer the new structured field. Fall back to
                # legacy theme_tags (list[str]) for analyses produced before
                # the schema upgrade — convert each tag to a bare-stance entry.
                ts_raw = data.get("theme_stances")
                if ts_raw:
                    theme_stances = _build_list(ThemeStance, ts_raw)
                else:
                    theme_stances = [
                        ThemeStance(theme=t)
                        for t in (data.get("theme_tags") or [])
                        if isinstance(t, str) and t.strip()
                    ]

                analysis = PdfAnalysis(
                    pdf_file_id=data["pdf_file_id"],
                    file_name=data.get("file_name", row.get("file_name", "")),
                    source=data.get("source", "Unknown"),
                    title=data.get("title", ""),
                    report_type=data.get("report_type", "other"),
                    priority=data.get("priority", row.get("priority", "medium")),
                    key_insights=data.get("key_insights", []),
                    market_movers=_build_list(MarketMover, data.get("market_movers")),
                    sector_views=_build_list(SectorView, data.get("sector_views")),
                    earnings_insights=data.get("earnings_insights", []),
                    macro_indicators=_build_list(MacroIndicator, data.get("macro_indicators")),
                    crypto_views=data.get("crypto_views", []),
                    trade_ideas=_build_list(TradeIdea, data.get("trade_ideas")),
                    risk_factors=data.get("risk_factors", []),
                    charts_described=data.get("charts_described", []),
                    vol_and_positioning=data.get("vol_and_positioning", []),
                    geopolitical=data.get("geopolitical", []),
                    cross_bank_references=data.get("cross_bank_references", []),
                    entities_mentioned=_build_list(EntityMention, data.get("entities_mentioned")),
                    key_data_points=_build_list(KeyDataPoint, data.get("key_data_points")),
                    tension_points=_build_list(TensionPoint, data.get("tension_points")),
                    theme_stances=theme_stances,
                    contextual_mentions=[
                        m.strip() for m in (data.get("contextual_mentions") or [])
                        if isinstance(m, str) and m.strip()
                    ],
                    pages_analyzed=data.get("pages_analyzed", 0),
                    total_pages=data.get("total_pages", 0),
                    input_tokens=data.get("input_tokens", 0),
                    output_tokens=data.get("output_tokens", 0),
                    published_at=row.get("dropbox_modified_at"),
                )
                analyses.append(analysis)
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Failed to load analysis for row {row.get('id')}: {e}")
    return analyses


async def run_daily_pulse(bot=None) -> DailyReport | None:
    """Generate and optionally send the Daily Market Pulse.

    Grabs all analyses since the last report was generated.
    If no prior report exists, falls back to today's analyses.
    """
    log.info("=== Starting Daily Market Pulse generation ===")

    # Process remaining pending PDFs
    await process_pending_queue()

    # Load analyses since the last report
    last_report_time = db.get_last_report_time()
    if last_report_time:
        rows = db.get_analyses_since(last_report_time)
        log.info(f"Loading analyses since last report at {last_report_time}")
    else:
        # No prior daily report — use a rolling 24h window based on Dropbox upload time.
        # NOT pa.created_at (which would grab backfilled analyses regardless of upload age).
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        rows = db.get_analyses_since(cutoff)
        log.info(f"No prior report — loading analyses for PDFs uploaded since {cutoff}")

    if not rows:
        log.warning("No analyses available for daily pulse")
        return None

    analyses = _load_analyses_from_db(rows)
    if not analyses:
        log.warning("No valid analyses to synthesize")
        return None

    # Check for still-pending PDFs
    pending = db.get_pending_pdfs()
    pending_note = ""
    if pending:
        pending_note = f"\n\n*Note: {len(pending)} additional reports are still being processed.*"

    # Scheduled pulses pass yesterday's theme list as a "don't restate verbatim"
    # directive. DRAFT still writes its own narrative (not anchored on yesterday's
    # structure); the theme list is used to cull repeat chart observations and
    # recycled headlines. Manual /pulse remains standalone.
    report = await synthesize_daily_pulse(analyses, use_prev_context=True)
    if pending_note:
        report.markdown_content += pending_note

    # Store in database
    report_id = db.insert_daily_report(
        report_date=report.report_date,
        report_type="daily",
        report_json=json.dumps(report.raw_json),
        report_markdown=report.markdown_content,
        pdf_count=report.pdf_count,
        input_tokens=report.input_tokens,
        output_tokens=report.output_tokens,
    )

    # Send to every configured Discord channel
    if bot:
        channel_ids = settings.discord_channel_ids
        if not channel_ids:
            log.error("No DISCORD_CHANNEL_ID configured — skipping Discord delivery")
        from report.formatter import format_report_embeds
        from discord_bot.sender import send_embeds
        embeds = format_report_embeds(report)
        any_sent = False
        for cid in channel_ids:
            try:
                channel = bot.get_channel(cid)
                if channel:
                    success = await send_embeds(channel, embeds)
                    if success:
                        any_sent = True
                        log.info(f"Daily pulse sent to channel {cid} ({channel.name})")
                else:
                    log.error(f"Discord channel {cid} not found")
            except Exception as e:
                log.error(f"Failed to send daily pulse to channel {cid}: {e}", exc_info=True)
        if any_sent:
            db.mark_report_sent(report_id)

    log.info(f"=== Daily Market Pulse complete: {report.pdf_count} reports ===")
    return report


async def reanalyze_recent_pdfs(
    hours: int,
    progress_cb=None,
    priority_filter: list[str] | None = None,
    job_id: int | None = None,
) -> dict:
    """Re-run analysis on PDFs already in the DB within the window.

    Use case: refresh historical analyses against an improved prompt/schema
    without having to wait for new uploads. Each PDF is re-downloaded from
    Dropbox, re-analyzed (text-only, full document) with the current prompt,
    and a NEW row is appended to pdf_analyses. Old rows are preserved as
    history — the latest analysis wins in SELECT queries.

    Args:
        hours: lookback window by Dropbox upload date (1-168). Ignored when
            `job_id` is given (target list comes from the job row).
        progress_cb: optional async callback(stats, phase) for updates.
        priority_filter: optional list of priorities to include
            (e.g., ['high', 'medium']). None = include all (default).
            Useful for backfilling new prompt fields on HIGH+MED only,
            saving ~30-40% time + cost by skipping LOW. Ignored when
            `job_id` is given.
        job_id: optional persistent job row to resume. When given:
            - target_pdf_ids comes from the job row (not re-queried)
            - already-processed and already-failed PDFs are skipped (resume)
            - progress is persisted to DB after each PDF
            Use this for the scheduler-driven background path so a worker
            restart resumes where it left off. Pass None for the legacy
            in-process path.
    """
    from datetime import timedelta
    import json as _json

    if job_id is not None:
        job = db.get_reanalyze_job(job_id)
        if not job:
            raise ValueError(f"reanalyze job {job_id} not found")
        target_ids = _json.loads(job["target_pdf_ids"])
        already_processed = set(_json.loads(job["processed_pdf_ids"] or "[]"))
        already_failed = set(_json.loads(job["failed_pdf_ids"] or "[]"))
        already_bridge_queued = set(_json.loads(job.get("bridge_queued_pdf_ids") or "[]"))
        # Resume: skip PDFs that already finished one way or another.
        remaining_ids = [
            pid for pid in target_ids
            if pid not in already_processed
            and pid not in already_failed
            and pid not in already_bridge_queued
        ]
        if not remaining_ids:
            log.info(f"Reanalyze job {job_id} has no remaining PDFs — marking complete")
            db.complete_reanalyze_job(job_id)
            return {
                "target": len(target_ids), "processed": len(already_processed),
                "failed": len(already_failed), "bridge_queued": len(already_bridge_queued),
                "input_tokens": job["input_tokens"], "output_tokens": job["output_tokens"],
            }
        # Re-fetch the PDF rows for the remaining ids.
        placeholders = ",".join("?" * len(remaining_ids))
        rows = db.get_connection().execute(
            f"""SELECT id, dropbox_path, file_name, local_path, dropbox_rev,
                       file_size_bytes, dropbox_modified_at, status, priority
                FROM pdf_files WHERE id IN ({placeholders})
                ORDER BY dropbox_modified_at ASC""",
            tuple(remaining_ids),
        ).fetchall()
        to_process = [dict(r) for r in rows]
        # Seed the running state from the job's prior progress.
        processed_set = set(already_processed)
        failed_set = set(already_failed)
        bridge_set = set(already_bridge_queued)
        running_input_tokens = int(job["input_tokens"])
        running_output_tokens = int(job["output_tokens"])
    else:
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
        conn = db.get_connection()
        if priority_filter:
            placeholders = ",".join("?" * len(priority_filter))
            rows = conn.execute(
                f"""SELECT id, dropbox_path, file_name, local_path, dropbox_rev,
                           file_size_bytes, dropbox_modified_at, status, priority
                    FROM pdf_files
                    WHERE dropbox_modified_at > ?
                      AND LOWER(priority) IN ({placeholders})
                    ORDER BY dropbox_modified_at ASC""",
                (cutoff, *[p.lower() for p in priority_filter]),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, dropbox_path, file_name, local_path, dropbox_rev,
                          file_size_bytes, dropbox_modified_at, status, priority
                   FROM pdf_files
                   WHERE dropbox_modified_at > ?
                   ORDER BY dropbox_modified_at ASC""",
                (cutoff,),
            ).fetchall()
        to_process = [dict(r) for r in rows]
        processed_set = set()
        failed_set = set()
        bridge_set = set()
        running_input_tokens = 0
        running_output_tokens = 0

    stats = {
        "target": (
            db.get_reanalyze_job(job_id)["target_count"]
            if job_id is not None
            else len(to_process)
        ),
        "processed": len(processed_set),
        "failed": len(failed_set),
        "bridge_queued": len(bridge_set),
        "input_tokens": running_input_tokens,
        "output_tokens": running_output_tokens,
        "current_file": "",
        "recent_files": [],
    }

    if progress_cb:
        await progress_cb(stats, "starting")

    download_dir = Path(settings.pdf_download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    recent_files: list[str] = []

    for i, pdf_data in enumerate(to_process, start=1):
        pdf_id = int(pdf_data["id"])
        stats["current_file"] = pdf_data["file_name"]
        stats["recent_files"] = recent_files[-5:]
        if progress_cb:
            await progress_cb(stats, "processing")

        outcome: str | None = None  # 'processed' | 'failed' | 'bridge_queued'
        try:
            dropbox_path = pdf_data["dropbox_path"]
            if not dropbox_path:
                log.warning(f"Reanalyze: {pdf_data['file_name']} missing dropbox_path — skipping")
                outcome = "failed"
                recent_files.append(f"✗ {pdf_data['file_name'][:70]} (no dropbox path)")
            else:
                # Always re-download: the local file from the original processing is
                # long gone. Use a fresh path, deterministic per file_name.
                safe_name = pdf_data["file_name"].replace("/", "_")
                local_path = download_dir / safe_name
                if local_path.exists():
                    stem, suffix, counter = local_path.stem, local_path.suffix, 1
                    while local_path.exists():
                        local_path = download_dir / f"{stem}_{counter}{suffix}"
                        counter += 1

                log.info(f"Reanalyze: downloading {dropbox_path} -> {local_path}")
                await asyncio.to_thread(download_file, dropbox_path, local_path)
                if not local_path.exists():
                    log.error(f"Reanalyze: download reported success but file missing: {local_path}")
                    outcome = "failed"
                    recent_files.append(f"✗ {pdf_data['file_name'][:70]} (download missing)")
                else:
                    pdf_data["local_path"] = str(local_path)
                    analysis = await process_single_pdf(pdf_data)
                    if analysis:
                        outcome = "processed"
                        stats["input_tokens"] += analysis.input_tokens
                        stats["output_tokens"] += analysis.output_tokens
                        recent_files.append(f"✓ {pdf_data['file_name'][:70]} ({analysis.priority})")
                    else:
                        # None could mean (a) genuinely failed, or (b) routed to opus_bridge
                        # and waiting for the routine. Differentiate so /reanalyze stats
                        # don't count bridge-queued PDFs as failures.
                        bridge = db.get_bridge_state(pdf_id)
                        if bridge and bridge.get("status") in ("pending", "committed"):
                            outcome = "bridge_queued"
                            recent_files.append(f"⏳ {pdf_data['file_name'][:70]} (bridge queued)")
                        else:
                            outcome = "failed"
                            recent_files.append(f"✗ {pdf_data['file_name'][:70]} (failed)")
        except Exception as e:
            log.error(f"Reanalyze failed for {pdf_data['file_name']}: {e}", exc_info=True)
            outcome = "failed"
            recent_files.append(f"✗ {pdf_data['file_name'][:70]} (error)")

        # Reflect outcome in the running state and (when job-backed) persist.
        if outcome == "processed":
            processed_set.add(pdf_id)
            stats["processed"] = len(processed_set)
        elif outcome == "bridge_queued":
            bridge_set.add(pdf_id)
            stats["bridge_queued"] = len(bridge_set)
        else:
            failed_set.add(pdf_id)
            stats["failed"] = len(failed_set)

        if job_id is not None:
            # Persist progress AFTER each PDF so a worker restart resumes
            # cleanly. Pass full lists (the helper rewrites the column).
            input_delta = analysis.input_tokens if outcome == "processed" else 0
            output_delta = analysis.output_tokens if outcome == "processed" else 0
            db.update_reanalyze_job_progress(
                job_id,
                processed_pdf_ids=sorted(processed_set),
                failed_pdf_ids=sorted(failed_set),
                bridge_queued_pdf_ids=sorted(bridge_set),
                input_tokens_delta=input_delta,
                output_tokens_delta=output_delta,
            )

        # Push progress every 3 PDFs (or on the last)
        if progress_cb and (i % 3 == 0 or i == len(to_process)):
            stats["current_file"] = pdf_data["file_name"]
            stats["recent_files"] = recent_files[-5:]
            await progress_cb(stats, "processing")

    if progress_cb:
        stats["current_file"] = ""
        stats["recent_files"] = recent_files[-5:]
        await progress_cb(stats, "done")

    if job_id is not None:
        db.complete_reanalyze_job(job_id)

    log.info(f"Reanalyze complete: {stats}")
    return stats


async def ingest_recent_pdfs(
    hours: int,
    progress_cb=None,
) -> dict:
    """Ingest PDFs from Dropbox uploaded in the last N hours.

    Args:
        hours: lookback window in hours.
        progress_cb: optional async callback(stats, phase) for periodic updates.
                     phase is one of 'listing', 'processing', 'done'.

    Returns a stats dict: {found, new, processed, skipped_low, failed, input_tokens, output_tokens}.
    """
    from datetime import timedelta
    since = datetime.utcnow() - timedelta(hours=hours)
    log.info(f"Ingest: listing Dropbox files since {since.isoformat()}")

    stats = {"found": 0, "new": 0, "processed": 0, "skipped_low": 0,
             "failed": 0, "input_tokens": 0, "output_tokens": 0}

    if progress_cb:
        await progress_cb(stats, "listing")

    files = await asyncio.to_thread(list_folder_files, settings.dropbox_folder_path, since)
    stats["found"] = len(files)

    download_dir = Path(settings.pdf_download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    # Filter to genuinely new files up front so "new" count is accurate in progress
    to_process = [f for f in files if not db.get_pdf_by_path(f.path)]
    stats["new"] = len(to_process)

    if progress_cb:
        await progress_cb(stats, "processing")

    # Track recently-processed filenames so the progress message can show them
    recent_files: list[str] = []

    for i, entry in enumerate(to_process, start=1):
        local_path = download_dir / entry.name.replace("/", "_")
        if local_path.exists():
            stem, suffix, counter = local_path.stem, local_path.suffix, 1
            while local_path.exists():
                local_path = download_dir / f"{stem}_{counter}{suffix}"
                counter += 1

        stats["current_file"] = entry.name
        stats["recent_files"] = recent_files[-5:]  # last 5 processed
        if progress_cb:
            await progress_cb(stats, "processing")

        try:
            await asyncio.to_thread(download_file, entry.path, local_path)
            pdf_id = db.insert_pdf_file(
                dropbox_path=entry.path, file_name=entry.name,
                local_path=str(local_path), dropbox_rev=entry.rev,
                file_size_bytes=entry.size, dropbox_modified_at=entry.server_modified,
            )
            db.log_event(pdf_id, "download", "completed")

            pdf_data = db.get_pdf_by_path(entry.path)
            analysis = await process_single_pdf(pdf_data)
            if analysis:
                stats["processed"] += 1
                stats["input_tokens"] += analysis.input_tokens
                stats["output_tokens"] += analysis.output_tokens
                if analysis.priority == "low":
                    stats["skipped_low"] += 1
                recent_files.append(f"✓ {entry.name[:70]} ({analysis.priority})")
            else:
                # Differentiate bridge-queued from genuine failure (see /reanalyze)
                bridge = db.get_bridge_state(int(pdf_data["id"]))
                if bridge and bridge.get("status") in ("pending", "committed"):
                    stats["bridge_queued"] = stats.get("bridge_queued", 0) + 1
                    recent_files.append(f"⏳ {entry.name[:70]} (bridge queued)")
                else:
                    stats["failed"] += 1
                    recent_files.append(f"✗ {entry.name[:70]} (failed)")
        except Exception as e:
            log.error(f"Ingest failed for {entry.name}: {e}")
            stats["failed"] += 1
            recent_files.append(f"✗ {entry.name[:70]} (error)")

    if progress_cb:
        await progress_cb(stats, "done")

    log.info(f"Ingest complete: {stats}")
    return stats


def seed_dropbox_cursor_to_now() -> str:
    """Seed Dropbox cursor to current state without enumerating files.

    After this, the next watcher poll will only see NEW uploads.
    Returns the cursor timestamp.
    """
    dbx = _get_client()
    result = dbx.files_list_folder_get_latest_cursor(
        settings.dropbox_folder_path, recursive=True
    )
    db.update_dropbox_cursor(result.cursor)
    ts = datetime.utcnow().isoformat()
    log.info(f"Dropbox cursor seeded to current state at {ts}")
    return ts


async def run_manual_pulse(
    since: str | None = None,
    until: str | None = None,
    persist: bool = True,
    progress_cb=None,
) -> DailyReport | None:
    """Run a pulse generation manually with optional time window.

    Args:
        since: ISO datetime string for start of window (e.g. "2026-04-07T00:00:00")
        until: ISO datetime string for end of window (e.g. "2026-04-08T00:00:00")
        persist: If True, save the report to daily_reports. Set False for dry-run tests.
        progress_cb: optional async callback(phase, detail) for periodic updates.

    If neither since nor until is provided, behaves like the scheduled pulse (since last report).
    If only `since` is provided, gets everything from that time to now.
    If both are provided, gets everything in that window.

    Note: unlike the scheduled pulse, this does NOT process the pending queue first.
    /pulse is a fast preview of what's already analyzed in the DB.
    """
    async def _emit(phase: str, detail: str = ""):
        if progress_cb:
            try:
                await progress_cb(phase, detail)
            except Exception:
                pass

    await _emit("loading", "Loading analyses from DB…")

    if since and until:
        rows = db.get_analyses_between(since, until)
    elif since:
        rows = db.get_analyses_since(since)
    else:
        # Default for manual /pulse: always last 24h of uploads.
        # This is independent of the scheduled pulse cadence — manual pulses
        # are on-demand snapshots, not continuity-tracking.
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        rows = db.get_analyses_since(cutoff)

    if not rows:
        return None

    analyses = _load_analyses_from_db(rows)
    if not analyses:
        return None

    await _emit("synthesizing", f"Synthesizing {len(analyses)} reports (fetching live market data + calling Gemini)…")

    # Manual /pulse is fully standalone — no diff framing vs prior pulses.
    report = await synthesize_daily_pulse(analyses, use_prev_context=False)

    await _emit("persisting", "Saving report + preparing Discord embeds…")

    if persist:
        # Manual pulses use report_type="manual" so they don't affect the
        # scheduled pulse's "since last report" cutoff (which filters on "daily").
        report_id = db.insert_daily_report(
            report_date=report.report_date,
            report_type="manual",
            report_json=json.dumps(report.raw_json),
            report_markdown=report.markdown_content,
            pdf_count=report.pdf_count,
            input_tokens=report.input_tokens,
            output_tokens=report.output_tokens,
        )
        report.report_id = report_id

    return report
