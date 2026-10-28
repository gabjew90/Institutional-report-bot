"""Entry point of the batch process: `python -m scheduler.batch_main`.

Started and supervised by the bot process (scheduler/batch_proc.py). Runs
the scheduler with only the batch jobs. No Discord client of its own; the
profile refresh opens its own gateway session, as it always has, and ops
pings go through the REST helper.
"""
import asyncio
import logging
import os
import signal
import sys

from config import settings

PARENT_CHECK_S = 30


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] batch %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def main() -> None:
    setup_logging()
    log = logging.getLogger("batch")
    import db
    db.assume_schema_ready()
    from scheduler.jobs import setup_scheduler

    scheduler = setup_scheduler(None, role="batch")
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
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), PARENT_CHECK_S)
        except asyncio.TimeoutError:
            if os.getppid() != parent:
                log.warning("Bot process is gone; batch process exiting")
                break
    scheduler.shutdown(wait=False)
    log.info("Batch process stopped")


if __name__ == "__main__":
    asyncio.run(main())
