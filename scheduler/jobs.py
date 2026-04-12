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
    scheduler.add_job(
        _daily_pulse_job,
        trigger=CronTrigger(
            hour=settings.daily_pulse_hour,
            minute=settings.daily_pulse_minute,
            timezone=tz,
        ),
        id="daily_pulse",
        name="Daily Market Pulse",
        kwargs={"bot": bot},
        max_instances=1,
        misfire_grace_time=600,
    )

    log.info(
        f"Scheduler configured: "
        f"poll every {settings.dropbox_poll_interval_minutes}min, "
        f"process every {settings.process_interval_minutes}min, "
        f"daily pulse at {settings.daily_pulse_hour}:{settings.daily_pulse_minute:02d} ET"
    )

    return scheduler


async def _poll_dropbox_job():
    """Scheduled job: poll Dropbox for new PDFs."""
    try:
        from dropbox_client.watcher import poll_and_download
        downloaded = poll_and_download()
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
