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
import re
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
# Headless-fragment web publishing directory. Host sites (GitHub Pages,
# Substack-via-API, etc.) fetch latest-fragment.html + latest.json from
# here and inject into their own layout. Updates on every new pulse archive.
WEB_DIR = "pulse-output/web"
WEB_FRAGMENTS_DIR = "pulse-output/web/fragments"
# How many past pulses to expose in the archive index and render fragments
# for. ~60 = roughly 12 weeks of weekdays; older pulses still readable as
# raw markdown via their archive_url but no rendered fragment.
WEB_ARCHIVE_LIMIT = 60
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
# and moving the pulse to delivery-failed/. 90 min covers ~90 poll
# cycles (1/min) — enough to ride out a sustained Discord 5xx episode
# or a routine multi-minute global rate-limit, while still surfacing
# truly stuck pulses for human attention within a reasonable window.
# (Raised from 15min on 2026-06-01 after a real partial-outage incident.)
MAX_DELIVERY_RETRY_MINUTES = 90


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

        # Format-overhaul Phase 1: persist a compact state snapshot
        # (top themes + high-conviction calls) for the WHAT CHANGED
        # diff. The bridge stamps the consumed candidate at daily-pulse
        # post time and diffs it against the previous pulse's stamp.
        try:
            from report.pulse_sections import extract_state_from_ctx
            db.save_pulse_state_candidate(
                json.dumps(extract_state_from_ctx(ctx)),
                ctx["dumped_at_utc"],
            )
        except Exception as e:
            log.warning(f"Bridge: pulse_state snapshot failed (non-fatal): {e}")

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
            # Adjudication 0-themes hard-fail. If the validator rejected
            # every candidate theme (adj_themes==0 AND adj_discarded>0),
            # the pulse markdown that follows is by construction empty of
            # real content — section headers may exist but no theme bodies
            # passed. Shipping it would publish a hollow pulse. Move to
            # delivery-failed/ for human inspection instead.
            if adj_themes == 0 and adj_discarded > 0:
                log.error(
                    f"Bridge: {name} adjudication validated 0/{adj_discarded} themes "
                    f"(everything rejected) → moving to delivery-failed/"
                )
                await _commit_delivery_failed_marker(
                    name, raw_markdown, target_filter="",
                    target_count=0,
                    configured_count=len(settings.discord_channel_ids),
                    per_channel_errors=[
                        f"(adjudication validated 0 of {adj_discarded} candidate themes — "
                        f"pulse has no surviving content; refusing to publish)"
                    ],
                )
                await asyncio.to_thread(
                    gh.put_file, delivery_failed_path, raw_markdown,
                    f"bridge: move to delivery-failed (adj 0 themes) {name}",
                )
                await asyncio.to_thread(
                    gh.delete_file, pending_path,
                    f"bridge: remove pending {name} (adj 0 themes)",
                )
                return

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

        # === Format-overhaul Phase 1: inject WHAT CHANGED + TRADE BOARD ===
        # Both sections are assembled deterministically here (the LLM never
        # touches them) and injected into the markdown BEFORE embeds are
        # formatted and BEFORE the archive write, so Discord, the archive,
        # and the web fragment all carry identical content. Test fires
        # (target_channels set) skip both injection and state mutation so
        # test output stays bit-identical to the routine's.
        _is_test_fire = bool((meta.get("target_channels") or "").strip())
        if not _is_test_fire:
            try:
                from report.pulse_sections import (
                    compute_what_changed, render_what_changed,
                    extract_leans_from_markdown, render_trade_board,
                    render_desk_signal_board,
                    inject_sections, split_main_event_briefs,
                    replace_body_after_frontmatter,
                )
                # TRADE BOARD — extract today's leans, merge into the
                # tracked set (which records same-instrument direction
                # flips), render NEW / FLIP / LIVE. Done BEFORE WHAT
                # CHANGED so the flips feed the diff.
                leans = extract_leans_from_markdown(markdown)
                flips = db.upsert_pulse_leans(today, leans)
                flip_instruments = {
                    (f.get("instrument") or "").upper() for f in flips
                }
                board_rows = db.get_board_leans(today)
                board_md = render_trade_board(board_rows, today, flip_instruments)

                # WHAT CHANGED — diff today's consumed state vs the
                # previous daily pulse's state, plus the lean flips and
                # only HC calls whose ticker is in the body.
                today_stamp = db.stamp_pulse_state_for_date(today)
                prev_stamp = db.get_prev_stamped_pulse_state(today)
                body_tickers = set(re.findall(r"\$([A-Za-z]{1,5})\b", markdown))
                body_tickers = {t.upper() for t in body_tickers}
                wc_md = ""
                if today_stamp and prev_stamp:
                    bullets = compute_what_changed(
                        json.loads(prev_stamp["state_json"]),
                        json.loads(today_stamp["state_json"]),
                        lean_flips=flips,
                        body_tickers=body_tickers,
                    )
                    wc_md = render_what_changed(bullets)
                elif today_stamp:
                    log.info(
                        "Bridge: WHAT CHANGED skipped — baseline day "
                        "(no prior stamped pulse_state)"
                    )

                # DESK SIGNAL BOARD — today's HC calls + consensus ledger,
                # rendered deterministically from the same stamped state.
                desk_md = ""
                if today_stamp:
                    desk_md = render_desk_signal_board(
                        json.loads(today_stamp["state_json"])
                    )

                injected = inject_sections(markdown, wc_md, board_md, desk_md)
                # Phase 3: split INSIGHTS into THE MAIN EVENT + BRIEFS as
                # the LAST transform, so inject anchors (which key on the
                # INSIGHTS / WHAT TO WATCH headers) all resolve first and
                # the final order reads RECAP → WHAT CHANGED → DESK
                # SIGNAL → MAIN EVENT → BRIEFS → TRADE BOARD → WHAT TO
                # WATCH.
                injected = split_main_event_briefs(injected)
                if injected != markdown:
                    markdown = injected
                    raw_markdown = replace_body_after_frontmatter(
                        raw_markdown, markdown
                    )
                    log.info(
                        f"Bridge: injected sections — what_changed="
                        f"{bool(wc_md)}, desk_signal={bool(desk_md)}, "
                        f"board_rows={len(board_rows)}, "
                        f"leans_today={len(leans)}, main_event_split=yes"
                    )
            except Exception as e:
                # Injection is enhancement, not gating — a failure ships
                # the un-injected pulse rather than blocking delivery.
                log.error(
                    f"Bridge: section injection failed (shipping pulse "
                    f"without new sections): {e}", exc_info=True,
                )

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

        # Defensive check: if format_report_embeds produced only the gold
        # header + gray footer (no RECAP/INSIGHTS/WHAT-TO-WATCH sections),
        # the markdown was structurally empty. Don't ship a hollow pulse
        # — move to delivery-failed/ for inspection.
        section_embeds = [
            e for e in embeds
            if getattr(e, "title", None)
            and isinstance(e.title, str)
            and e.title.strip()
        ]
        if len(section_embeds) == 0:
            log.error(
                f"Bridge: {name} formatted to 0 section embeds "
                f"(only header+footer) — markdown appears empty. "
                f"→ moving to delivery-failed/"
            )
            await _commit_delivery_failed_marker(
                name, raw_markdown, target_filter,
                target_count=len(target_ids),
                configured_count=len(configured_ids),
                per_channel_errors=[
                    "(pulse markdown parsed to 0 section embeds — RECAP/"
                    "INSIGHTS/WHAT-TO-WATCH headers missing or empty)"
                ],
            )
            await asyncio.to_thread(
                gh.put_file, delivery_failed_path, raw_markdown,
                f"bridge: move to delivery-failed (empty embeds) {name}",
            )
            await asyncio.to_thread(
                gh.delete_file, pending_path,
                f"bridge: remove pending {name} (delivery-failed)",
            )
            return

        # Use send_embeds_detailed so partial-delivery (some embeds shipped
        # but not all) is captured and treated as a real failure, not silently
        # archived as "channel succeeded".
        from discord_bot.sender import send_embeds_detailed

        channels_sent = 0
        channels_partial = 0
        per_channel_errors: list[str] = []
        for cid in target_ids:
            try:
                channel = bot.get_channel(cid)
                if channel is None:
                    msg = f"channel {cid}: not found in bot cache"
                    log.warning(f"Bridge: {msg}")
                    per_channel_errors.append(msg)
                    continue
                result = await send_embeds_detailed(
                    channel, embeds, leading_content=leading_content,
                )
                if result["success"]:
                    channels_sent += 1
                    log.info(
                        f"Bridge: posted {name} to channel {cid} ({channel.name}) "
                        f"({result['embeds_sent']}/{result['embeds_total']} embeds)"
                    )
                elif result["embeds_sent"] > 0:
                    # Partial delivery — some embeds shipped, then a 429/5xx
                    # broke mid-stream. Count as a failed channel for the
                    # retry-window decision (we want the next bridge poll to
                    # retry the full pulse) but log the partial state so a
                    # human can see Discord may have a half-broken render.
                    channels_partial += 1
                    msg = (
                        f"channel {cid} ({channel.name}): PARTIAL — "
                        f"{result['embeds_sent']}/{result['embeds_total']} embeds shipped "
                        f"before failure ({result['last_error']})"
                    )
                    log.warning(f"Bridge: {msg}")
                    per_channel_errors.append(msg)
                else:
                    msg = (
                        f"channel {cid} ({channel.name}): 0/{result['embeds_total']} "
                        f"embeds shipped ({result['last_error']})"
                    )
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
        if channels_sent < len(target_ids):
            failed_count = len(target_ids) - channels_sent
            partial = (
                f" (PARTIAL: {failed_count} channel(s) failed"
                + (
                    f", {channels_partial} of those mid-stream after some embeds shipped"
                    if channels_partial > 0 else ""
                )
                + ")"
            )
        else:
            partial = ""
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
# JOB 3 — publish headless HTML fragment + JSON for host-site embeds
# -----------------------------------------------------------------------------
#
# After each pulse archives, regenerate three files in pulse-output/web/:
#   latest-fragment.html   — <article class="pulse">...</article>, no inline
#                            styles. Host sites embed this and style via
#                            their own CSS.
#   latest.json            — metadata (title, date, pdf_count, theme list,
#                            URLs). Host sites can render their own byline /
#                            masthead from this instead of using the
#                            fragment's built-in one.
#   archive.json           — index of the last N pulses (ts + filename + raw
#                            URL). Lets the host site render an archive list.
#
# Idempotent: latest.json carries the source pulse ts; if it already matches
# the most recent archive, the job is a no-op.


