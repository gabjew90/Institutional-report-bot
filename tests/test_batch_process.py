"""The bot/batch split (scheduler/batch_proc.py, 2026-10-27)."""
import asyncio
import sys

import pytest

from scheduler import batch_proc as BP
from scheduler.jobs import setup_scheduler


def _ids(role):
    return {j.id for j in setup_scheduler(None, role=role).get_jobs()}


def test_every_job_runs_in_exactly_one_process():
    everything, bot, batch = _ids("all"), _ids("bot"), _ids("batch")
    assert bot | batch == everything
    assert bot & batch == set(BP.SHARED_JOB_IDS) & everything


def test_the_heavy_jobs_are_in_the_batch_process():
    batch = _ids("batch")
    for job_id in ("poll_dropbox", "process_queue", "user_profile_refresh"):
        assert job_id in batch
    bot = _ids("bot")
    for job_id in ("poll_dropbox", "process_queue", "user_profile_refresh"):
        assert job_id not in bot
    assert "research_feed_watchdog" in bot and "malloc_trim" in bot


def test_keeps_job():
    assert BP.keeps_job("process_queue", "batch")
    assert not BP.keeps_job("process_queue", "bot")
    assert BP.keeps_job("daily_calendar", "bot") and not BP.keeps_job("daily_calendar", "batch")
    assert BP.keeps_job("malloc_trim", "bot") and BP.keeps_job("malloc_trim", "batch")
    assert BP.keeps_job("anything", "all")


def test_restart_backoff_grows_and_caps():
    assert [BP.restart_delay(n) for n in (1, 2, 3, 6, 9)] == [10, 30, 60, 600, 600]


def test_supervisor_restarts_a_dead_child_and_stops_a_live_one(monkeypatch):
    alerts = []

    async def fake_alert(text, dedupe_key=""):
        alerts.append(text)
    import discord_bot.ops_alert as oa
    monkeypatch.setattr(oa, "ops_alert", fake_alert)
    monkeypatch.setattr(BP, "RESTART_DELAYS_S", (0,))

    async def run():
        # First child exits at once, the restart sleeps until stopped.
        sup = BP.BatchSupervisor([sys.executable, "-c",
                                  "import sys,time; sys.exit(3)"])
        sup.start()
        for _ in range(200):
            await asyncio.sleep(0.05)
            if alerts:
                break
        sup.argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        for _ in range(200):
            await asyncio.sleep(0.05)
            if sup.running and sup.proc.pid:
                break
        assert sup.running
        await sup.stop()
        assert not sup.running
    asyncio.run(run())
    assert alerts and "code 3" in alerts[0]
