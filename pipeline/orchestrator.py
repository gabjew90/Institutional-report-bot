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
from pdf_processing.extractor import extract_text_per_page, extract_pdf
from pdf_processing.page_selector import select_pages
from report.synthesizer import synthesize_daily_pulse
from report.models import DailyReport
from config import settings
import db

log = logging.getLogger(__name__)


async def process_single_pdf(pdf_data: dict) -> PdfAnalysis | None:
    """Process a single PDF through the full pipeline.

    triage (Gemini) → page selection → extraction → deep analysis (Gemini)
    """
    pdf_id = pdf_data["id"]
    file_name = pdf_data["file_name"]
    local_path = pdf_data["local_path"]

    if not local_path or not Path(local_path).exists():
        log.error(f"PDF file not found: {local_path}")
        db.update_pdf_status(pdf_id, "FAILED", "Local file not found")
        return None

    try:
        db.update_pdf_status(pdf_id, "PROCESSING")
        db.log_event(pdf_id, "process", "started")
        start_time = time.time()

        # Step 1: Extract text from all pages
        pages = extract_text_per_page(local_path)
        full_text = "\n".join(p.text for p in pages)

        # Step 2: Triage with Gemini
        triage = await triage_pdf(file_name, full_text)
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
            # Select pages for analysis
            selected_pages = select_pages(pages)

            # For HIGH priority: render images of selected pages
            # For MEDIUM: text-only
            render_images = triage.priority == "high"
            extraction = extract_pdf(
                local_path,
                selected_pages if render_images else None,
            )

            # Deep analysis
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
                analysis = PdfAnalysis(
                    pdf_file_id=data["pdf_file_id"],
                    file_name=data.get("file_name", row.get("file_name", "")),
                    source=data.get("source", "Unknown"),
                    title=data.get("title", ""),
                    report_type=data.get("report_type", "other"),
                    priority=data.get("priority", row.get("priority", "medium")),
                    key_insights=data.get("key_insights", []),
                    market_movers=[MarketMover(**mm) for mm in data.get("market_movers", []) if isinstance(mm, dict)],
                    sector_views=[SectorView(**sv) for sv in data.get("sector_views", []) if isinstance(sv, dict)],
                    earnings_insights=data.get("earnings_insights", []),
                    macro_indicators=[MacroIndicator(**mi) for mi in data.get("macro_indicators", []) if isinstance(mi, dict)],
                    crypto_views=data.get("crypto_views", []),
                    trade_ideas=[TradeIdea(**ti) for ti in data.get("trade_ideas", []) if isinstance(ti, dict)],
                    risk_factors=data.get("risk_factors", []),
                    charts_described=data.get("charts_described", []),
                    pages_analyzed=data.get("pages_analyzed", 0),
                    total_pages=data.get("total_pages", 0),
                    input_tokens=data.get("input_tokens", 0),
                    output_tokens=data.get("output_tokens", 0),
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
        rows = db.get_todays_analyses()
        log.info("No prior report found, loading today's analyses")

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

    # Send to Discord
    if bot:
        try:
            channel = bot.get_channel(settings.discord_channel_id)
            if channel:
                from report.formatter import format_report_embeds
                from discord_bot.sender import send_embeds
                embeds = format_report_embeds(report)
                success = await send_embeds(channel, embeds)
                if success:
                    db.mark_report_sent(report_id)
                    log.info("Daily pulse sent to Discord")
            else:
                log.error(f"Discord channel {settings.discord_channel_id} not found")
        except Exception as e:
            log.error(f"Failed to send daily pulse to Discord: {e}", exc_info=True)

    log.info(f"=== Daily Market Pulse complete: {report.pdf_count} reports ===")
    return report


async def run_manual_pulse(
    since: str | None = None,
    until: str | None = None,
    persist: bool = True,
) -> DailyReport | None:
    """Run a pulse generation manually with optional time window.

    Args:
        since: ISO datetime string for start of window (e.g. "2026-04-07T00:00:00")
        until: ISO datetime string for end of window (e.g. "2026-04-08T00:00:00")
        persist: If True, save the report to daily_reports. Set False for dry-run tests.

    If neither since nor until is provided, behaves like the scheduled pulse (since last report).
    If only `since` is provided, gets everything from that time to now.
    If both are provided, gets everything in that window.
    """
    # Process pending first
    await process_pending_queue()

    if since and until:
        rows = db.get_analyses_between(since, until)
    elif since:
        rows = db.get_analyses_since(since)
    else:
        # Same logic as scheduled: since last report, or today
        last_report_time = db.get_last_report_time()
        if last_report_time:
            rows = db.get_analyses_since(last_report_time)
        else:
            rows = db.get_todays_analyses()

    if not rows:
        return None

    analyses = _load_analyses_from_db(rows)
    if not analyses:
        return None

    report = await synthesize_daily_pulse(analyses)

    if persist:
        report_id = db.insert_daily_report(
            report_date=report.report_date,
            report_type="daily",
            report_json=json.dumps(report.raw_json),
            report_markdown=report.markdown_content,
            pdf_count=report.pdf_count,
            input_tokens=report.input_tokens,
            output_tokens=report.output_tokens,
        )
        report.report_id = report_id

    return report
