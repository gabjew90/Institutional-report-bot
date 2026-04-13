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

from ai_analysis.analyzer import triage_pdf, analyze_pdf_deep, analyze_batch
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
            # Store triage result directly as the analysis
            analysis = PdfAnalysis(
                pdf_file_id=pdf_id,
                file_name=file_name,
                source="Unknown",
                title=file_name,
                report_type=triage.report_type,
                priority="low",
                key_insights=[triage.summary] if triage.summary else [],
                total_pages=len(pages),
                input_tokens=triage.input_tokens,
                output_tokens=triage.output_tokens,
            )
        else:
            # Text-only deep analysis — full document, no image rendering
            extraction = await asyncio.to_thread(extract_pdf, local_path, None)
            analysis = await analyze_pdf_deep(
                pdf_file_id=pdf_id,
                file_name=file_name,
                extraction=extraction,
                priority=triage.priority,
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
                from ai_analysis.models import MarketMover, SectorView, MacroIndicator, TradeIdea
                from ai_analysis.analyzer import _safe_dataclass

                def _build_list(cls, raw):
                    out = []
                    for item in raw or []:
                        if isinstance(item, dict):
                            obj = _safe_dataclass(cls, item)
                            if obj is not None:
                                out.append(obj)
                    return out

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

    # Synthesize
    report = await synthesize_daily_pulse(analyses)
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
) -> dict:
    """Re-run analysis on PDFs already in the DB within the window.

    Use case: refresh historical analyses against an improved prompt/schema
    without having to wait for new uploads. Each PDF is re-downloaded from
    Dropbox, re-analyzed (text-only, full document) with the current prompt,
    and a NEW row is appended to pdf_analyses. Old rows are preserved as
    history — the latest analysis wins in SELECT queries.

    Args:
        hours: lookback window by Dropbox upload date (1-48).
        progress_cb: optional async callback(stats, phase) for updates.
    """
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

    conn = db.get_connection()
    rows = conn.execute(
        """SELECT id, dropbox_path, file_name, local_path, dropbox_rev,
                  file_size_bytes, dropbox_modified_at, status, priority
           FROM pdf_files
           WHERE dropbox_modified_at > ?
           ORDER BY dropbox_modified_at ASC""",
        (cutoff,),
    ).fetchall()
    to_process = [dict(r) for r in rows]

    stats = {
        "target": len(to_process), "processed": 0, "failed": 0,
        "input_tokens": 0, "output_tokens": 0,
        "current_file": "", "recent_files": [],
    }

    if progress_cb:
        await progress_cb(stats, "starting")

    download_dir = Path(settings.pdf_download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)
    recent_files: list[str] = []

    for i, pdf_data in enumerate(to_process, start=1):
        stats["current_file"] = pdf_data["file_name"]
        stats["recent_files"] = recent_files[-5:]
        if progress_cb:
            await progress_cb(stats, "processing")

        try:
            dropbox_path = pdf_data["dropbox_path"]
            if not dropbox_path:
                log.warning(f"Reanalyze: {pdf_data['file_name']} missing dropbox_path — skipping")
                stats["failed"] += 1
                recent_files.append(f"✗ {pdf_data['file_name'][:70]} (no dropbox path)")
                continue

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
                stats["failed"] += 1
                recent_files.append(f"✗ {pdf_data['file_name'][:70]} (download missing)")
                continue

            pdf_data["local_path"] = str(local_path)
            analysis = await process_single_pdf(pdf_data)
            if analysis:
                stats["processed"] += 1
                stats["input_tokens"] += analysis.input_tokens
                stats["output_tokens"] += analysis.output_tokens
                recent_files.append(f"✓ {pdf_data['file_name'][:70]} ({analysis.priority})")
            else:
                stats["failed"] += 1
                recent_files.append(f"✗ {pdf_data['file_name'][:70]} (failed)")
        except Exception as e:
            log.error(f"Reanalyze failed for {pdf_data['file_name']}: {e}", exc_info=True)
            stats["failed"] += 1
            recent_files.append(f"✗ {pdf_data['file_name'][:70]} (error)")

        # Push progress every 3 PDFs (or on the last)
        if progress_cb and (i % 3 == 0 or i == len(to_process)):
            stats["current_file"] = pdf_data["file_name"]
            stats["recent_files"] = recent_files[-5:]
            await progress_cb(stats, "processing")

    if progress_cb:
        stats["current_file"] = ""
        stats["recent_files"] = recent_files[-5:]
        await progress_cb(stats, "done")

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
        # Same logic as scheduled: since last daily report, or last 24h of uploads
        last_report_time = db.get_last_report_time()
        if last_report_time:
            rows = db.get_analyses_since(last_report_time)
        else:
            from datetime import timedelta
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            rows = db.get_analyses_since(cutoff)

    if not rows:
        return None

    analyses = _load_analyses_from_db(rows)
    if not analyses:
        return None

    await _emit("synthesizing", f"Synthesizing {len(analyses)} reports (fetching live market data + calling Gemini)…")

    report = await synthesize_daily_pulse(analyses)

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
