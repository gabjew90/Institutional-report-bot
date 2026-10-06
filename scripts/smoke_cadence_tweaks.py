"""Static smoke test for the cadence tweaks.

Validates the scheduler configuration in scheduler/jobs.py uses the
intended periodicity for the three jobs whose freshness affects the
trader-log overhaul's user experience:

  1. user_profile_refresh — every 6h from 2026-06-02, back to once
     daily (21:00 ET, settings.profile_refresh_hours) on 2026-10-06
     for cost.
  2. analyst_expire_sweep — was daily 4:00 AM ET, now daily 4:00 PM ET
     (16:00) — runs right after market close so 0DTE options expiring
     that day get marked the same day instead of 12h later.
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
    """Owner call 2026-10-06 (cost): the refresh runs on the hours in
    settings.profile_refresh_hours, once a day at 21:00 ET by default
    (after the close, so trader_score carries the day's trades). It ran
    every 6h from 2026-06-02 to 2026-10-06. The name is kept so the
    manifest entry and the runner keep pointing here."""
    src = _scheduler_source()
    idx = src.find('id="user_profile_refresh"')
    assert idx > 0, "couldn't find user_profile_refresh add_job block"
    block = src[max(0, idx - 800):idx + 200]
    assert "hour=settings.profile_refresh_hours" in block, (
        "user_profile_refresh must read its hours from settings.profile_refresh_hours. "
        f"Got block: {block[-300:]!r}")
    from config import Settings
    assert Settings.model_fields["profile_refresh_hours"].default == "21"
    _ok("user_profile_refresh runs at settings.profile_refresh_hours (daily 21:00 ET)")


def test_expire_sweep_runs_at_market_close():
    """analyst_expire_sweep should run once daily at 16:00 ET (market
    close) — earlier 04:00 AM had options sitting 'open' for 12h before
    being marked expired. Once-daily at close catches all of today's
    0DTE expirations the same day."""
    src = _scheduler_source()
    idx = src.find('id="analyst_expire_sweep"')
    assert idx > 0, "couldn't find analyst_expire_sweep add_job block"
    block = src[max(0, idx - 800):idx + 200]
    # CronTrigger(hour=16, minute=0) — 4 PM ET
    has_4pm_cron = bool(re.search(
        r'CronTrigger\([^)]*hour\s*=\s*16[^)]*minute\s*=\s*0', block
    ))
    assert has_4pm_cron, (
        "analyst_expire_sweep trigger should be "
        "CronTrigger(hour=16, minute=0) — daily at 4 PM ET market close. "
        f"Got block: {block[-300:]!r}"
    )
    _ok("analyst_expire_sweep runs daily at 16:00 ET (market close)")


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
    test_expire_sweep_runs_at_market_close()
    test_chat_catchup_periodic_backstop_exists()
    print("\nALL CADENCE-TWEAKS SMOKE TESTS PASS")