def publish_web_fragment_job() -> None:
    """Publish per-pulse HTML fragments + enriched archive index for the web.

    Architecture:
      - For each archived pulse (capped at WEB_ARCHIVE_LIMIT most recent),
        render a headless HTML fragment and commit to web/fragments/<ts>.html
        if it doesn't already exist there.
      - Maintain web/archive.json as the index: every entry carries ts +
        title + date_utc + pdf_count + fragment_url + archive_url. Host
        sites use this to filter by week, render a list, deep-link, etc.
      - Keep web/latest-fragment.html + web/latest.json as pointers to the
        most recent pulse for backward-compat with simple embeds that just
        want "today's pulse" without paginating.

    Idempotency:
      - Per-pulse fragment files are written once and never rewritten.
      - archive.json reuses cached title/date_utc for already-known pulses,
        so only NEW pulses pay the cost of fetching the full markdown to
        extract metadata.
      - latest-* pointers are rewritten only when a new pulse appears
        (detected via a delta in the archive listing).
    """
    if not bridge_enabled():
        return
    try:
        items = gh.list_dir(ARCHIVE_DIR)
    except Exception as e:
        log.warning(f"Bridge: failed to list {ARCHIVE_DIR} for web publish: {e}")
        return

    md_files = [
        it for it in items
        if it.get("type") == "file" and it.get("name", "").endswith(".md")
    ]
    if not md_files:
        return

    # Newest first.
    md_files.sort(key=lambda it: it.get("name", ""), reverse=True)
    candidates = md_files[:WEB_ARCHIVE_LIMIT]

    # Lazy imports — keep heavy markdown/HTML rendering off the bridge
    # module's top-level so the dev environment that only runs the discord
    # bot doesn't have to install `markdown`.
    from scripts.pulse_dashboard import (
        render_pulse_fragment,
        extract_pulse_metadata,
    )

    repo = settings.github_repo.strip().strip("/")
    branch = settings.github_bridge_branch

    # Reuse cached entries from existing archive.json so we don't refetch
    # each pulse's markdown just to extract title/date that we already
    # extracted in a previous job run.
    cached_entries: dict[str, dict[str, Any]] = {}
    try:
        existing_archive = gh.get_file_text(f"{WEB_DIR}/archive.json")
        if existing_archive:
            data = json.loads(existing_archive)
            for entry in (data.get("pulses") or []):
                ts = entry.get("ts")
                if ts:
                    cached_entries[ts] = entry
    except (json.JSONDecodeError, Exception):
        pass

    # Per-pulse fragments already on disk — don't re-render these.
    try:
        existing_fragments_listing = gh.list_dir(WEB_FRAGMENTS_DIR)
    except Exception:
        existing_fragments_listing = []
    existing_fragments: set[str] = {
        it.get("name", "")
        for it in existing_fragments_listing
        if it.get("type") == "file"
    }

    # === TEST-PULSE FILTER ===
    # Pulses fired with a non-empty `target_channels` frontmatter field are
    # test fires (Discord delivery is filtered to a test channel). They must
    # NOT appear on the public web dashboard — only scheduled production
    # pulses do.
    #
    # Classification source: the `target_channels` field that
    # extract_pulse_metadata pulls from the pulse markdown's frontmatter.
    # When a cached archive.json entry already carries that field (entries
    # processed after this filter shipped), we reuse it. When the field is
    # absent (entries cached before the filter shipped), we fetch the
    # markdown once to classify and the result then gets cached on this
    # cycle's archive.json write. So this pre-pass is at worst a one-shot
    # backfill cost on the first run after deploy, then cheap forever.
    #
    # Test pulses dropped here: removed from archive.json (not visible to
    # the dashboard), skipped for fragment rendering, ignored when picking
    # the "newest" pointer for latest-fragment.html / latest.json. Their
    # existing fragment files (if any) stay on disk as harmless artifacts
    # — nothing references them once archive.json drops the entry.
    filtered_candidates: list[dict[str, Any]] = []
    prefetched_md: dict[str, str] = {}
    test_pulses_skipped: list[str] = []
    for item in candidates:
        name = item.get("name", "")
        ts = name[:-3]
        cached = cached_entries.get(ts)
        if cached and "target_channels" in cached:
            # Cached classification — use directly.
            if (cached.get("target_channels") or "").strip():
                test_pulses_skipped.append(ts)
                continue
            filtered_candidates.append(item)
        else:
            # Cached entry lacks the target_channels field (or this entry
            # isn't cached at all). Fetch the markdown once to classify.
            pulse_md = gh.get_file_text(f"{ARCHIVE_DIR}/{name}")
            if not pulse_md:
                log.debug(f"Bridge: web publish — {name} unreadable during classify, skipping")
                continue
            peek_meta = extract_pulse_metadata(
                pulse_md, ts=ts, source_filename=name, repo=repo, branch=branch,
            )
            if (peek_meta.get("target_channels") or "").strip():
                test_pulses_skipped.append(ts)
                continue
            filtered_candidates.append(item)
            # Cache the fetched markdown so the main loop doesn't refetch
            # it when this entry becomes the "newest" or needs metadata.
            prefetched_md[ts] = pulse_md

    if test_pulses_skipped:
        log.info(
            f"Bridge: web publish skipped {len(test_pulses_skipped)} test pulse(s) — "
            f"{test_pulses_skipped[:5]}{'...' if len(test_pulses_skipped) > 5 else ''}"
        )

    if not filtered_candidates:
        log.debug("Bridge: web publish — no production pulses to process")
        return

    # POLICY: only the SINGLE most recent PRODUCTION pulse gets full
    # processing (markdown fetch + metadata extract + fragment render).
    # Older production pulses get either a cached entry (if we extracted
    # them on a previous job run) or a MINIMAL stub (ts + filename +
    # archive_url only). This keeps the first-deploy cost bounded to one
    # fetch + one fragment commit instead of N=WEB_ARCHIVE_LIMIT fetches
    # + commits.
    #
    # Effect on host-site pages:
    #   - The weekly-view dashboard filters by date_utc and only renders
    #     entries that have a fragment_url. Older stubs are silently
    #     skipped — visible only as raw-markdown links via archive_url.
    #   - As new pulses fire each weekday, the worker fills in the
    #     fragment for the new latest. Past pulses stay as stubs unless
    #     a separate backfill is run manually.
    archive_entries: list[dict[str, Any]] = []
    new_fragments = 0
    latest_md: str | None = None
    latest_ts: str | None = None
    latest_filename: str | None = None

    for idx, item in enumerate(filtered_candidates):
        name = item["name"]
        ts = name[:-3]  # strip .md
        fragment_name = f"{ts}.html"
        is_newest = (idx == 0)

        cached = cached_entries.get(ts)
        fragment_present = fragment_name in existing_fragments

        if is_newest:
            # Full processing: fetch markdown (if needed), extract metadata,
            # render fragment (if missing), commit, build a full entry.
            if cached and fragment_present and "target_channels" in cached:
                # Already up-to-date AND already classified as production.
                # Refresh URLs in the entry but skip the markdown fetch.
                entry = dict(cached)
                entry["archive_url"] = (
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{ARCHIVE_DIR}/{name}"
                )
                entry["fragment_url"] = (
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{WEB_FRAGMENTS_DIR}/{fragment_name}"
                )
            else:
                # Use the markdown the classifier already fetched if we
                # have it; otherwise fetch fresh.
                pulse_md = prefetched_md.get(ts) or gh.get_file_text(f"{ARCHIVE_DIR}/{name}")
                if not pulse_md:
                    log.warning(
                        f"Bridge: web publish — newest pulse {name} markdown unreadable, "
                        f"falling back to stub entry"
                    )
                    entry = {
                        "ts": ts,
                        "filename": name,
                        "archive_url": (
                            f"https://raw.githubusercontent.com/{repo}/{branch}/{ARCHIVE_DIR}/{name}"
                        ),
                        "target_channels": "",
                    }
                else:
                    meta = extract_pulse_metadata(
                        pulse_md, ts=ts, source_filename=name,
                        repo=repo, branch=branch,
                    )
                    if not fragment_present:
                        fragment_html = render_pulse_fragment(pulse_md)
                        gh.put_file(
                            f"{WEB_FRAGMENTS_DIR}/{fragment_name}",
                            fragment_html,
                            f"bridge: web fragment for {ts}",
                        )
                        new_fragments += 1
                    entry = {
                        "ts": ts,
                        "filename": name,
                        "title": meta["title"],
                        "date_utc": meta["date_utc"],
                        "pdf_count": meta["pdf_count"],
                        "archive_url": meta["archive_url"],
                        "fragment_url": (
                            f"https://raw.githubusercontent.com/{repo}/{branch}/{WEB_FRAGMENTS_DIR}/{fragment_name}"
                        ),
                        "target_channels": meta.get("target_channels", ""),
                    }
                    latest_md = pulse_md
                    latest_ts = ts
                    latest_filename = name
        elif cached:
            # Older pulse we processed in a previous run — reuse the cached
            # entry (preserves any title/date_utc that was extracted then).
            entry = dict(cached)
            entry["archive_url"] = (
                f"https://raw.githubusercontent.com/{repo}/{branch}/{ARCHIVE_DIR}/{name}"
            )
            # Only expose fragment_url if the file actually exists on disk.
            if fragment_present:
                entry["fragment_url"] = (
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{WEB_FRAGMENTS_DIR}/{fragment_name}"
                )
            else:
                entry.pop("fragment_url", None)
            # Ensure target_channels is set so the next cycle's pre-pass
            # can use cached classification without refetching markdown.
            # All entries reaching this branch are production (the test-
            # pulse filter dropped them in the pre-pass), so empty string.
            entry.setdefault("target_channels", "")
        else:
            # Older pulse, no cached metadata, no fragment — minimal stub.
            # Host pages skip entries without a fragment_url; the raw
            # markdown is still reachable via archive_url for anyone who
            # wants to read it directly. Stamp target_channels="" so the
            # next cycle's pre-pass can skip the classification fetch.
            entry = {
                "ts": ts,
                "filename": name,
                "archive_url": (
                    f"https://raw.githubusercontent.com/{repo}/{branch}/{ARCHIVE_DIR}/{name}"
                ),
                "target_channels": "",
            }

        archive_entries.append(entry)

    # Update the latest-* pointers ONLY if a new fragment was rendered or
    # the latest entry differs from what's currently in latest.json. Avoids
    # spamming the branch with no-op commits on every poll cycle.
    should_refresh_pointers = new_fragments > 0
    if not should_refresh_pointers and latest_ts:
        try:
            existing_latest = gh.get_file_text(f"{WEB_DIR}/latest.json")
            if existing_latest:
                existing_data = json.loads(existing_latest)
                if existing_data.get("ts") != latest_ts:
                    should_refresh_pointers = True
            else:
                should_refresh_pointers = True
        except (json.JSONDecodeError, Exception):
            should_refresh_pointers = True

    if should_refresh_pointers and latest_md and latest_ts and latest_filename:
        latest_fragment = render_pulse_fragment(latest_md)
        latest_meta = extract_pulse_metadata(
            latest_md, ts=latest_ts, source_filename=latest_filename,
            repo=repo, branch=branch,
        )
        gh.put_file(
            f"{WEB_DIR}/latest-fragment.html",
            latest_fragment,
            f"bridge: latest fragment pointer -> {latest_ts}",
        )
        gh.put_file(
            f"{WEB_DIR}/latest.json",
            json.dumps(latest_meta, indent=2),
            f"bridge: latest metadata pointer -> {latest_ts}",
        )

    # Write archive.json if new fragments were rendered OR the structure
    # changed (new ts in the list).
    cached_ts_set = set(cached_entries.keys())
    current_ts_set = {e["ts"] for e in archive_entries}
    archive_changed = new_fragments > 0 or cached_ts_set != current_ts_set

    if archive_changed:
        archive_payload = {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "count": len(archive_entries),
            "pulses": archive_entries,
        }
        gh.put_file(
            f"{WEB_DIR}/archive.json",
            json.dumps(archive_payload, indent=2),
            f"bridge: archive index ({len(archive_entries)} pulses, {new_fragments} new)",
        )
        log.info(
            f"Bridge: web publish — {new_fragments} new fragments, "
            f"{len(archive_entries)} archive entries"
        )
