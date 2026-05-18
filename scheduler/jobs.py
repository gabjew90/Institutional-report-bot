"""APScheduler job definitions for automated pipeline execution."""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import settings

log = logging.getLogger(__name__)


def setup_scheduler(bot=None) -> AsyncIOScheduler:
    """Create and configure the scheduler with all pipeline jobs."""
    from pytz import timezone
    tz = timezone(settings.timezone)

    scheduler = AsyncIOScheduler(timezone=tz)

    # Job 1: Poll Dropbox for new PDFs (every 15 min)
    scheduler.add_job(
        _poll_dropbox_job,
        trigger=IntervalTrigger(minutes=settings.dropbox_poll_interval_minutes),
        id="poll_dropbox",
        name="Poll Dropbox for new PDFs",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Job 2: Process pending PDFs (every 5 min, offset from polling)
    scheduler.add_job(
        _process_queue_job,
        trigger=IntervalTrigger(minutes=settings.process_interval_minutes),
        id="process_queue",
        name="Process pending PDF queue",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Job 3: Daily Market Pulse (9am PST / 12pm ET)
    # NOTE: when GitHub-bridge is enabled, the Opus routine produces the
    # scheduled pulse instead. Skip registering this internal Gemini job to
    # avoid duplicate posts. Manual /pulse from Discord still works as before.
    from github_bridge.jobs import bridge_enabled
    bridge_active = bridge_enabled()
    if not bridge_active:
        scheduler.add_job(
            _daily_pulse_job,
            trigger=CronTrigger(
                hour=settings.daily_pulse_hour,
                minute=settings.daily_pulse_minute,
                timezone=tz,
            ),
            id="daily_pulse",
            name="Daily Market Pulse (Gemini)",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=600,
        )
    else:
        log.info("GitHub-bridge active — skipping internal Gemini scheduled pulse")

    # Bridge jobs (only register if GITHUB_TOKEN is set)
    if bridge_active:
        from github_bridge.jobs import dump_context_job
        scheduler.add_job(
            dump_context_job,
            trigger=IntervalTrigger(minutes=settings.bridge_dump_interval_minutes),
            id="bridge_dump_context",
            name="GitHub bridge: dump pulse context",
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            _bridge_post_pending_job,
            trigger=IntervalTrigger(seconds=settings.bridge_post_poll_interval_seconds),
            id="bridge_post_pending",
            name="GitHub bridge: post pending pulses",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=120,
        )

        # Web fragment publisher — regenerates pulse-output/web/{latest-
        # fragment.html, latest.json, archive.json} when a new pulse archives.
        # Host sites (GitHub Pages, embeds elsewhere) fetch these three files
        # to render the pulse into their own layout. Idempotent: skips when
        # latest.json's ts already matches the most recent archive.
        from github_bridge.jobs import publish_web_fragment_job
        scheduler.add_job(
            publish_web_fragment_job,
            trigger=IntervalTrigger(seconds=settings.bridge_post_poll_interval_seconds),
            id="bridge_publish_web_fragment",
            name="GitHub bridge: publish headless fragment + JSON for embeds",
            max_instances=1,
            misfire_grace_time=120,
        )

    # Reanalyze background processor — picks up persistent reanalyze jobs
    # and runs them to completion. Survives worker restarts: a job in
    # 'processing' state gets re-attached on next tick, resuming from the
    # processed/failed/bridge_queued lists already persisted to DB. Runs
    # every 60s so a freshly-queued job starts within a minute, but with
    # max_instances=1 a long-running job blocks subsequent ticks until done
    # (which is what we want — one reanalyze at a time).
    scheduler.add_job(
        _reanalyze_processor_job,
        trigger=IntervalTrigger(seconds=60),
        id="reanalyze_processor",
        name="Process pending reanalyze job",
        kwargs={"bot": bot},
        max_instances=1,
        misfire_grace_time=300,
    )

    # Real-time ingestion feed (only registers if DISCORD_INGEST_FEED_CHANNEL_ID set)
    from discord_bot.ingestion_feed import feed_enabled
    if feed_enabled():
        scheduler.add_job(
            _ingest_feed_tick_job,
            trigger=IntervalTrigger(seconds=settings.ingest_feed_interval_seconds),
            id="ingest_feed_tick",
            name="Ingestion feed: announce next pending",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=120,
        )

    # Opus-bridge HIGH ingestion (only registers when backend=opus_bridge AND
    # GITHUB_TOKEN is set). Job is a no-op otherwise — see opus_bridge_enabled().
    from github_bridge.ingestion import opus_bridge_enabled
    if opus_bridge_enabled():
        scheduler.add_job(
            _bridge_dump_high_ingestion_job,
            trigger=IntervalTrigger(minutes=5),
            id="bridge_dump_high_ingestion",
            name="Opus bridge: dump pending HIGH ingestions",
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            _bridge_pull_completed_ingestion_job,
            trigger=IntervalTrigger(minutes=2),
            id="bridge_pull_completed_ingestion",
            name="Opus bridge: pull completed analyses",
            max_instances=1,
            misfire_grace_time=120,
        )
        scheduler.add_job(
            _bridge_watchdog_timeout_job,
            trigger=IntervalTrigger(minutes=5),
            id="bridge_watchdog_timeout",
            name="Opus bridge: watchdog (timeout stale committed rows)",
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            _bridge_fallback_sweeper_job,
            trigger=IntervalTrigger(minutes=5),
            id="bridge_fallback_sweeper",
            name="Opus bridge: run Gemini on fallback_to_gemini rows",
            max_instances=1,
            misfire_grace_time=300,
        )
        log.info(
            "Opus-bridge HIGH ingestion: dump (5m), pull (2m), watchdog (5m), "
            "fallback sweeper (5m) all registered"
        )

    # Daily analyst-trade expire sweep — marks positions whose expiry has
    # passed as 'expired_unknown' so they drop out of the "currently open"
    # view. Posts a summary to the announce channel if anything was marked.
    # Only registers if the watcher is enabled (analyst_channel_name set).
    if settings.analyst_channel_name:
        scheduler.add_job(
            _analyst_expire_sweep_job,
            trigger=CronTrigger(hour=4, minute=0, timezone=tz),
            id="analyst_expire_sweep",
            name="Analyst log: mark expired positions",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=3600,
        )
        # Startup catch-up: redeploys cycle the in-memory scheduler, so if
        # a deploy happens between cron fires (or right after the 04:00 ET
        # slot), the daily sweep can be missed. Schedule a one-shot run
        # ~2 minutes after boot — `next_run_time=now+2min` triggers once
        # then drops the job. Cheap, defensive, prevents drift.
        from datetime import datetime as _dt, timedelta as _td
        scheduler.add_job(
            _analyst_expire_sweep_job,
            trigger="date",
            run_date=_dt.now(tz) + _td(minutes=2),
            id="analyst_expire_sweep_startup",
            name="Analyst log: startup catch-up expire sweep",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=600,
        )
        # Weekly purge — hard-deletes trade rows that have been marked
        # expired_unknown AND are past their retention window
        # (settings.analyst_trade_retention_days, default 14). Runs Sunday
        # 04:30 local — 30 min after the daily mark sweep so Sunday's
        # newly-expired rows DON'T get purged that same morning.
        if settings.analyst_trade_retention_days > 0:
            scheduler.add_job(
                _analyst_purge_job,
                trigger=CronTrigger(day_of_week="sun", hour=4, minute=30, timezone=tz),
                id="analyst_purge",
                name="Analyst log: purge old expired rows",
                kwargs={"bot": bot},
                max_instances=1,
                misfire_grace_time=3600,
            )

    # Daily user-profile refresh — only registers if profile channels are
    # configured. Refreshes profiles for active users in the yapping
    # channels. Runs 05:00 local every day. The backfill script applies
    # a per-user delta filter (profile_delta_threshold) so users whose
    # message count since last profile hasn't moved enough are skipped —
    # the daily run only re-profiles the people who actually changed.
    if settings.profile_channels:
        scheduler.add_job(
            _user_profile_refresh_job,
            trigger=CronTrigger(hour=5, minute=0, timezone=tz),
            id="user_profile_refresh",
            name="User profiles: refresh active members",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=3600,
        )
        log.info(
            f"User-profile system active — channels '{settings.profile_channels}', "
            f"daily refresh 05:00 {settings.timezone} "
            f"(delta threshold: {settings.profile_delta_threshold} new msgs)"
        )

    # /ask interaction log publisher — every 30 min, push any local
    # ask-logs/YYYY-MM-DD.md files to pulse-data branch for browseable QC.
    # Local append happens per-call inside _answer_with_gemini; this job
    # batches the commits so we don't hammer the GitHub API per question.
    if settings.github_token:
        from apscheduler.triggers.interval import IntervalTrigger
        scheduler.add_job(
            _ask_log_publish_job,
            trigger=IntervalTrigger(minutes=30),
            id="ask_log_publish",
            name="/ask interaction log: publish to pulse-data",
            max_instances=1,
            misfire_grace_time=600,
        )
        log.info(
            f"Analyst trade-log watcher active — channel "
            f"'{settings.analyst_channel_name}', daily expire sweep at 04:00 "
            f"{settings.timezone}, weekly purge Sunday 04:30 "
            f"(retention {settings.analyst_trade_retention_days}d)"
        )

    log.info(
        f"Scheduler configured: "
        f"poll every {settings.dropbox_poll_interval_minutes}min, "
        f"process every {settings.process_interval_minutes}min, "
        f"{'bridge active (Opus routine produces pulses)' if bridge_active else f'daily pulse at {settings.daily_pulse_hour}:{settings.daily_pulse_minute:02d} ET (Gemini)'}"
    )

    return scheduler


async def _poll_dropbox_job():
    """Scheduled job: poll Dropbox for new PDFs (blocking I/O in thread)."""
    import asyncio
    try:
        from dropbox_client.watcher import poll_and_download
        downloaded = await asyncio.to_thread(poll_and_download)
        if downloaded:
            log.info(f"Dropbox poll: {len(downloaded)} new PDFs downloaded")
    except Exception as e:
        log.error(f"Dropbox poll failed: {e}", exc_info=True)


async def _process_queue_job():
    """Scheduled job: process pending PDFs in the queue."""
    try:
        from pipeline.orchestrator import process_pending_queue
        results = await process_pending_queue()
        if results:
            log.info(f"Queue processing: {len(results)} PDFs analyzed")
    except Exception as e:
        log.error(f"Queue processing failed: {e}", exc_info=True)


async def _daily_pulse_job(bot=None):
    """Scheduled job: generate and send Daily Market Pulse."""
    try:
        from pipeline.orchestrator import run_daily_pulse
        report = await run_daily_pulse(bot)
        if report:
            log.info(f"Daily pulse delivered: {report.pdf_count} reports")
        else:
            log.warning("Daily pulse: no reports available")
    except Exception as e:
        log.error(f"Daily pulse failed: {e}", exc_info=True)


async def _reanalyze_processor_job(bot=None):
    """Scheduled job: pick up the next pending/in-progress reanalyze job and
    run it to completion via the orchestrator's job-aware path.

    The orchestrator's `reanalyze_recent_pdfs(job_id=N)` reads the job row
    from DB, skips already-processed PDFs (resume after worker restart),
    and persists progress after each PDF. When the job completes, it posts
    a final status message to the originating Discord channel.

    `max_instances=1` on the scheduler entry means only one of these runs
    at a time — long jobs naturally block subsequent ticks until done,
    which is what we want (one reanalyze at a time, no overlap).
    """
    try:
        import db
        job = db.get_active_reanalyze_job()
        if not job:
            return  # nothing to do
        if job["status"] == "pending":
            db.start_reanalyze_job(job["id"])
            log.info(
                f"Reanalyze job {job['id']}: starting "
                f"(target {job['target_count']} PDFs, hours={job['hours']})"
            )
        else:
            log.info(
                f"Reanalyze job {job['id']}: resuming from prior state "
                f"(target {job['target_count']} PDFs)"
            )

        from pipeline.orchestrator import reanalyze_recent_pdfs
        try:
            stats = await reanalyze_recent_pdfs(
                hours=job["hours"], job_id=job["id"]
            )
        except Exception as e:
            log.error(
                f"Reanalyze job {job['id']} crashed: {e}", exc_info=True
            )
            db.fail_reanalyze_job(job["id"], f"{type(e).__name__}: {e}")
            await _post_reanalyze_completion_message(
                bot, job["id"], status="failed",
                error=f"{type(e).__name__}: {e}",
            )
            return

        # Job-aware orchestrator marked it complete itself — post the final
        # message back to Discord.
        await _post_reanalyze_completion_message(
            bot, job["id"], status="complete", stats=stats,
        )

    except Exception as e:
        log.error(f"Reanalyze processor job failed: {e}", exc_info=True)


async def _post_reanalyze_completion_message(
    bot,
    job_id: int,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    """Post a final completion (or failure) message to the Discord channel
    that originated the /reanalyze. Best-effort — a missing bot, channel,
    or message ID is logged and ignored. The job state is already
    persisted in DB regardless.
    """
    if bot is None:
        return
    import db
    job = db.get_reanalyze_job(job_id)
    if not job:
        return
    cid = job.get("discord_channel_id")
    if not cid:
        return
    try:
        channel = bot.get_channel(int(cid))
        if channel is None:
            log.warning(
                f"Reanalyze job {job_id}: completion channel {cid} not found in cache"
            )
            return
        if status == "complete":
            s = stats or {}
            content = (
                f"**Reanalyze job #{job_id} complete** ({job['hours']}h window)\n"
                f"Target: {job['target_count']} | "
                f"Processed: {s.get('processed', 0)} | "
                f"Failed: {s.get('failed', 0)}"
            )
            if s.get("bridge_queued"):
                content += f" | Bridge queued: {s['bridge_queued']}"
            content += (
                f"\nTokens: {s.get('input_tokens', 0):,} in / "
                f"{s.get('output_tokens', 0):,} out\n"
                f"New analysis rows appended alongside old ones. "
                f"Run `/pulse` to see synthesis with refreshed data."
            )
        else:
            content = (
                f"**Reanalyze job #{job_id} FAILED** ({job['hours']}h window)\n"
                f"Error: {(error or 'unknown')[:300]}\n"
                f"Progress preserved in DB. The job marked itself failed; "
                f"running `/reanalyze` again will start a fresh job."
            )
        # Try to edit the original status message if we have one; fall back
        # to a new post in the same channel.
        msg_id = job.get("discord_status_message_id")
        if msg_id:
            try:
                msg = await channel.fetch_message(int(msg_id))
                await msg.edit(content=content[:1900])
                return
            except Exception:
                pass  # fall through to new post
        await channel.send(content[:1900])
    except Exception as e:
        log.warning(
            f"Reanalyze job {job_id}: failed to post completion message: {e}"
        )


async def _bridge_post_pending_job(bot=None):
    """Scheduled job: poll GitHub bridge pending/, post any new pulses to Discord."""
    try:
        from github_bridge.jobs import post_pending_pulses_job
        await post_pending_pulses_job(bot=bot)
    except Exception as e:
        log.error(f"GitHub bridge post-pending failed: {e}", exc_info=True)


async def _ingest_feed_tick_job(bot=None):
    """Scheduled job: announce next pending HIGH/MEDIUM ingestion to the feed channel."""
    try:
        from discord_bot.ingestion_feed import announce_next_pending
        await announce_next_pending(bot=bot)
    except Exception as e:
        log.error(f"Ingestion feed tick failed: {e}", exc_info=True)


async def _bridge_dump_high_ingestion_job():
    """Scheduled job: package pending HIGH PDFs to the GitHub bridge.

    Sync function wrapped in asyncio.to_thread because it does blocking
    I/O (urllib calls to GitHub API + local PDF reads).
    """
    import asyncio
    try:
        from github_bridge.ingestion import dump_pending_high_ingestions_job
        await asyncio.to_thread(dump_pending_high_ingestions_job)
    except Exception as e:
        log.error(f"Bridge HIGH-ingestion dump failed: {e}", exc_info=True)


async def _bridge_pull_completed_ingestion_job():
    """Scheduled job: pull completed Opus analyses from the bridge into the DB."""
    import asyncio
    try:
        from github_bridge.ingestion import pull_completed_ingestions_job
        await asyncio.to_thread(pull_completed_ingestions_job)
    except Exception as e:
        log.error(f"Bridge HIGH-ingestion pull failed: {e}", exc_info=True)


async def _bridge_watchdog_timeout_job():
    """Scheduled job: convert stale 'committed' rows into 'fallback_to_gemini'."""
    import asyncio
    try:
        from github_bridge.ingestion import watchdog_timeout_committed_job
        await asyncio.to_thread(watchdog_timeout_committed_job)
    except Exception as e:
        log.error(f"Bridge watchdog failed: {e}", exc_info=True)


async def _bridge_fallback_sweeper_job():
    """Scheduled job: run Gemini deep-analysis on rows tagged fallback_to_gemini.

    The sweeper is itself async (calls async Gemini path) — no asyncio.to_thread.
    """
    try:
        from github_bridge.ingestion import fallback_sweeper_job
        await fallback_sweeper_job()
    except Exception as e:
        log.error(f"Bridge fallback sweeper failed: {e}", exc_info=True)


async def _ask_log_publish_job():
    """Push today's (and yesterday's, near the boundary) /ask interaction
    log files from /data/ask-logs/ to pulse-data:ask-logs/ on GitHub for
    browseable QC. Runs every 30 min.

    Idempotent: rewrites whatever's at the path with the current local
    content. No-ops cheaply if the file is unchanged (GitHub's put_file
    still commits but the content blob sha stays the same).
    """
    try:
        from pathlib import Path
        from datetime import datetime, timezone, timedelta
        from config import settings as _settings
        from github_bridge import client as gh_client

        base_dir = Path(_settings.pdf_download_dir).resolve().parent
        log_dir = base_dir / "ask-logs"
        if not log_dir.exists():
            log.debug("ask-log publish: no local log dir, nothing to push")
            return

        # Push today's + yesterday's files — covers any late writes near
        # the UTC date boundary. Older days were already pushed (or never
        # written if the bot was down then) and don't need re-pushing.
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        targets = [today, yesterday]

        pushed = 0
        for date_str in targets:
            local_path = log_dir / f"{date_str}.md"
            if not local_path.exists():
                continue
            try:
                content = local_path.read_text(encoding="utf-8")
            except Exception as e:
                log.warning(f"ask-log publish: couldn't read {local_path}: {e}")
                continue
            if not content.strip():
                continue
            try:
                gh_client.put_file(
                    path=f"ask-logs/{date_str}.md",
                    content=content,
                    message=f"ask-log: snapshot {date_str}",
                )
                pushed += 1
            except Exception as e:
                log.warning(
                    f"ask-log publish: GitHub commit failed for {date_str}: {e}"
                )
        if pushed:
            log.info(f"ask-log publish: pushed {pushed} day file(s) to pulse-data")
    except Exception as e:
        log.error(f"ask-log publish job failed: {e}", exc_info=True)


async def _user_profile_refresh_job(bot=None):
    """Weekly cron (Sunday 05:00 local). Re-runs the profile backfill for
    active users, then prunes anyone outside the top-N cutoff.

    The backfill upserts new + existing profiles for users above the
    20-message threshold AND in the top settings.max_user_profiles. After
    upsert, prune_user_profiles_to_top_n drops any older profiles whose
    activity has fallen below the cutoff so the table stays bounded.
    """
    try:
        from scripts.backfill_user_profiles import run as backfill_run
        import db
        channels = [
            c.strip() for c in (settings.profile_channels or "").split(",")
            if c.strip()
        ]
        if not channels:
            return
        log.info(f"User-profile refresh: scanning {channels} for "
                 f"{settings.profile_window_days}d")
        await backfill_run(settings.profile_window_days, channels)
        # Prune to top N by message_count_at_update
        pruned = db.prune_user_profiles_to_top_n(settings.max_user_profiles)
        if pruned:
            log.info(
                f"User-profile refresh: pruned {len(pruned)} below-cutoff "
                f"profiles: "
                f"{[p.get('display_name') or p.get('username') for p in pruned[:10]]}"
            )

        # Publish a markdown snapshot to GitHub (pulse-data branch) so
        # users can read the current dossier set without shell access.
        # pulse-data is intentionally NOT the working branch — committing
        # here doesn't trigger a Railway redeploy. Failure is non-fatal:
        # if the bridge is misconfigured, the in-DB profiles are still
        # the source of truth for /ask.
        if settings.github_token:
            try:
                from github_bridge import client as gh_client
                from datetime import datetime, timezone
                md = db.export_user_profiles_markdown()
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                gh_client.put_file(
                    path="profile-snapshots/user-profiles-snapshot.md",
                    content=md,
                    message=f"profiles: daily snapshot {stamp}",
                )
                log.info(
                    "User-profile refresh: published snapshot to "
                    "pulse-data:profile-snapshots/user-profiles-snapshot.md"
                )
            except Exception as e:
                log.warning(
                    f"User-profile refresh: snapshot publish failed "
                    f"(non-fatal, DB profiles still current): {e}"
                )
    except Exception as e:
        log.error(f"User-profile refresh failed: {e}", exc_info=True)


async def _analyst_purge_job(bot=None):
    """Weekly cron (Sunday 04:30 local). Hard-deletes analyst_trades rows
    that are marked expired_unknown AND have been past expiry for more
    than settings.analyst_trade_retention_days. Announces the count to
    the configured channel if anything was purged.
    """
    try:
        import db
        retention = settings.analyst_trade_retention_days
        if retention <= 0:
            return
        purged = db.purge_old_expired_analyst_trades(days_after_expiry=retention)
        if not purged:
            log.info("Analyst purge: nothing to delete")
            return
        log.info(f"Analyst purge: deleted {len(purged)} rows")
        if bot is None:
            return
        chan_name = (settings.analyst_test_announce_channel or "").strip()
        if not chan_name:
            return
        target = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == chan_name.lower():
                    target = ch
                    break
            if target:
                break
        if target is None:
            log.warning(f"Analyst purge: announce channel '{chan_name}' not found")
            return
        # Group by ticker for a compact summary
        by_ticker: dict[str, int] = {}
        for r in purged:
            tk = r.get("ticker") or "?"
            by_ticker[tk] = by_ticker.get(tk, 0) + 1
        ticker_summary = ", ".join(
            f"{tk}×{n}" for tk, n in sorted(by_ticker.items(), key=lambda x: -x[1])[:10]
        )
        body = (
            f"🧹 **Analyst log purge** — deleted {len(purged)} expired rows "
            f"older than {retention}d. By ticker: {ticker_summary}"
        )
        try:
            await target.send(body[:1900])
        except Exception as e:
            log.error(f"Analyst purge: announce failed: {e}")
    except Exception as e:
        log.error(f"Analyst purge failed: {e}", exc_info=True)


async def _analyst_expire_sweep_job(bot=None):
    """Daily cron: mark any analyst-trade row whose expiry has passed as
    'expired_unknown'. Posts a summary to the announce channel if anything
    was marked. Handles the case-C failure mode where the analyst forgets to
    post a close — once the contract expires we drop it from the
    "currently open" view by setting inferred_status.
    """
    try:
        import db
        expired_rows = db.mark_expired_analyst_positions()
        if not expired_rows:
            log.info("Analyst expire sweep: nothing to mark")
            return
        log.info(f"Analyst expire sweep: marked {len(expired_rows)} rows as expired_unknown")
        if bot is None:
            return
        # Format announce — one-line per ticker/contract, no duplicates
        seen: set[tuple] = set()
        lines = []
        for r in expired_rows:
            key = (r.get("ticker"), r.get("contract_type"),
                   r.get("strike"), r.get("expiry"))
            if key in seen:
                continue
            seen.add(key)
            tk = r.get("ticker") or "?"
            ct = (r.get("contract_type") or "").lower()
            ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
            strike = r.get("strike")
            strike_s = (
                f"{int(strike) if strike == int(strike) else strike}"
                if strike is not None else "?"
            )
            exp = r.get("expiry") or "?"
            lines.append(f"   • {tk} {strike_s}{ct_suffix} {exp}")
        if not lines:
            return
        body = (
            f"🗓️ **Analyst log auto-expire** — {len(seen)} contracts past "
            f"expiry, marked as `expired_unknown`:\n" + "\n".join(lines[:20])
        )
        if len(lines) > 20:
            body += f"\n   • ... and {len(lines) - 20} more"

        chan_name = (settings.analyst_test_announce_channel or "").strip()
        if not chan_name:
            return
        target = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == chan_name.lower():
                    target = ch
                    break
            if target:
                break
        if target is None:
            log.warning(f"Analyst expire sweep: announce channel '{chan_name}' not found")
            return
        try:
            await target.send(body[:1900])
        except Exception as e:
            log.error(f"Analyst expire sweep: announce failed: {e}")
    except Exception as e:
        log.error(f"Analyst expire sweep failed: {e}", exc_info=True)
