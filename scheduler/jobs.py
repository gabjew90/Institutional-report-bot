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
        log.info("Opus-bridge HIGH ingestion: dump job registered (every 5 min)")

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
