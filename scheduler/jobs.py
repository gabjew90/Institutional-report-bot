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
        # 2026-06-02: moved from daily 04:00 AM -> daily 16:00 ET (4 PM,
        # market close). All options expiring "today" expire exactly at
        # market close, so one sweep right after close catches every
        # silent-expiry the same day instead of waiting until 4 AM
        # tomorrow (12h lag with the old schedule). Hourly cadence was
        # considered but redundant — expirations only happen at one
        # moment per day, not continuously through the day.
        scheduler.add_job(
            _analyst_expire_sweep_job,
            trigger=CronTrigger(hour=16, minute=0, timezone=tz),
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

    # Daily chat_messages retention purge. Deletes rows older than
    # settings.chat_retention_days (default 180) to keep the table
    # bounded. Runs at 04:00 local — well outside trading hours, also
    # before any other scheduled work hits the DB. Skips entirely when
    # retention is 0 (purge disabled).
    if getattr(settings, "chat_retention_days", 0) > 0:
        scheduler.add_job(
            _chat_purge_job,
            trigger=CronTrigger(hour=4, minute=0, timezone=tz),
            id="chat_messages_purge",
            name="chat_messages: daily retention purge",
            max_instances=1,
            misfire_grace_time=3600,
        )

    # Daily append-only retention purge. Trims pdf_analyses,
    # processing_log, pipeline_events, daily_reports (manual only).
    # Runs at 04:15 local, 15 min after the chat purge so the two
    # don't contend for the SQLite write lock. Without this, the
    # tables grow unbounded — CLAUDE.md TODO line 282 has flagged
    # processing_log specifically; pdf_analyses grows via the
    # /reanalyze append-only design; pipeline_events grows from
    # every Gemini call's token-budget event.
    scheduler.add_job(
        _retention_purge_job,
        trigger=CronTrigger(hour=4, minute=15, timezone=tz),
        id="retention_purge",
        name="append-only tables: daily retention purge",
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Daily user-profile refresh. Always registered now — the profile
    # builder reads ALL ingested channels in chat_messages (no narrower
    # filter). Runs 15:00 local every day. The backfill script applies
    # a per-user delta filter (profile_delta_threshold) so users whose
    # message count since last profile hasn't moved enough are skipped —
    # the daily run only re-profiles the people who actually changed.
    # 2026-06-02: bumped from daily 15:00 -> every 6h (4x per day at
    # 03:00 / 09:00 / 15:00 / 21:00 local). With the new text-extraction
    # pipeline writing analyst_trades rows in real-time and the
    # wins-only +2 / 7d ledger, trader_score is now more responsive to
    # live trade activity. Daily refresh meant a user could rip 5
    # winning trades at 4 PM ET and his score wouldn't move until 3 PM
    # the next day. 6h cadence cuts that staleness to <6h. The
    # profile_delta_threshold gate still filters out users without
    # meaningful activity since their last refresh, so most 6h ticks
    # only re-profile a handful of users.
    scheduler.add_job(
        _user_profile_refresh_job,
        trigger=CronTrigger(hour="3,9,15,21", minute=0, timezone=tz),
        id="user_profile_refresh",
        name="User profiles: refresh active members",
        kwargs={"bot": bot},
        max_instances=1,
        misfire_grace_time=3600,
    )
    log.info(
        f"User-profile system active — ALL ingested channels, "
        f"refresh every 6h (03/09/15/21 {settings.timezone}) "
        f"(delta threshold: {settings.profile_delta_threshold} new msgs)"
    )

    # 2026-06-02: periodic chat catchup backstop. chat_ingestion's
    # run_chat_catchup runs on bot.on_ready / on_resumed only — if the
    # bot stays connected to Discord's gateway with no flap, no catchup
    # runs. Today's zhawk-thawghts perm-fix surfaced this: backfill
    # only happened on redeploy. Periodic backstop (every 4h) ensures
    # gap detection runs at most every 4h even on steady-state bots.
    # MAX(latest_stored_posted_at) resume keeps the scan cheap.
    scheduler.add_job(
        _chat_catchup_periodic_job,
        trigger=IntervalTrigger(hours=4),
        id="chat_catchup_periodic",
        name="chat ingestion: periodic catchup backstop",
        kwargs={"bot": bot},
        max_instances=1,
        misfire_grace_time=1800,
    )

    # /ask interaction log publisher — every 30 min, push any local
    # ask-logs/YYYY-MM-DD.md files to pulse-data branch for browseable QC.
    # Local append happens per-call inside _answer_with_gemini; this job
    # batches the commits so we don't hammer the GitHub API per question.
    if settings.github_token:
        scheduler.add_job(
            _ask_log_publish_job,
            trigger=IntervalTrigger(minutes=30),
            id="ask_log_publish",
            name="/ask interaction log: publish to pulse-data",
            max_instances=1,
            misfire_grace_time=600,
        )

    # Note: slur-count + trader-ranking are now computed as part of the
    # daily profile refresh (folded into backfill_user_profiles.py).
    # No separate metrics cron needed — the data lives on user_profiles.
        log.info(
            f"Analyst trade-log watcher active — channel "
            f"'{settings.analyst_channel_name}', daily expire sweep at 04:00 "
            f"{settings.timezone}, weekly purge Sunday 04:30 "
            f"(retention {settings.analyst_trade_retention_days}d)"
        )

    # /ask QC sub-agent — daily 03:00 ET grader. Reads yesterday's
    # /ask log, runs Gemini judge over each interaction, publishes
    # report to pulse-data:ask-qc/. Independent of github_token — we
    # still write locally for inspection even without a push target.
    # See ask_qc/ + docs/superpowers/specs/2026-06-02-ask-qc-subagent-design.md.
    scheduler.add_job(
        _ask_qc_job,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="ask_qc",
        name="/ask QC: grade yesterday's interactions",
        max_instances=1,
        misfire_grace_time=3600,
    )

    # Channel reminder system — daily 3:45 PM ET, posts due calendar
    # reminders (reminders/calendar.json) to REMINDER_CHANNEL_ID. The
    # job no-ops when the channel isn't configured, so registering it
    # unconditionally is safe.
    if settings.reminder_channel_id:
        from reminders.job import reminder_check_job
        scheduler.add_job(
            reminder_check_job,
            trigger=CronTrigger(hour=15, minute=45, timezone=tz),
            id="reminder_check",
            name="Channel reminders: post due calendar events",
            kwargs={"bot": bot},
            max_instances=1,
            misfire_grace_time=3600,
        )
        log.info(
            f"Reminder system active — daily 15:45 {settings.timezone} "
            f"to channel {settings.reminder_channel_id}"
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


async def _ask_qc_job():
    """Daily 03:00 ET - grade yesterday's /ask interactions.

    Reads /data/ask-logs/{yesterday-UTC}.md, runs the Gemini judge
    over every interaction, renders a markdown report, writes it
    locally to /data/ask-qc/{date}.md, pushes to pulse-data:ask-qc/.

    Graceful degradation:
      - Missing log file -> log + exit
      - Empty log file (0 interactions) -> write stub locally,
        skip GitHub push
      - Gemini failures per interaction -> UNGRADED in report
      - GitHub push failure -> log WARNING, don't raise (local
        file is source of truth)

    Records a pipeline_events row on completion with the daily stats."""
    from pathlib import Path
    from datetime import datetime, timezone, timedelta
    from config import settings as _settings
    import db as _db

    try:
        # Yesterday UTC - the day whose log file is now closed
        now = datetime.now(timezone.utc)
        date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        base_dir = Path(_settings.pdf_download_dir).resolve().parent
        log_dir = base_dir / "ask-logs"
        qc_dir = base_dir / "ask-qc"
        qc_dir.mkdir(parents=True, exist_ok=True)

        log_path = log_dir / f"{date}.md"
        if not log_path.exists():
            log.info(f"ask-qc: no log for {date}, nothing to grade")
            return

        from ask_qc.parser import parse_ask_log
        from ask_qc.grader import grade_day
        from ask_qc.aggregator import render_report

        text = log_path.read_text(encoding="utf-8")
        interactions = parse_ask_log(text)
        log.info(f"ask-qc: grading {date} ({len(interactions)} interactions)")

        if not interactions:
            report = render_report(date, [])
            (qc_dir / f"{date}.md").write_text(report, encoding="utf-8")
            log.info(f"ask-qc: 0 interactions on {date}, wrote stub, skipping push")
            _db.record_pipeline_event(
                "ask_qc", "completed",
                {"date": date, "interactions_total": 0,
                 "interactions_graded": 0, "interactions_ungraded": 0,
                 "github_pushed": False},
            )
            return

        graded = await grade_day(interactions, sem_size=3)
        report = render_report(date, graded)
        (qc_dir / f"{date}.md").write_text(report, encoding="utf-8")

        # Push to pulse-data:ask-qc/. Best-effort; local file is source of truth.
        pushed = False
        if _settings.github_token:
            try:
                from github_bridge import client as gh_client
                gh_client.put_file(
                    path=f"ask-qc/{date}.md",
                    content=report,
                    message=f"ask-qc: snapshot {date}",
                )
                pushed = True
            except Exception as e:
                log.warning(f"ask-qc: GitHub push failed for {date}: {e}")

        # Verdict tallies for the pipeline_event row
        from collections import Counter
        counts = Counter(g.overall_verdict for g in graded)
        ungraded = counts.get("UNGRADED", 0)
        _db.record_pipeline_event(
            "ask_qc", "partial" if ungraded > 0 else "completed",
            {
                "date": date,
                "interactions_total": len(interactions),
                "interactions_graded": len(graded) - ungraded,
                "interactions_ungraded": ungraded,
                "clean": counts.get("CLEAN", 0),
                "concern": counts.get("CONCERN", 0),
                "fail": counts.get("FAIL", 0),
                "github_pushed": pushed,
            },
        )
        log.info(
            f"ask-qc: done {date} - {counts.get('CLEAN', 0)}/"
            f"{counts.get('CONCERN', 0)}/{counts.get('FAIL', 0)}/"
            f"{ungraded} (clean/concern/fail/ungraded), "
            f"pushed={pushed}"
        )
    except Exception as e:
        log.error(f"ask-qc job failed: {e}", exc_info=True)
        try:
            _db.record_pipeline_event(
                "ask_qc", "failed",
                {"error": f"{type(e).__name__}: {e}"},
            )
        except Exception:
            pass


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


async def _user_profile_refresh_job(bot=None, force: bool = False):
    """Daily cron (15:00 local / 3 PM ET). Re-runs the profile backfill for
    active users, then prunes anyone outside the top-N cutoff.

    The backfill upserts new + existing profiles for users above the
    20-message threshold AND in the top settings.max_user_profiles. After
    upsert, prune_user_profiles_to_top_n drops any older profiles whose
    activity has fallen below the cutoff so the table stays bounded.

    `force=True` (from /refresh_profiles force:true) bypasses the
    20-msg delta gate — every user above the 30-msg lifetime floor
    gets re-profiled. Used after a prompt change to refresh all
    existing dossiers in one shot.
    """
    try:
        from scripts.backfill_user_profiles import run as backfill_run
        import db
        # Channels=[] → backfill reads ALL chat_messages within the
        # window. Profile builder no longer scopes to profile_channels.
        log.info(
            f"User-profile refresh: scanning ALL ingested channels for "
            f"{settings.profile_window_days}d (force={force})"
        )
        await backfill_run(settings.profile_window_days, [], force=force)
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


async def _chat_catchup_periodic_job(bot=None):
    """Periodic backstop (every 4h) for the chat-ingestion catchup
    that normally only fires on bot.on_ready / on_resumed events.

    Today's zhawk-thawghts perm-fix surfaced the gap: when the bot
    stays connected to Discord's gateway with no flap, catchup never
    runs. A channel that becomes accessible mid-day (perm grant, name
    change, etc.) won't get backfilled until the bot restarts.

    Cheap: MAX(latest_stored_posted_at) per channel limits scan window.
    A typical 4h gap is ~50-200 messages per channel × 12 channels.
    Idempotent via UNIQUE(discord_message_id) — duplicates silently skip.
    """
    if bot is None:
        log.warning("chat_catchup_periodic: bot=None, skipping")
        return
    try:
        from chat_ingestion.watcher import run_chat_catchup
        # force=True bypasses the 2-min rate limit guard since periodic
        # invocations are at 4h spacing which the guard doesn't anticipate.
        n = await run_chat_catchup(bot, reason="periodic", force=True)
        log.info(f"chat_catchup_periodic: stored {n} new rows across channels")
    except Exception as e:
        log.error(f"chat_catchup_periodic failed: {e}", exc_info=True)


async def _chat_purge_job():
    """Daily cron (04:00 local). Deletes chat_messages rows older than
    settings.chat_retention_days. Logs the count purged. No-op when
    retention is 0 (purge disabled).

    The table grows ~8-15k rows/day across the configured channels.
    180 days = ~2M rows / ~400 MB on the /data volume — fine. Older
    history isn't useful for the profile-refresh windows (30d) or
    /ask claim verification (recent quotes), so dropping it keeps
    queries fast and storage bounded.
    """
    try:
        import db
        retention = int(getattr(settings, "chat_retention_days", 0) or 0)
        if retention <= 0:
            log.debug("chat_messages purge: retention=0, skipping")
            return
        deleted = db.purge_old_chat_messages(retention)
        if deleted > 0:
            log.info(
                f"chat_messages purge: deleted {deleted} rows older "
                f"than {retention} days"
            )
        else:
            log.debug(
                f"chat_messages purge: nothing older than {retention} days"
            )
    except Exception as e:
        log.error(f"chat_messages purge failed: {e}", exc_info=True)


async def _retention_purge_job():
    """Daily cron (04:15 local) — append-only table retention.

    Trims four tables that grow unbounded by design:

      pdf_analyses     — keeps the latest 2 rows per pdf_file_id.
                          Re-analyses leave history rows around for
                          forensics; only the most-recent two are
                          useful (current + immediate prior for diff).
      processing_log   — drops rows older than 30 days. Forensic
                          audit trail; no use case for older.
      pipeline_events  — drops rows older than 90 days. Same reasoning;
                          90 covers two QC review-cycles of history.
      daily_reports    — keeps `report_type='daily'` rows indefinitely
                          (small volume, ~1 per day), drops
                          `report_type='manual'` rows older than 30
                          days. Manual /pulse runs are test artifacts.

    Run sequentially with explicit COMMIT between each so a failure
    on one table doesn't roll back the others. Counts logged so
    operators can see retention biting (if a count jumps suddenly,
    something upstream is generating noise).
    """
    try:
        import db
        results = db.run_retention_purge()
        for table, count in (results or {}).items():
            if count > 0:
                log.info(f"retention purge: {table} dropped {count} rows")
            else:
                log.debug(f"retention purge: {table} clean")
    except Exception as e:
        log.error(f"retention purge job failed: {e}", exc_info=True)


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
        log.info(
            f"Analyst expire sweep: marked {len(expired_rows)} rows as "
            f"expired_unknown (announcement disabled)"
        )
        # Announcement disabled per user preference — the expire-sweep
        # logic still runs (positions get dropped from "currently open"
        # via the inferred_status flag), but no embed gets posted. If
        # you want to see what was marked, check the log line above or
        # query analyst_trades WHERE inferred_status='expired_unknown'.
        return
    except Exception as e:
        log.error(f"Analyst expire sweep failed: {e}", exc_info=True)
