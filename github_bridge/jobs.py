"""Scheduled jobs that drive the GitHub-as-bridge pulse pipeline.

Two jobs:

1. dump_context_job — periodically (~15 min) build the synthesis context and
   write it to `pulse-context/latest.json` on the bridge branch. The Opus
   routine reads this file via raw.githubusercontent.com when it fires.

2. post_pending_pulses_job — periodically (~60s) poll
   `pulse-output/pending/*.md` on the bridge branch. For each new pulse
   markdown: post it to all configured Discord channels via the existing
   formatter+sender chain, save to daily_reports, then move the file from
   pending/ to archive/<date>.md to mark it posted.

These jobs only run when GITHUB_TOKEN, GITHUB_REPO, and GITHUB_BRIDGE_BRANCH
are configured. Empty token = bridge disabled.
"""

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from config import settings
from github_bridge import client as gh
from pipeline.orchestrator import _load_analyses_from_db
from report.synthesizer import build_pulse_context
from report.formatter import format_report_embeds
from report.models import DailyReport
from discord_bot.sender import send_embeds
import db

log = logging.getLogger(__name__)

CONTEXT_PATH = "pulse-context/latest.json"
PENDING_DIR = "pulse-output/pending"
ARCHIVE_DIR = "pulse-output/archive"


def bridge_enabled() -> bool:
    return bool(
        settings.github_token
        and settings.github_repo
        and settings.github_bridge_branch
    )


# -----------------------------------------------------------------------------
# JOB 1 — dump synthesis context to GitHub
# -----------------------------------------------------------------------------


def dump_context_job() -> None:
    """Build the pulse context (24h window) and commit JSON to the bridge.

    Runs in the APScheduler thread. Synchronous because build_pulse_context is
    sync and gh.put_file is sync. Wrapped in try/except so a transient GitHub
    blip doesn't crash the scheduler.
    """
    if not bridge_enabled():
        return
    try:
        gh.ensure_branch_exists()

        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        rows = db.get_analyses_since(cutoff)
        if not rows:
            log.info("Bridge: no analyses in 24h window — skipping context dump")
            return

        analyses = _load_analyses_from_db(rows)
        if not analyses:
            log.info("Bridge: no parseable analyses — skipping context dump")
            return

        ctx = build_pulse_context(analyses, use_prev_context=True)
        ctx["dumped_at_utc"] = datetime.utcnow().isoformat() + "Z"
        ctx["window_hours"] = 24

        body = json.dumps(ctx, indent=1)
        msg = f"bridge: dump pulse-context ({ctx['pdf_count']} PDFs, {ctx['today_label']})"
        result = gh.put_file(CONTEXT_PATH, body, msg)
        sha = (result.get("commit") or {}).get("sha", "")[:8]
        log.info(
            f"Bridge: dumped pulse-context — {ctx['pdf_count']} PDFs, {len(body) // 1024}KB, commit {sha}"
        )
    except Exception as e:
        log.error(f"Bridge: dump_context_job failed: {e}", exc_info=True)


# -----------------------------------------------------------------------------
# JOB 2 — poll pending pulses, post to Discord, archive
# -----------------------------------------------------------------------------


async def post_pending_pulses_job(bot=None) -> None:
    """Poll `pulse-output/pending/`, post each new .md to Discord, archive.

    Async because send_embeds is async (uses discord.py).
    """
    if not bridge_enabled():
        return
    if bot is None:
        # Race during startup — bot reference not yet attached. Skip and retry
        # next interval; not worth crashing.
        log.debug("Bridge: post_pending called without bot — skipping")
        return

    try:
        items = gh.list_dir(PENDING_DIR)
    except Exception as e:
        log.warning(f"Bridge: failed to list {PENDING_DIR}: {e}")
        return

    md_files = [it for it in items if it.get("name", "").endswith(".md") and it.get("type") == "file"]
    if not md_files:
        return

    # Sort oldest first by name (timestamps in filenames sort lexically)
    md_files.sort(key=lambda it: it.get("name", ""))

    log.info(f"Bridge: found {len(md_files)} pending pulse(s) to post")

    for item in md_files:
        await _process_one_pulse(bot, item)


async def _process_one_pulse(bot, item: dict[str, Any]) -> None:
    name = item.get("name", "")
    pending_path = f"{PENDING_DIR}/{name}"
    archive_path = f"{ARCHIVE_DIR}/{name}"

    try:
        markdown = gh.get_file_text(pending_path)
        if not markdown:
            log.warning(f"Bridge: empty pending file {pending_path} — skipping")
            return

        # Build a DailyReport for the existing formatter chain. We don't have
        # a reliable pdf_count from the routine output, so derive from filename
        # prefix if it includes one, else 0.
        today = date.today().isoformat()
        pdf_count = 0  # not authoritative; routine may include it in markdown header
        report = DailyReport(
            report_date=today,
            report_type="daily",  # routine pulses replace the scheduled Gemini one
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json={"source": "github_bridge", "pending_file": name},
            input_tokens=0,
            output_tokens=0,
            stats={},
        )

        # Persist before posting so we don't lose track if Discord errors
        report_id = db.insert_daily_report(
            report_date=today,
            report_type="daily",
            report_json=json.dumps(report.raw_json),
            report_markdown=markdown,
            pdf_count=pdf_count,
            input_tokens=0,
            output_tokens=0,
        )

        # Post to every configured Discord channel
        embeds = format_report_embeds(report)
        channels_sent = 0
        for cid in settings.discord_channel_ids:
            try:
                channel = bot.get_channel(cid)
                if channel is None:
                    log.warning(f"Bridge: channel {cid} not found")
                    continue
                ok = await send_embeds(channel, embeds)
                if ok:
                    channels_sent += 1
                    log.info(f"Bridge: posted {name} to channel {cid} ({channel.name})")
            except Exception as e:
                log.error(f"Bridge: failed to post {name} to channel {cid}: {e}", exc_info=True)

        if channels_sent > 0:
            db.mark_report_sent(report_id)

        # Archive the pending file regardless of channel success — the pulse is
        # in daily_reports table; we don't want to repost it on next poll.
        await asyncio.to_thread(
            gh.put_file,
            archive_path,
            markdown,
            f"bridge: archive posted pulse {name} ({channels_sent} ch)",
        )
        await asyncio.to_thread(
            gh.delete_file,
            pending_path,
            f"bridge: remove pending {name} after posting",
        )
        log.info(f"Bridge: archived {name} (posted to {channels_sent} channels)")

    except Exception as e:
        log.error(f"Bridge: error processing pending pulse {name}: {e}", exc_info=True)
