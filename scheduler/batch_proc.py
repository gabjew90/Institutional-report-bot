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
})
# Per-process housekeeping both processes run.
SHARED_JOB_IDS = frozenset({"malloc_trim"})

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
