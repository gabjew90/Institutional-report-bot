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
from report.formatter import format_report_embeds, format_report_header_message
from report.models import DailyReport
from discord_bot.sender import send_embeds
import db

log = logging.getLogger(__name__)

CONTEXT_PATH = "pulse-context/latest.json"
PENDING_DIR = "pulse-output/pending"
ARCHIVE_DIR = "pulse-output/archive"
# Hidden HTML comment we write into a QC review after appending its
# dashboard link section. Idempotency marker: presence => already
# published, skip on subsequent polls.
QC_DASHBOARD_PUBLISHED_MARKER = "<!-- dashboard-published -->"
# Retention for litterbox uploads. 24h is enough for "read it this
# morning, or read it later today before close" without keeping every
# pulse's HTML hosted on a third-party service forever. The pulse
# markdown stays archived on GitHub regardless.
QC_DASHBOARD_RETENTION = "24h"
PENDING_ADJUDICATIONS_DIR = "pulse-output/pending-adjudications"
ARCHIVE_ADJUDICATIONS_DIR = "pulse-output/archive-adjudications"
# Operational location for pulse markdown that exhausted delivery retries.
# A human can manually move the file back to PENDING_DIR/ to retry once
# the underlying issue (e.g., Discord outage) resolves. The HUMAN-READABLE
# explanation lives in pulse-output/qc-reviews/<ts>.delivery.md, NOT here.
DELIVERY_FAILED_DIR = "pulse-output/delivery-failed"
# Unified quality artifact location. Both the routine's QC review (success
# path) and any failure markers (routine failures from STEP 2/6/7, plus
# bridge delivery failures as <ts>.delivery.md sidecars) land here so a
# single directory poll catches every quality/error event for any pulse.
QC_REVIEWS_DIR = "pulse-output/qc-reviews"
# How long the bridge keeps retrying a failed delivery before giving up
# and moving the pulse to delivery-failed/. 15 min covers ~15 poll
# cycles (1/min) — enough to ride out a typical Discord outage but
# not so long that a stuck pulse blocks attention.
MAX_DELIVERY_RETRY_MINUTES = 15


def bridge_enabled() -> bool:
    return bool(
        settings.github_token
        and settings.github_repo
        and settings.github_bridge_branch
    )


# -----------------------------------------------------------------------------
# JOB 1 — dump synthesis context to GitHub
# -----------------------------------------------------------------------------


def _compute_window_cutoff() -> tuple[str, str]:
    """Return (cutoff_iso, window_label) used for both dump + stats.

    Rule: take the WIDER of `since-last-daily-pulse` or `last 24h`, clipped
    to a 96h ceiling. Wider = earlier cutoff = lexicographically smaller ISO.

    Behavior:
    - Tuesday 9 AM ET pulse: last daily was Monday 9 AM (24h). min(24h, 24h) = 24h ✓
    - Monday 9 AM ET pulse: last daily was Friday 9 AM (72h). min(72h, 24h) = 72h ✓
      (Friday→Monday spans the weekend correctly.)
    - Mid-day test re-run, 3 hours after the morning pulse: min(3h, 24h) = 24h ✓
      (Test pulse gets a full 24h of context, not just 3h.)
    """
    from datetime import timedelta
    twenty_four_h = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    ninety_six_h = (datetime.utcnow() - timedelta(hours=96)).isoformat()
    last_daily = db.get_last_report_time()
    if last_daily:
        # min() on ISO strings = lexicographically smaller = earlier = wider window
        cutoff = min(last_daily, twenty_four_h)
        if cutoff < ninety_six_h:
            cutoff = ninety_six_h
            label = "96h ceiling"
        elif cutoff == twenty_four_h:
            label = "last-24h"
        else:
            label = f"since-last-daily ({cutoff[:16]})"
    else:
        cutoff = twenty_four_h
        label = "first-run-24h"
    return cutoff, label


def dump_context_job() -> None:
    """Build the pulse context and commit JSON to the bridge branch.

    Cutoff logic via _compute_window_cutoff(): wider of since-last-daily
    or last-24h, with a 96h ceiling. Ensures Monday spans the weekend
    AND mid-day test runs still get 24h of context.

    Runs in the APScheduler thread. Synchronous (no async tools used).
    """
    if not bridge_enabled():
        return
    try:
        gh.ensure_branch_exists()

        cutoff, window_label = _compute_window_cutoff()

        rows = db.get_analyses_since(cutoff)
        if not rows:
            log.info(f"Bridge: no analyses since {cutoff[:16]} — skipping context dump")
            return

        # Filter LOW priority out of synthesis input. LOW is admin/wrappers/
        # regional single-stocks per triage rules; including them inflates
        # analyses_json by ~30-40% and dilutes signal in INSIGHTS selection.
        total_rows = len(rows)
        rows = [r for r in rows if (r.get("priority") or "").lower() != "low"]
        dropped = total_rows - len(rows)
        if dropped:
            log.info(f"Bridge: dropped {dropped} LOW-priority analyses from synthesis input")

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


