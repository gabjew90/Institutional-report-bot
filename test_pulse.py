"""CLI tool for manually triggering a Market Pulse.

Uses the same code path as the scheduled daily pulse and the Discord /pulse command.

Usage:
    python test_pulse.py --since 24h --dry-run        # Last 24 hours, print only
    python test_pulse.py --since 48h --dry-run        # Last 48 hours
    python test_pulse.py --since 2026-04-07 --dry-run # Since Monday
    python test_pulse.py --since 2026-04-07 --until 2026-04-08  # Monday only
    python test_pulse.py --dry-run                    # Since last report (default)
    python test_pulse.py --send                       # Generate and send to Discord
    python test_pulse.py --ingest --limit 20          # Ingest 20 new PDFs from Dropbox first, then generate
"""

import argparse
import asyncio
import io
import logging
import sys
from datetime import datetime, timedelta

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_pulse")


def _parse_time(value: str) -> str:
    """Parse '24h', '48h', or ISO date string into an ISO datetime string."""
    if value.endswith("h"):
        hours = int(value.rstrip("h"))
        return (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    # If just a date like '2026-04-07', treat as start of that day
    if len(value) == 10:
        return value + "T00:00:00"
    return value


async def run_ingest(since_str: str | None = None, until_str: str | None = None, force_all: bool = False):
    """Ingest new PDFs from Dropbox: download, triage, analyze, store in DB."""
    from config import settings
    from dropbox_client.watcher import list_folder_files, download_file
    from pdf_processing.extractor import extract_text_per_page, extract_pdf
    from ai_analysis.analyzer import triage_pdf, analyze_pdf_deep
    from pathlib import Path
    import db

    db.get_connection()

    folder = settings.dropbox_folder_path
    since_dt = datetime.fromisoformat(_parse_time(since_str)) if since_str else datetime.utcnow() - timedelta(hours=48)
    until_dt = datetime.fromisoformat(_parse_time(until_str)) if until_str else None

    label = f"since {since_dt.isoformat()}" + (f" until {until_dt.isoformat()}" if until_dt else "")
    log.info(f"Listing PDFs in Dropbox {label}")

    files = list_folder_files(folder, since=since_dt, until=until_dt)
    log.info(f"Found {len(files)} PDFs matching time range")

    download_dir = Path(settings.pdf_download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    processed = 0
    for i, entry in enumerate(files):
        # Skip if already in DB
        existing = db.get_pdf_by_path(entry.path)
        if existing:
            continue

        print(f"\n--- [{i+1}/{len(files)}] {entry.name} ---")

        # Download
        local_path = download_dir / entry.name.replace("/", "_")
        if not local_path.exists():
            download_file(entry.path, local_path)

        # Extract text
        pages = extract_text_per_page(str(local_path))
        full_text = "\n".join(p.text for p in pages)

        # Triage
        folder_path = str(Path(entry.path).parent)
        triage = await triage_pdf(entry.name, full_text, folder_path=folder_path)
        print(f"  Priority: {triage.priority} | Type: {triage.report_type}")
        print(f"  Summary: {triage.summary[:150]}")

        if triage.priority == "low" and not force_all:
            log.info("Skipping low-priority PDF")
            # Still register in DB so we don't re-download
            pdf_id = db.insert_pdf_file(
                dropbox_path=entry.path, file_name=entry.name,
                local_path=str(local_path), dropbox_rev=entry.rev,
                file_size_bytes=entry.size, dropbox_modified_at=entry.server_modified,
            )
            db.update_pdf_status(pdf_id, "PROCESSED")
            db.update_pdf_priority(pdf_id, "low")
            continue

        extraction = extract_pdf(str(local_path), None)

        pdf_id = db.insert_pdf_file(
            dropbox_path=entry.path, file_name=entry.name,
            local_path=str(local_path), dropbox_rev=entry.rev,
            file_size_bytes=entry.size, dropbox_modified_at=entry.server_modified,
        )

        analysis = await analyze_pdf_deep(
            pdf_file_id=pdf_id, file_name=entry.name,
            extraction=extraction, priority=triage.priority,
        )

        # Store in DB
        import json
        from dataclasses import asdict
        db.insert_analysis(
            pdf_file_id=pdf_id,
            triage_json=json.dumps({"priority": triage.priority, "report_type": triage.report_type,
                                    "key_tickers": triage.key_tickers, "summary": triage.summary}),
            analysis_json=json.dumps(asdict(analysis)),
            priority=triage.priority,
            pages_analyzed=len(selected),
            total_pages=len(pages),
            input_tokens=analysis.input_tokens,
            output_tokens=analysis.output_tokens,
            model="gemini",
            duration=0,
        )
        db.update_pdf_status(pdf_id, "PROCESSED")
        db.update_pdf_priority(pdf_id, triage.priority)

        print(f"  Source: {analysis.source} | Title: {analysis.title}")
        print(f"  Tokens: {analysis.input_tokens:,} in / {analysis.output_tokens:,} out")
        processed += 1

    print(f"\nIngested {processed} new PDFs into database.")


async def run_pulse(args: argparse.Namespace):
    """Generate a pulse using the same path as production."""
    from pipeline.orchestrator import run_manual_pulse
    from report.formatter import format_report_plain
    import db

    db.get_connection()

    # Parse time args
    since = _parse_time(args.since) if args.since else None
    until = _parse_time(args.until) if args.until else None

    if since:
        log.info(f"Generating pulse: since={since}" + (f", until={until}" if until else ""))
    else:
        log.info("Generating pulse: since last report")

    # Only persist to daily_reports when actually sending (not dry-run)
    report = await run_manual_pulse(since=since, until=until, persist=args.send)

    if not report:
        print("\nNo analyses found for the specified time range.")
        print("Run with --ingest first to download and analyze PDFs from Dropbox.")
        return

    # Print report
    messages = format_report_plain(report)
    for msg in messages:
        print(msg)

    print(f"\n{'=' * 60}")
    print(f"PDFs in report: {report.pdf_count}")
    print(f"Synthesis tokens: {report.input_tokens:,} in / {report.output_tokens:,} out")
    print(f"{'=' * 60}")

    # Send to Discord
    if args.send:
        from config import settings
        log.info("Sending to Discord...")
        import discord as discord_lib
        from discord_bot.sender import send_embeds
        from report.formatter import format_report_embeds

        intents = discord_lib.Intents.default()
        client = discord_lib.Client(intents=intents)

        @client.event
        async def on_ready():
            embeds = format_report_embeds(report)
            any_sent = False
            for cid in settings.discord_channel_ids:
                channel = client.get_channel(cid)
                if channel:
                    success = await send_embeds(channel, embeds)
                    if success:
                        any_sent = True
                        print(f"Sent to Discord channel: {channel.name} ({cid})")
                else:
                    print(f"Could not find channel {cid}")
            if any_sent and report.report_id:
                db.mark_report_sent(report.report_id)
            await client.close()

        await client.start(settings.discord_bot_token)


async def run(args: argparse.Namespace):
    if args.ingest:
        await run_ingest(since_str=args.since, until_str=args.until, force_all=args.force_all)
    await run_pulse(args)


def main():
    parser = argparse.ArgumentParser(description="Test Market Pulse generation")
    parser.add_argument("--since", type=str, help="Start time: '24h', '48h', or ISO date like '2026-04-07'")
    parser.add_argument("--until", type=str, help="End time: ISO date (default: now)")
    parser.add_argument("--ingest", action="store_true", help="Ingest new PDFs from Dropbox before generating")
    parser.add_argument("--dry-run", action="store_true", help="Print only, don't send to Discord")
    parser.add_argument("--send", action="store_true", help="Send result to Discord")
    parser.add_argument("--force-all", action="store_true", help="Include low-priority PDFs")

    args = parser.parse_args()

    if args.dry_run:
        args.send = False

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
