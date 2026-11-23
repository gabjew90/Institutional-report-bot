"""The batch process (2026-10-27).

Until this date one Python process ran the Discord bot and every scheduled
job. Three times on 2026-10-27 a burst of research broke /ask: the PDF
backlog used up the shared Gemini budget, budget refusals burned retries,
and in the evening the bridge's theme build over 473 notes held the CPU
while the profile refresh read the chat table on the Discord thread, which
froze the gateway for over 25 minutes. Earlier freezes (2026-08-10, 09-09)
were fixed one blocking call at a time.

Python runs one thread at a time per process, so a CPU-heavy job in a
worker thread still takes turns from the Discord thread. A child process
does not: it has its own interpreter, memory and scheduler. The jobs in
BATCH_JOB_IDS run only in the child (scheduler/batch_main.py), and the bot
process runs everything else. Both open the same SQLite database in WAL
mode, which supports several processes. The child skips the schema pass
the bot process already ran at boot (db.assume_schema_ready).

Every caller of the "default" token budget pool (PDF triage and deep
analysis, the lane's Gemini fallback) is in the child, so that pool's
daily count stays in one process. /reanalyze stays in the bot process
with its own count, since it is manual and rare.

BatchSupervisor starts the child, restarts it with backoff when it exits,
pings ops when it does, and stops it on shutdown.
`BATCH_PROCESS_ENABLED=false` runs every job in one process again.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

log = logging.getLogger(__name__)

# Scheduler job ids that run in the batch process and nowhere else.
BATCH_JOB_IDS = frozenset({
    "poll_dropbox",
    "process_queue",
    "bridge_dump_context",
    "claude_lane_pull",
    "claude_lane_fallback",
    "claude_lane_pre_pulse_sweep_et",
    "claude_lane_pre_pulse_sweep_utc",
    "bridge_dump_high_ingestion",
    "bridge_pull_completed_ingestion",
    "bridge_watchdog_timeout",
    "bridge_fallback_sweeper",
    "user_profile_refresh",
    "report_consensus_capture",
})
# Per-process housekeeping both processes run.
SHARED_JOB_IDS = frozenset({"malloc_trim"})

# Recycling (2026-11-23, owner: "go on 1").
#
# Railway bills the container's memory, and the batch process keeps what its
# heaviest job needed. Its own trim log: 320 MB before the 21:00 ET profile
# refresh on 11-23, 618 MB after it, and malloc_trim gets back about 15 MB of
# that. Two days after a deploy it sat at 780 to 980 MB. A fresh process is
# 150 MB. The worker's memory line went $7.15, $9.71, $12.80 over three
# invoices (NOTES.md, 2026-11-23 night).
#
# So the child ends itself when it is over `BATCH_RECYCLE_RSS_MB` with nothing
# running, and the supervisor starts a fresh one at once, with no ops page.
# What an exit can cost, and what stops it:
#   - a job cut off mid-run: only when no job is running (JobCounter, exact,
#     from the scheduler's own submit and finish events);
#   - a cron job whose minute falls in the two seconds between processes:
#     only when no cron job is due within RECYCLE_CRON_CLEAR_S. Interval jobs
#     cannot be missed, their clock restarts with the process, so the next
#     Dropbox poll can be up to one interval later;
#   - a loop of restarts when the limit is set below what the process needs:
#     never before RECYCLE_MIN_UPTIME_S, and the supervisor treats a recycle
#     exit sooner than RECYCLE_MIN_RUN_S as a failure, with backoff and a page.
RECYCLE_EXIT_CODE = 75
RECYCLE_MIN_UPTIME_S = 1200
RECYCLE_CRON_CLEAR_S = 60
RECYCLE_MIN_RUN_S = 600
# This many recycles inside the window means the limit is below what the
# process needs between jobs. One ops page says so. The restarts go on.
RECYCLE_CHURN_COUNT = 4
RECYCLE_CHURN_WINDOW_S = 3 * 3600


def recycle_due(*, rss_mb: float | None, limit_mb: int, uptime_s: float,
                running_jobs: int, next_cron_s: float | None) -> bool:
    """Whether the batch process should end itself now. `limit_mb` 0 turns it off."""
    if not limit_mb or limit_mb <= 0 or rss_mb is None:
        return False
    if rss_mb <= limit_mb or uptime_s < RECYCLE_MIN_UPTIME_S or running_jobs > 0:
        return False
    return next_cron_s is None or next_cron_s > RECYCLE_CRON_CLEAR_S


class JobCounter:
    """How many scheduler jobs are running in this process right now."""

    def __init__(self):
        self.running: dict[str, int] = {}

    def attach(self, scheduler) -> "JobCounter":
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED
        scheduler.add_listener(self._on_event,
                               EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)
        self._submitted = EVENT_JOB_SUBMITTED
        return self

    def _on_event(self, event) -> None:
        job_id = str(getattr(event, "job_id", ""))
        if event.code == self._submitted:
            self.running[job_id] = self.running.get(job_id, 0) + 1
        elif self.running.get(job_id, 0) > 1:
            self.running[job_id] -= 1
        else:
            self.running.pop(job_id, None)

    @property
    def count(self) -> int:
        return sum(self.running.values())


def next_cron_seconds(scheduler, now=None) -> float | None:
    """Seconds until the next cron-triggered job, or None when there is none."""
    from datetime import datetime, timezone
    from apscheduler.triggers.cron import CronTrigger
    now = now or datetime.now(timezone.utc)
    due = [j.next_run_time for j in scheduler.get_jobs()
           if isinstance(j.trigger, CronTrigger) and getattr(j, "next_run_time", None)]
    if not due:
        return None
    return (min(due) - now).total_seconds()


RESTART_DELAYS_S = (10, 30, 60, 120, 300, 600)
# A child that ran this long before exiting resets the backoff.
STABLE_AFTER_S = 600
STOP_TIMEOUT_S = 20


def keeps_job(job_id: str, role: str) -> bool:
    """Whether a process in `role` ("all", "bot" or "batch") runs `job_id`."""
    if role == "all" or job_id in SHARED_JOB_IDS:
        return True
    if role == "batch":
        return job_id in BATCH_JOB_IDS
    return job_id not in BATCH_JOB_IDS


def restart_delay(failures: int) -> int:
    return RESTART_DELAYS_S[min(max(failures, 1), len(RESTART_DELAYS_S)) - 1]


class BatchSupervisor:
    def __init__(self, argv: list[str] | None = None):
        self.argv = argv or [sys.executable, "-m", "scheduler.batch_main"]
        self.proc: asyncio.subprocess.Process | None = None
        self._stopping = False
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def _run(self) -> None:
        failures = 0
        recycles: list[float] = []
        while not self._stopping:
            started = time.monotonic()
            rc: int | None = None
            try:
                self.proc = await asyncio.create_subprocess_exec(
                    *self.argv, env={**os.environ, "BATCH_CHILD": "1"})
                log.info(f"Batch process started (pid {self.proc.pid})")
                rc = await self.proc.wait()
            except Exception as e:
                log.error(f"Batch process could not start: {e}")
            if self._stopping:
                return
            ran = time.monotonic() - started
            if rc == RECYCLE_EXIT_CODE and ran >= RECYCLE_MIN_RUN_S:
                # The child ended itself to give its memory back. Not a failure.
                log.info(f"Batch process recycled itself after {ran / 60:.0f} min; starting a fresh one")
                failures = 0
                now = time.monotonic()
                recycles = [t for t in recycles if now - t < RECYCLE_CHURN_WINDOW_S] + [now]
                if len(recycles) >= RECYCLE_CHURN_COUNT:
                    recycles = []
                    try:
                        from discord_bot.ops_alert import ops_alert
                        await ops_alert(
                            f"Batch process restarted itself {RECYCLE_CHURN_COUNT} times in "
                            f"{RECYCLE_CHURN_WINDOW_S // 3600} hours to give memory back. "
                            "BATCH_RECYCLE_RSS_MB is probably below what it needs between jobs. "
                            "Jobs are running.", dedupe_key="batch-recycle-churn")
                    except Exception as e:
                        log.warning(f"recycle churn alert failed: {e}")
                continue
            failures = 1 if ran >= STABLE_AFTER_S else failures + 1
            delay = restart_delay(failures)
            text = (f"Batch process (PDF ingestion, Claude lane, pulse context, "
                    f"profiles) exited with code {rc} after {ran:.0f} s; "
                    f"restarting in {delay} s. /ask is unaffected.")
            log.error(text)
            try:
                from discord_bot.ops_alert import ops_alert
                await ops_alert(text, dedupe_key="batch-process-exit")
            except Exception as e:
                log.warning(f"batch exit alert failed: {e}")
            await asyncio.sleep(delay)

    def terminate_now(self) -> None:
        """Signal the child at once. Called from the SIGTERM handler, where a
        task scheduled for `stop()` may be cancelled before it runs."""
        self._stopping = True
        p = self.proc
        if p is not None and p.returncode is None:
            try:
                p.terminate()
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        self.terminate_now()
        p = self.proc
        if p is not None and p.returncode is None:
            try:
                await asyncio.wait_for(p.wait(), STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                p.kill()
        if self._task is not None:
            self._task.cancel()