def _compute_footer_stats() -> dict:
    """Recompute the rich footer stats over the SAME window the dump used.

    Uses _compute_window_cutoff() so the headline pdf_count and the footer
    breakdown (top sources, priority mix, date range) always reflect the
    same set of analyses. Prevents the bug where headline showed 3 PDFs
    (since-last-daily) but breakdown showed 178 (24h) — different windows.

    Returns {pdf_count, top_sources, priority_mix, earliest_upload,
    latest_upload}. Empty dict on failure.
    """
    try:
        cutoff, _ = _compute_window_cutoff()
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


def _fetch_matching_adjudication(pulse_md_name: str) -> tuple[dict | None, str | None]:
    """For pulse markdown filename '<base>.md', look for the matching adjudication
    JSON at PENDING_ADJUDICATIONS_DIR/<base>.json.

    Returns (parsed_dict, raw_text):
      - (None, None)         file is absent (404 from the bridge)
      - (dict, raw_text)     file present and parses as JSON
      - (None, raw_text)     file present but JSON malformed — caller can still
                             archive the raw form for inspection; pulse posting
                             continues without an adjudication payload.

    Never raises. The bridge worker must not lose a pulse over an adjudication
    fetch issue.
    """
    if not pulse_md_name.endswith(".md"):
        return None, None
    base = pulse_md_name[:-3]
    adj_path = f"{PENDING_ADJUDICATIONS_DIR}/{base}.json"
    try:
        raw = gh.get_file_text(adj_path)
    except Exception as e:
        log.warning(f"Bridge: error fetching adjudication {adj_path}: {e}")
        return None, None
    if not raw:
        return None, None
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        log.warning(f"Bridge: adjudication file {adj_path} present but malformed JSON: {e}")
        return None, raw


def _pulse_age_minutes(name: str) -> float | None:
    """Parse the timestamp out of a pulse filename like '2026-05-08T19-53-08Z.md'
    and return age in minutes vs now (UTC). Returns None if filename can't
    be parsed — caller should treat that as "give up retry, archive normally"
    rather than block forever on a malformed name.
    """
    try:
        base = name[:-3] if name.endswith(".md") else name  # strip .md
        # '2026-05-08T19-53-08Z' → '2026-05-08T19:53:08Z'
        if len(base) >= 20 and base.endswith("Z"):
            iso = base[:10] + "T" + base[11:13] + ":" + base[14:16] + ":" + base[17:19]
            ts = datetime.fromisoformat(iso)
            return (datetime.utcnow() - ts).total_seconds() / 60.0
    except Exception:
        pass
    return None


async def _commit_delivery_failed_marker(
    name: str,
    raw_markdown: str,
    target_filter: str,
    target_count: int,
    configured_count: int,
    per_channel_errors: list[str],
) -> None:
    """Write a structured marker under pulse-output/qc-reviews/<ts>.delivery.md
    so a watcher (or human) sees exactly why delivery failed without
    spelunking Railway logs. Sidecar to the routine's QC review for the
    same <ts> — together they form the unified quality record:
      - <ts>.md         → routine review (or routine-failure marker)
      - <ts>.delivery.md → bridge delivery outcome (this file)

    Includes which target_channels filter ran, how many channels matched
    the filter, and per-channel error detail (Discord 503s, channel-not-
    found, etc.).

    Best-effort: a failed marker commit must NOT cascade — the pulse is
    already preserved in delivery-failed/ alongside this marker.
    """
    base = name[:-3] if name.endswith(".md") else name
    marker_path = f"{QC_REVIEWS_DIR}/{base}.delivery.md"
    body_lines = [
        f"# QC Review — {base}",
        "",
        "## Status: DELIVERY FAILED (bridge)",
        "",
        f"- **Time (UTC):** {datetime.utcnow().isoformat()}",
        f"- **Pulse file:** {DELIVERY_FAILED_DIR}/{name} (preserved for manual retry)",
        f"- **target_channels filter:** {target_filter or '(none — all configured)'}",
        f"- **Channels matched filter:** {target_count} of {configured_count}",
        f"- **Channels delivered:** 0",
        "",
        "This sidecar accompanies the routine's main QC review at "
        f"`pulse-output/qc-reviews/{base}.md` (or absent if the routine never reached "
        "STEP 7). Bridge delivery happens AFTER the routine completes, so this marker "
        "is appended post-hoc when delivery fails.",
        "",
        "## Per-channel errors",
        "",
        "```",
    ]
    if per_channel_errors:
        body_lines.extend(per_channel_errors)
    else:
        body_lines.append("(no per-channel errors logged — channels matched but send loop produced no signal)")
    body_lines.append("```")
    body_lines.append("")
    body_lines.append("## Recovery")
    body_lines.append("")
    body_lines.append(
        "Pulse markdown is preserved at the path above. To retry delivery, "
        "move the file from `pulse-output/delivery-failed/` back to `pulse-output/pending/` — "
        "the bridge will re-attempt on the next 60s poll."
    )
    body = "\n".join(body_lines)
    try:
        await asyncio.to_thread(
            gh.put_file, marker_path, body,
            f"bridge: QC delivery-failed marker for {name}",
        )
        log.info(f"Bridge: wrote QC delivery marker {marker_path}")
    except Exception as e:
        log.warning(f"Bridge: could not commit QC delivery marker: {e}")


