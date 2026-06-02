"""Static smoke test for the cadence tweaks.

Validates the scheduler configuration in scheduler/jobs.py uses the
intended periodicity for the three jobs whose freshness affects the
trader-log overhaul's user experience:

  1. user_profile_refresh — was daily 15:00 ET, now every 6h
     so new analyst_trades rows reflect in trader_score within ~6h
     instead of waiting up to 24h.
  2. analyst_expire_sweep — was daily 4:00 ET, now hourly so options
     expiring at 4pm market close get marked within an hour, not 12h.
  3. chat catchup periodic backstop (new) — chat_ingestion.watcher
     only catches up on gateway events; bot running steady all day
     means no catchup runs. Add an every-4h backstop.

Why static checks: the alternative (running APScheduler in-process and
introspecting jobs) is much heavier for what's effectively a "is the
schedule string right" verification.
"""

import inspect
import re
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _scheduler_source() -> str:
    """Return the source of scheduler/jobs.py:setup_scheduler so the
    smoke can grep its add_job blocks."""
    from scheduler import jobs
    return inspect.getsource(jobs.setup_scheduler)


def test_profile_refresh_runs_every_6_hours():
    """Block ID 'user_profile_refresh' should have an interval / cron
    trigger that fires multiple times per day, not once daily."""
    src = _scheduler_source()
    idx = src.find('id="user_profile_refresh"')
    assert idx > 0, "couldn't find user_profile_refresh add_job block"
    # Grab the 800 chars before id= (the trigger lives above the id= line)
    block = src[max(0, idx - 800):idx + 200]
    # Look for either an IntervalTrigger with hours= OR a CronTrigger
    # with hour="X,Y,Z" listing multiple values.
    has_interval = "IntervalTrigger(hours=6)" in block
    has_multi_cron = bool(re.search(r'CronTrigger\([^)]*hour\s*=\s*"[\d,]+"', block))
    assert has_interval or has_multi_cron, (
        "user_profile_refresh trigger should be IntervalTrigger(hours=6) OR "
        "CronTrigger with explicit hour='3,9,15,21' (4x/day). "
        f"Got block: {block[-300:]!r}"
    )
    _ok("user_profile_refresh runs every 6h (4x per day)")


def test_expire_sweep_runs_hourly():
    """analyst_expire_sweep was CronTrigger(hour=4, minute=0). Should
    now be IntervalTrigger(hours=1) or CronTrigger(minute=0)."""
    src = _scheduler_source()
    idx = src.find('id="analyst_expire_sweep"')
    assert idx > 0, "couldn't find analyst_expire_sweep add_job block"
    block = src[max(0, idx - 800):idx + 200]
    has_interval = "IntervalTrigger(hours=1)" in block
    has_hourly_cron = bool(re.search(
        r'CronTrigger\(\s*minute\s*=\s*0\s*[,)]', block
    ))
    assert has_interval or has_hourly_cron, (
        "analyst_expire_sweep trigger should be IntervalTrigger(hours=1) "
        "OR CronTrigger(minute=0). "
        f"Got block: {block[-300:]!r}"
    )
    _ok("analyst_expire_sweep runs hourly")


def test_chat_catchup_periodic_backstop_exists():
    """New job: periodic chat catchup so it doesn't only fire on
    gateway events. id='chat_catchup_periodic' with interval ~4h."""
    src = _scheduler_source()
    has_id = (
        'id="chat_catchup_periodic"' in src
        or "id='chat_catchup_periodic'" in src
    )
    assert has_id, (
        "scheduler should register a 'chat_catchup_periodic' job as a "
        "backstop for the on_ready / on_resumed catchup pattern"
    )
    _ok("chat_catchup_periodic backstop job is registered")


if __name__ == "__main__":
    print("=== cadence tweaks static smoke ===")
    test_profile_refresh_runs_every_6_hours()
    test_expire_sweep_runs_hourly()
    test_chat_catchup_periodic_backstop_exists()
    print("\nALL CADENCE-TWEAKS SMOKE TESTS PASS")
