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
    """Build the pulse context and commit JSON to the bridge branch.

    Window logic mirrors the original Gemini scheduled pulse:
      cutoff = max(last 'daily' report's created_at, now - 96h ceiling)
    so Monday's pulse correctly spans Friday's close through the weekend
    to Monday morning. Falls back to 24h on the very first run when no
    prior scheduled pulse exists.

    Runs in the APScheduler thread. Synchronous (no async tools used).
    """
    if not bridge_enabled():
        return
    try:
        gh.ensure_branch_exists()

        from datetime import timedelta
        last_report_time = db.get_last_report_time()
        max_lookback = (datetime.utcnow() - timedelta(hours=96)).isoformat()
        if last_report_time:
            cutoff = last_report_time if last_report_time > max_lookback else max_lookback
            window_label = f"since-last-daily ({cutoff[:16]})"
        else:
            # No prior daily pulse exists — fall back to a 24h window.
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            window_label = "first-run-24h"

        rows = db.get_analyses_since(cutoff)
        if not rows:
            log.info(f"Bridge: no analyses since {cutoff[:16]} — skipping context dump")
            return

        analyses = _load_analyses_from_db(rows)
        if not analyses:
            log.info("Bridge: no parseable analyses — skipping context dump")
            return

        ctx = build_pulse_context(analyses, use_prev_context=True)
        ctx["dumped_at_utc"] = datetime.utcnow().isoformat() + "Z"
        ctx["window_cutoff"] = cutoff
        ctx["window_label"] = window_label

        body = json.dumps(ctx, indent=1)
        msg = f"bridge: dump pulse-context ({ctx['pdf_count']} PDFs, {window_label})"
        result = gh.put_file(CONTEXT_PATH, body, msg)
        sha = (result.get("commit") or {}).get("sha", "")[:8]
        log.info(
            f"Bridge: dumped pulse-context — {ctx['pdf_count']} PDFs, {len(body) // 1024}KB, "
            f"window={window_label}, commit {sha}"
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


def _compute_footer_stats(window_hours: int = 24) -> dict:
    """Recompute the same rich footer stats the Gemini synthesizer uses.

    Loads analyses for the given window from the local DB and runs them
    through the existing _compute_stats helper in synthesizer.py. Returns
    {pdf_count, top_sources, priority_mix, earliest_upload, latest_upload}.
    Empty dict on any failure.
    """
    from datetime import timedelta
    try:
        cutoff = (datetime.utcnow() - timedelta(hours=window_hours)).isoformat()
        rows = db.get_analyses_since(cutoff)
        if not rows:
            return {}
        analyses = _load_analyses_from_db(rows)
        if not analyses:
            return {}
        from report.synthesizer import _compute_stats
        return _compute_stats(analyses)
    except Exception as e:
        log.warning(f"Bridge: failed to compute footer stats: {e}")
        return {}


def _parse_frontmatter(markdown: str) -> tuple[dict, str]:
    """Strip an optional YAML-style frontmatter block from the top of markdown.

    Routine writes:
        ---
        pdf_count: 220
        input_tokens: 12345
        output_tokens: 4567
        ---

        <actual pulse markdown>

    Returns (metadata_dict, markdown_without_frontmatter). If no frontmatter
    is present, returns ({}, markdown_unchanged).
    """
    if not markdown.startswith("---"):
        return {}, markdown
    end = markdown.find("\n---", 4)
    if end == -1:
        return {}, markdown
    block = markdown[3:end].strip()
    body = markdown[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            meta[k] = int(v)
        else:
            meta[k] = v
    return meta, body


async def _process_one_pulse(bot, item: dict[str, Any]) -> None:
    name = item.get("name", "")
    pending_path = f"{PENDING_DIR}/{name}"
    archive_path = f"{ARCHIVE_DIR}/{name}"

    try:
        raw_markdown = gh.get_file_text(pending_path)
        if not raw_markdown:
            log.warning(f"Bridge: empty pending file {pending_path} — skipping")
            return

        # Parse optional frontmatter for accurate pdf_count + token usage
        meta, markdown = _parse_frontmatter(raw_markdown)

        today = date.today().isoformat()
        pdf_count = int(meta.get("pdf_count", 0))
        input_tokens = int(meta.get("input_tokens", 0))
        output_tokens = int(meta.get("output_tokens", 0))

        # Recompute the rich footer stats (top sources, priority mix, date
        # range) from the same 24h window the routine just synthesized over,
        # so the Discord embed footer matches the Gemini-pulse format.
        stats = _compute_footer_stats(window_hours=24)
        # If the routine reported a pdf_count, prefer it (the routine saw the
        # exact context); otherwise fall back to whatever this 24h window has.
        if pdf_count > 0:
            stats["pdf_count"] = pdf_count
        else:
            pdf_count = stats.get("pdf_count", 0)
        report = DailyReport(
            report_date=today,
            report_type="daily",  # routine pulses replace the scheduled Gemini one
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json={"source": "github_bridge", "pending_file": name, **meta},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stats=stats,
        )

        # Persist before posting so we don't lose track if Discord errors
        report_id = db.insert_daily_report(
            report_date=today,
            report_type="daily",
            report_json=json.dumps(report.raw_json),
            report_markdown=markdown,
            pdf_count=pdf_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

        # Post to Discord channels. By default, all channels in
        # DISCORD_CHANNEL_ID. If the routine sets a `target_channels`
        # frontmatter value (comma-separated IDs or name substrings), filter
        # to those — used for test-runs that should only hit a test channel.
        target_filter = (meta.get("target_channels") or "").strip()
        configured_ids = settings.discord_channel_ids
        target_ids: list[int] = []
        if target_filter:
            tokens = [t.strip() for t in str(target_filter).split(",") if t.strip()]
            for cid in configured_ids:
                channel = bot.get_channel(cid)
                cname = channel.name if channel else ""
                for tok in tokens:
                    if tok.isdigit() and int(tok) == cid:
                        target_ids.append(cid); break
                    if tok and not tok.isdigit() and tok.lower() in cname.lower():
                        target_ids.append(cid); break
            log.info(f"Bridge: target_channels filter '{target_filter}' matched {len(target_ids)} of {len(configured_ids)} channels")
        else:
            target_ids = list(configured_ids)

        embeds = format_report_embeds(report)
        channels_sent = 0
        for cid in target_ids:
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
        # Archive the raw form (with frontmatter) so we keep the metadata.
        await asyncio.to_thread(
            gh.put_file,
            archive_path,
            raw_markdown,
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