async def _process_one_pulse(bot, item: dict[str, Any]) -> None:
    name = item.get("name", "")
    pending_path = f"{PENDING_DIR}/{name}"
    archive_path = f"{ARCHIVE_DIR}/{name}"
    delivery_failed_path = f"{DELIVERY_FAILED_DIR}/{name}"

    try:
        raw_markdown = gh.get_file_text(pending_path)
        if not raw_markdown:
            log.warning(f"Bridge: empty pending file {pending_path} — skipping")
            return

        # Parse optional frontmatter for accurate pdf_count + token usage
        meta, markdown = _parse_frontmatter(raw_markdown)

        # Fetch the matching adjudication JSON if the routine produced one.
        # Returns (None, None) cleanly when absent, so this is a no-op for
        # pulses produced before the adjudication step was wired into the
        # routine prompt. Captured here so we can both embed it into the
        # DailyReport and archive it alongside the pulse markdown later.
        parsed_adj, raw_adj = _fetch_matching_adjudication(name)
        if parsed_adj is not None:
            adj_themes = len(parsed_adj.get("themes", []) or [])
            adj_discarded = len(parsed_adj.get("discarded_themes", []) or [])
            log.info(
                f"Bridge: matched adjudication for {name} — "
                f"{adj_themes} themes, {adj_discarded} discarded"
            )

        today = date.today().isoformat()
        pdf_count = int(meta.get("pdf_count", 0))
        input_tokens = int(meta.get("input_tokens", 0))
        output_tokens = int(meta.get("output_tokens", 0))

        # Recompute the rich footer stats (top sources, priority mix, date
        # range) from the same 24h window the routine just synthesized over,
        # so the Discord embed footer matches the Gemini-pulse format.
        stats = _compute_footer_stats()
        # If the routine reported a pdf_count, prefer it (the routine saw the
        # exact context); otherwise fall back to whatever this 24h window has.
        if pdf_count > 0:
            stats["pdf_count"] = pdf_count
        else:
            pdf_count = stats.get("pdf_count", 0)
        raw_json_payload: dict[str, Any] = {
            "source": "github_bridge",
            "pending_file": name,
            **meta,
        }
        if parsed_adj is not None:
            raw_json_payload["adjudication"] = parsed_adj

        report = DailyReport(
            report_date=today,
            report_type="daily",  # routine pulses replace the scheduled Gemini one
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json=raw_json_payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stats=stats,
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

        # No matched channels at all — this is a config error, not a
        # transient delivery problem. Move directly to delivery-failed/
        # rather than retry forever.
        if not target_ids:
            log.error(
                f"Bridge: no Discord channels matched for {name} "
                f"(filter='{target_filter}', configured={len(configured_ids)}) "
                f"→ moving to delivery-failed/"
            )
            await _commit_delivery_failed_marker(
                name, raw_markdown, target_filter,
                target_count=0, configured_count=len(configured_ids),
                per_channel_errors=[
                    "(no channels matched the target_channels filter — check filter "
                    "string against actual Discord channel names/IDs)"
                ],
            )
            await asyncio.to_thread(
                gh.put_file, delivery_failed_path, raw_markdown,
                f"bridge: move to delivery-failed (no channels matched) {name}",
            )
            await asyncio.to_thread(
                gh.delete_file, pending_path,
                f"bridge: remove pending {name} (delivery-failed)",
            )
            return

        embeds = format_report_embeds(report)
        leading_content = format_report_header_message(report)
        channels_sent = 0
        per_channel_errors: list[str] = []
        for cid in target_ids:
            try:
                channel = bot.get_channel(cid)
                if channel is None:
                    msg = f"channel {cid}: not found in bot cache"
                    log.warning(f"Bridge: {msg}")
                    per_channel_errors.append(msg)
                    continue
                ok = await send_embeds(channel, embeds, leading_content=leading_content)
                if ok:
                    channels_sent += 1
                    log.info(f"Bridge: posted {name} to channel {cid} ({channel.name})")
                else:
                    msg = f"channel {cid} ({channel.name}): send_embeds returned False (Discord API error — see Railway logs around this timestamp)"
                    log.warning(f"Bridge: {msg}")
                    per_channel_errors.append(msg)
            except Exception as e:
                msg = f"channel {cid}: {type(e).__name__}: {e}"
                log.error(f"Bridge: failed to post {name} to {msg}", exc_info=True)
                per_channel_errors.append(msg)

        # === DELIVERY OUTCOME ===
        if channels_sent == 0:
            # Zero successful posts. Decide between retry and give-up based
            # on pulse age. Transient outages (Discord 503, brief network
            # blip) get auto-recovered by subsequent bridge polls. Persistent
            # failures eventually move to delivery-failed/ for human attention.
            age_min = _pulse_age_minutes(name)
            if age_min is not None and age_min <= MAX_DELIVERY_RETRY_MINUTES:
                log.warning(
                    f"Bridge: {name} delivery failed (0 of {len(target_ids)} channels succeeded), "
                    f"age={age_min:.1f}m, will retry on next poll"
                )
                # Leave files in pending/ + pending-adjudications/. Skip db
                # insert — we'll insert when delivery actually succeeds.
                return
            # Retry window exhausted (or filename unparseable) → give up.
            log.error(
                f"Bridge: {name} delivery exhausted retries "
                f"(age={age_min}m, max={MAX_DELIVERY_RETRY_MINUTES}m) → moving to delivery-failed/"
            )
            await _commit_delivery_failed_marker(
                name, raw_markdown, target_filter,
                target_count=len(target_ids),
                configured_count=len(configured_ids),
                per_channel_errors=per_channel_errors,
            )
            await asyncio.to_thread(
                gh.put_file, delivery_failed_path, raw_markdown,
                f"bridge: move to delivery-failed (retries exhausted) {name}",
            )
            await asyncio.to_thread(
                gh.delete_file, pending_path,
                f"bridge: remove pending {name} (delivery-failed)",
            )
            # Adjudication stays in pending-adjudications/ unless a future
            # pulse with the same base name lands — orphan cleanup is a
            # separate concern.
            return

        # === SUCCESS PATH ===
        # At least one channel got the post. Persist to DB now (delayed
        # from before-post so we don't accumulate stale rows on retries).
        report_id = db.insert_daily_report(
            report_date=today,
            report_type="daily",
            report_json=json.dumps(report.raw_json),
            report_markdown=markdown,
            pdf_count=pdf_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        db.mark_report_sent(report_id)

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
        partial = (
            f" (PARTIAL: {len(target_ids) - channels_sent} channel(s) failed)"
            if channels_sent < len(target_ids) else ""
        )
        log.info(f"Bridge: archived {name} (posted to {channels_sent}/{len(target_ids)} channels){partial}")

        # Archive the matching adjudication file if one was retrieved. Errors
        # here must NOT cascade — the pulse is already posted and persisted.
        # Worst case: the adjudication file stays in pending-adjudications/
        # and gets cleaned up next cycle (the worker only matches by pulse
        # markdown, so an orphaned adjudication does no harm).
        if raw_adj is not None and name.endswith(".md"):
            base = name[:-3]
            adj_pending = f"{PENDING_ADJUDICATIONS_DIR}/{base}.json"
            adj_archive = f"{ARCHIVE_ADJUDICATIONS_DIR}/{base}.json"
            try:
                await asyncio.to_thread(
                    gh.put_file,
                    adj_archive,
                    raw_adj,
                    f"bridge: archive adjudication for {name}",
                )
                await asyncio.to_thread(
                    gh.delete_file,
                    adj_pending,
                    f"bridge: remove pending adjudication for {name}",
                )
                log.info(f"Bridge: archived adjudication {base}.json")
            except Exception as e:
                log.warning(
                    f"Bridge: failed to archive adjudication for {name}: {e} "
                    f"(pulse already posted; adjudication will retry next cycle)"
                )

    except Exception as e:
        log.error(f"Bridge: error processing pending pulse {name}: {e}", exc_info=True)


# -----------------------------------------------------------------------------
# JOB 3 — publish a phone-accessible dashboard link into each QC review
# -----------------------------------------------------------------------------
#
# After each pulse is delivered + QC reviewed, the routine commits the QC
# markdown to pulse-output/qc-reviews/<ts>.md. This job watches that
# directory, renders the matching archived pulse as styled HTML, uploads
# the HTML to litterbox.catbox.moe (24h retention), and rewrites the QC
# review file to append a "Phone-accessible dashboard" section with the
# URL. Marker comment makes the rewrite idempotent.
#
# Skipped cases (no pulse to render):
#   - Routine failure markers ("Status: FAILED" anywhere in the file)
#   - Bridge delivery sidecars (*.delivery.md filename suffix)


def publish_qc_dashboards_job() -> None:
    """Poll QC reviews; append a litterbox dashboard URL into each new one.

    Runs in the APScheduler thread. Synchronous — litterbox upload is HTTP
    via the `requests` library, and GitHub API I/O is via the bridge client.
    Each iteration handles one full QC review end-to-end; failures on one
    review don't block others.
    """
    if not bridge_enabled():
        return
    try:
        items = gh.list_dir(QC_REVIEWS_DIR)
    except Exception as e:
        log.warning(f"Bridge: failed to list {QC_REVIEWS_DIR} for dashboard publish: {e}")
        return

    candidates = [
        it for it in items
        if it.get("type") == "file"
        and it.get("name", "").endswith(".md")
        and not it.get("name", "").endswith(".delivery.md")
    ]
    if not candidates:
        return

    candidates.sort(key=lambda it: it.get("name", ""))
    for item in candidates:
        try:
            _publish_one_qc_dashboard(item)
        except Exception as e:
            log.error(
                f"Bridge: dashboard publish failed for {item.get('name','?')}: {e}",
                exc_info=True,
            )


def _publish_one_qc_dashboard(item: dict[str, Any]) -> None:
    name = item.get("name", "")
    qc_path = f"{QC_REVIEWS_DIR}/{name}"
    qc_content = gh.get_file_text(qc_path)
    if not qc_content:
        return
    if QC_DASHBOARD_PUBLISHED_MARKER in qc_content:
        return  # already published
    if "Status: FAILED" in qc_content:
        return  # routine-failure marker — no pulse to render

    pulse_path = f"{ARCHIVE_DIR}/{name}"
    pulse_md = gh.get_file_text(pulse_path)
    if not pulse_md:
        # Pulse might still be in pending/ (delivery hasn't completed yet, or
        # delivery-failed). Either way we can't render a dashboard from a
        # missing pulse; try again on the next poll once the pulse archives.
        log.debug(f"Bridge: QC {name} has no archived pulse yet — deferring dashboard")
        return

    # Lazy imports — keep these out of the module top-level so the bridge
    # module doesn't fail to import when `requests` or `markdown` aren't
    # installed on a dev environment that only runs the discord bot.
    import tempfile
    from pathlib import Path as _Path
    from scripts.pulse_dashboard import render_dashboard_html
    from scripts.litterbox_upload import upload_to_litterbox

    html = render_dashboard_html(pulse_md, source_filename=name)
    # litterbox_upload takes a file path; write the HTML to a tempfile.
    with tempfile.NamedTemporaryFile(
        suffix=".html", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        tmp.write(html)
        tmp_path = _Path(tmp.name)
    try:
        url = upload_to_litterbox(tmp_path, retention=QC_DASHBOARD_RETENTION)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass

    addendum = (
        "\n\n---\n\n"
        "## Phone-accessible dashboard\n\n"
        f"[Tap to view this pulse on phone (24h)]({url})\n\n"
        "_Rendered styled HTML hosted at litter.catbox.moe — expires "
        f"{QC_DASHBOARD_RETENTION} after upload. The pulse markdown itself "
        "stays archived on GitHub at "
        f"`{pulse_path}`._\n\n"
        f"{QC_DASHBOARD_PUBLISHED_MARKER}\n"
    )
    new_content = qc_content.rstrip("\n") + addendum
    gh.put_file(qc_path, new_content, f"bridge: append dashboard link to QC {name}")
    log.info(f"Bridge: appended dashboard link to QC {name}: {url}")
