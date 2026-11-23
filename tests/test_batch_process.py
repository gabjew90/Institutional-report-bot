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


# ── recycling (2026-11-23) ──────────────────────────────────────────────────

def _due(**over):
    base = dict(rss_mb=620.0, limit_mb=400, uptime_s=BP.RECYCLE_MIN_UPTIME_S + 1,
                running_jobs=0, next_cron_s=3600.0)
    base.update(over)
    return BP.recycle_due(**base)


def test_recycle_only_when_bloated_idle_settled_and_clear_of_a_cron_job():
    assert _due()
    assert _due(next_cron_s=None)                       # no cron job at all
    assert not _due(rss_mb=320.0)                       # a normal working day
    assert not _due(rss_mb=400.0)                       # at the limit is not over it
    assert not _due(running_jobs=1)                     # the profile refresh is still going
    assert not _due(uptime_s=BP.RECYCLE_MIN_UPTIME_S - 1)   # no loop of restarts
    assert not _due(next_cron_s=BP.RECYCLE_CRON_CLEAR_S)    # 21:00 is a minute away
    assert not _due(limit_mb=0)                         # switched off
    assert not _due(rss_mb=None)                        # no /proc (Windows)


def test_the_child_cannot_recycle_sooner_than_the_supervisor_accepts():
    assert BP.RECYCLE_MIN_UPTIME_S >= BP.RECYCLE_MIN_RUN_S


def test_the_job_counter_follows_a_real_scheduler():
    from datetime import datetime, timedelta, timezone
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    seen = []

    async def run():
        sched = AsyncIOScheduler(timezone="UTC")
        jobs = BP.JobCounter().attach(sched)
        gate = asyncio.Event()

        async def slow():
            await gate.wait()

        async def boom():
            raise RuntimeError("x")
        soon = datetime.now(timezone.utc) + timedelta(milliseconds=50)
        sched.add_job(slow, "date", run_date=soon, id="slow")
        sched.add_job(boom, "date", run_date=soon, id="boom")
        sched.start()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if jobs.running.get("slow") and "boom" not in jobs.running:
                break
        seen.append(dict(jobs.running))
        gate.set()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if jobs.count == 0:
                break
        seen.append(jobs.count)
        sched.shutdown(wait=False)
    import logging
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(run())
    finally:
        logging.disable(logging.NOTSET)
    # Running while it runs, gone when it ends, and a job that raised is gone too.
    assert seen == [{"slow": 1}, 0]


def test_only_cron_jobs_count_toward_the_clear_window():
    from datetime import datetime, timezone
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    async def noop():
        pass

    async def run():
        sched = AsyncIOScheduler(timezone="UTC")
        sched.add_job(noop, IntervalTrigger(seconds=5), id="poll")
        sched.start(paused=True)
        assert BP.next_cron_seconds(sched) is None
        sched.add_job(noop, CronTrigger(hour=21, minute=0, timezone="UTC"), id="refresh")
        at = datetime(2026, 11, 23, 20, 59, 30, tzinfo=timezone.utc)
        nxt = sched.get_job("refresh").next_run_time
        secs = BP.next_cron_seconds(sched, now=at)
        sched.shutdown(wait=False)
        return nxt, secs
    nxt, secs = asyncio.run(run())
    assert secs == (nxt - datetime(2026, 11, 23, 20, 59, 30, tzinfo=timezone.utc)).total_seconds()


def _supervise(monkeypatch, min_run_s):
    alerts = []

    async def fake_alert(text, dedupe_key=""):
        alerts.append(text)
    import discord_bot.ops_alert as oa
    monkeypatch.setattr(oa, "ops_alert", fake_alert)
    monkeypatch.setattr(BP, "RESTART_DELAYS_S", (0,))
    monkeypatch.setattr(BP, "RECYCLE_MIN_RUN_S", min_run_s)
    starts = []

    async def run():
        sup = BP.BatchSupervisor([sys.executable, "-c", f"import sys; sys.exit({BP.RECYCLE_EXIT_CODE})"])
        real = asyncio.create_subprocess_exec

        async def counting(*a, **k):
            starts.append(1)
            if len(starts) >= 3:
                sup.argv = [sys.executable, "-c", "import time; time.sleep(60)"]
                a = tuple(sup.argv)
            return await real(*a, **k)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", counting)
        sup.start()
        for _ in range(400):
            await asyncio.sleep(0.05)
            if len(starts) >= 3 and sup.running:
                break
        await sup.stop()
    asyncio.run(run())
    return alerts, starts


def test_a_recycle_exit_restarts_at_once_and_pages_nobody(monkeypatch):
    alerts, starts = _supervise(monkeypatch, min_run_s=0)
    assert len(starts) >= 3 and alerts == []


def test_recycles_in_quick_succession_page_once_and_keep_restarting(monkeypatch):
    monkeypatch.setattr(BP, "RECYCLE_CHURN_COUNT", 2)
    alerts, starts = _supervise(monkeypatch, min_run_s=0)
    assert len(starts) >= 3
    assert len(alerts) == 1 and "BATCH_RECYCLE_RSS_MB" in alerts[0]


def test_a_recycle_exit_right_after_start_is_treated_as_a_failure(monkeypatch):
    alerts, starts = _supervise(monkeypatch, min_run_s=600)
    assert alerts and f"code {BP.RECYCLE_EXIT_CODE}" in alerts[0]


def test_the_batch_process_checks_between_jobs_and_passes_its_exit_code_on():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scheduler" / "batch_main.py").read_text(encoding="utf-8")
    assert "sys.exit(asyncio.run(main()))" in src
    assert "JobCounter().attach(scheduler)" in src
    assert src.index("JobCounter().attach(scheduler)") < src.index("scheduler.start()")
    # The limit is a setting, and the default sits between a working day and
    # what the nightly refresh leaves behind.
    from config import Settings
    assert 320 < Settings.model_fields["batch_recycle_rss_mb"].default < 600


def test_the_recycle_check_trims_before_it_decides(monkeypatch):
    import memtrim
    from scheduler import batch_main as BM
    import logging
    calls = []
    readings = iter([650.0, 380.0])          # over the limit, then under it after the trim
    monkeypatch.setattr(memtrim, "rss_mb", lambda: next(readings))
    monkeypatch.setattr(memtrim, "trim", lambda: calls.append("trim") or 1)
    monkeypatch.setattr(BM.settings, "batch_recycle_rss_mb", 400, raising=False)
    monkeypatch.setattr(BP, "next_cron_seconds", lambda s, now=None: None)
    long_ago = BM.time.monotonic() - 10_000.0
    jobs = BP.JobCounter()
    assert BM._recycle_now(object(), jobs, long_ago, logging.getLogger("t")) is False
    assert calls == ["trim"]
    readings = iter([650.0, 640.0])          # still over it after the trim
    monkeypatch.setattr(memtrim, "rss_mb", lambda: next(readings))
    assert BM._recycle_now(object(), jobs, long_ago, logging.getLogger("t")) is True
    jobs.running["process_queue"] = 1        # a job is running: nothing is trimmed
    calls.clear()
    monkeypatch.setattr(memtrim, "rss_mb", lambda: 650.0)
    assert BM._recycle_now(object(), jobs, long_ago, logging.getLogger("t")) is False
    assert calls == []
    jobs.running.clear()                     # and a process that just started is left alone
    assert BM._recycle_now(object(), jobs, BM.time.monotonic(), logging.getLogger("t")) is False
