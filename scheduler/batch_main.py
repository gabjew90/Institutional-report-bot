"""Entry point of the batch process: `python -m scheduler.batch_main`.

Started and supervised by the bot process (scheduler/batch_proc.py). Runs
the scheduler with only the batch jobs. No Discord client of its own; the
profile refresh opens its own gateway session, as it always has, and ops
pings go through the REST helper.
"""
import runtime_env  # noqa: F401  first: thread limits must be set before numpy loads

import asyncio
import logging
import os
import signal
import sys
import time

from config import settings

PARENT_CHECK_S = 30


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] batch %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _recycle_now(scheduler, jobs, started: float, log) -> bool:
    """Whether to end this process so a fresh one replaces it
    (scheduler/batch_proc.py, Recycling). Trims first, so memory the allocator
    can hand back is not a reason to restart."""
    import memtrim
    from scheduler import batch_proc as BP
    limit = int(getattr(settings, "batch_recycle_rss_mb", 0) or 0)
    uptime = time.monotonic() - started
    # The cheap conditions first: nothing is trimmed while a job is running.
    if not BP.recycle_due(rss_mb=memtrim.rss_mb(), limit_mb=limit, uptime_s=uptime,
                          running_jobs=jobs.count, next_cron_s=BP.next_cron_seconds(scheduler)):
        return False
    memtrim.trim()
    rss = memtrim.rss_mb()
    if not BP.recycle_due(rss_mb=rss, limit_mb=limit, uptime_s=uptime,
                          running_jobs=jobs.count, next_cron_s=BP.next_cron_seconds(scheduler)):
        return False
    log.info(f"Batch process recycling: {rss:.0f} MB resident, over the {limit} MB limit after "
             f"{uptime / 60:.0f} min, no job running; exiting so the supervisor starts a fresh one")
    return True


async def main() -> int:
    setup_logging()
    log = logging.getLogger("batch")
    started = time.monotonic()
    import db
    db.assume_schema_ready()
    from scheduler.batch_proc import JobCounter, RECYCLE_EXIT_CODE
    from scheduler.jobs import setup_scheduler

    scheduler = setup_scheduler(None, role="batch")
    jobs = JobCounter().attach(scheduler)
    scheduler.start()
    log.info(f"Batch process running {len(scheduler.get_jobs())} job(s)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))

    # Exit when the bot process is gone, so a crashed parent never leaves a
    # second ingestion loop running beside the one its replacement starts.
    parent = os.getppid()
    code = 0
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), PARENT_CHECK_S)
        except asyncio.TimeoutError:
            if os.getppid() != parent:
                log.warning("Bot process is gone; batch process exiting")
                break
            try:
                if _recycle_now(scheduler, jobs, started, log):
                    code = RECYCLE_EXIT_CODE
                    break
            except Exception as e:  # a fault in the check never stops the jobs
                log.warning(f"recycle check failed: {e}")
    scheduler.shutdown(wait=False)
    log.info("Batch process stopped")
    return code


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
