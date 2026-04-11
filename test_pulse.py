"""CLI tool for manually testing Market Pulse generation.

Usage:
    python test_pulse.py                          # Process all Dropbox PDFs, print to terminal
    python test_pulse.py --folder "/Research"      # Specific Dropbox subfolder
    python test_pulse.py --limit 5                 # Only process 5 PDFs
    python test_pulse.py --type afternoon           # Generate afternoon-style pulse
    python test_pulse.py --dry-run                 # Print report to terminal only
    python test_pulse.py --send                    # Also send to Discord
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Setup logging before imports
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("test_pulse")


async def run_test(args: argparse.Namespace) -> None:
    from config import settings
    from dropbox_client.watcher import list_folder_files, download_file
    from pdf_processing.extractor import extract_text_per_page, extract_pdf
    from pdf_processing.page_selector import select_pages
    from ai_analysis.analyzer import triage_pdf, analyze_pdf_deep
    from ai_analysis.models import PdfAnalysis
    from report.synthesizer import synthesize_morning_pulse, synthesize_afternoon_pulse
    from report.formatter import format_report_plain
    import db

    # Initialize database
    db.get_connection()

    # Step 1: Get PDFs from Dropbox
    folder = args.folder or settings.dropbox_folder_path
    log.info(f"Listing PDFs in Dropbox folder: {folder}")

    files = list_folder_files(folder)
    log.info(f"Found {len(files)} PDFs")

    if args.limit:
        files = files[:args.limit]
        log.info(f"Limited to {len(files)} PDFs")

    if not files:
        print("\nNo PDFs found in the specified folder.")
        return

    # Step 2: Download and process each PDF
    download_dir = Path(settings.pdf_download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    analyses: list[PdfAnalysis] = []

    for i, entry in enumerate(files):
        print(f"\n--- [{i+1}/{len(files)}] {entry.name} ({entry.size:,} bytes) ---")

        # Download
        local_path = download_dir / entry.name.replace("/", "_")
        if not local_path.exists():
            download_file(entry.path, local_path)
        else:
            log.info(f"Already downloaded: {local_path}")

        # Extract text
        log.info("Extracting text...")
        pages = extract_text_per_page(str(local_path))
        full_text = "\n".join(p.text for p in pages)

        # Triage
        log.info("Running triage...")
        triage = await triage_pdf(entry.name, full_text)
        print(f"  Priority: {triage.priority} | Type: {triage.report_type}")
        print(f"  Tickers: {', '.join(triage.key_tickers) if triage.key_tickers else 'none'}")
        print(f"  Summary: {triage.summary[:200]}")

        if triage.priority == "low" and not args.force_all:
            log.info("Skipping low-priority PDF (use --force-all to include)")
            continue

        # Select pages and extract
        log.info("Selecting pages for analysis...")
        selected = select_pages(pages)
        extraction = extract_pdf(str(local_path), selected if triage.priority == "high" else None)

        # Deep analysis
        log.info(f"Deep analyzing ({'multimodal' if triage.priority == 'high' else 'text-only'})...")
        analysis = await analyze_pdf_deep(
            pdf_file_id=i + 1,
            file_name=entry.name,
            extraction=extraction,
            priority=triage.priority,
        )
        analyses.append(analysis)

        print(f"  Source: {analysis.source}")
        print(f"  Title: {analysis.title}")
        print(f"  Insights: {len(analysis.key_insights)}")
        print(f"  Tokens: {analysis.input_tokens:,} in / {analysis.output_tokens:,} out")

    if not analyses:
        print("\nNo analyses generated. Nothing to synthesize.")
        return

    # Step 3: Synthesize report
    print(f"\n{'=' * 60}")
    print(f"Synthesizing {args.type} Market Pulse from {len(analyses)} analyses...")
    print(f"{'=' * 60}\n")

    if args.type == "morning":
        report = await synthesize_morning_pulse(analyses)
    else:
        report = await synthesize_afternoon_pulse(analyses)

    # Step 4: Output
    messages = format_report_plain(report)
    for msg in messages:
        print(msg)

    print(f"\n{'=' * 60}")
    print(f"Total PDFs analyzed: {report.pdf_count}")
    print(f"Synthesis tokens: {report.input_tokens:,} in / {report.output_tokens:,} out")

    total_in = sum(a.input_tokens for a in analyses) + report.input_tokens
    total_out = sum(a.output_tokens for a in analyses) + report.output_tokens
    print(f"Total tokens used: {total_in:,} in / {total_out:,} out")
    print(f"{'=' * 60}")

    # Optionally send to Discord
    if args.send:
        log.info("Sending to Discord...")
        import discord as discord_lib
        from discord_bot.sender import send_embeds
        from report.formatter import format_report_embeds

        intents = discord_lib.Intents.default()
        client = discord_lib.Client(intents=intents)

        @client.event
        async def on_ready():
            channel = client.get_channel(settings.discord_channel_id)
            if channel:
                embeds = format_report_embeds(report)
                await send_embeds(channel, embeds)
                print(f"\nSent to Discord channel: {channel.name}")
            else:
                print(f"\nCould not find channel {settings.discord_channel_id}")
            await client.close()

        await client.start(settings.discord_bot_token)


def main():
    parser = argparse.ArgumentParser(description="Test Market Pulse generation")
    parser.add_argument("--folder", type=str, help="Dropbox folder path to scan")
    parser.add_argument("--limit", type=int, help="Max number of PDFs to process")
    parser.add_argument(
        "--type", choices=["morning", "afternoon"], default="morning",
        help="Pulse type to generate (default: morning)"
    )
    parser.add_argument("--dry-run", action="store_true", help="Print only, no Discord")
    parser.add_argument("--send", action="store_true", help="Send result to Discord")
    parser.add_argument("--force-all", action="store_true", help="Include low-priority PDFs")

    args = parser.parse_args()

    if args.dry_run:
        args.send = False

    asyncio.run(run_test(args))


if __name__ == "__main__":
    main()
