"""Smoke: a market-open weekday with no pulse gets announced.

2026-08-11. The Claude.ai routine that writes the pulse ran out of model
credits mid-run. It wrote one progress stamp at STEP 2 and stopped. No
pulse, no error artifact, no skip marker, no alert. The miss was found
only because a human noticed and dug through the pulse-data branch.

The routine's holiday path self-documents by committing a skip marker,
which works only while the agent is alive to commit it. Credit
exhaustion, a crash, or a bootstrap failure kill it first. So the check
runs on the always-on worker and tests for the ABSENCE OF A RESULT rather
than any particular cause.

scripts/routine_watcher.py does not cover this — it is a manual tool
invoked under Monitor to watch a fire live, and nothing schedules it.
"""

import asyncio
import inspect
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, body):
        self.sent.append(body)


class FakeBot:
    def __init__(self):
        self.channel = FakeChannel()

    def get_channel(self, _cid):
        return self.channel


def _fresh_db():
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    dbmod._conn = conn
    return dbmod, conn


def _insert_daily(conn, date, report_type="daily"):
    conn.execute(
        "INSERT INTO daily_reports (report_date, report_type, report_json, "
        "report_markdown) VALUES (?,?,?,?)",
        (date, report_type, "{}", "# pulse"))
    conn.commit()


def test_delivered_pulse_is_detected():
    dbmod, conn = _fresh_db()
    _insert_daily(conn, "2026-08-12")
    if not dbmod.daily_pulse_delivered_on("2026-08-12"):
        _fail("a delivered daily pulse was not detected")
    if dbmod.daily_pulse_delivered_on("2026-08-11"):
        _fail("a date with no pulse reported as delivered")
    _ok("daily_pulse_delivered_on distinguishes delivered from missing")


def test_manual_pulse_does_not_count():
    """A manual /pulse does not satisfy the scheduled cadence."""
    dbmod, conn = _fresh_db()
    _insert_daily(conn, "2026-08-12", report_type="manual")
    if dbmod.daily_pulse_delivered_on("2026-08-12"):
        _fail("a manual pulse was counted as the scheduled pulse")
    _ok("manual pulses do not satisfy the watchdog")


def _run_watchdog(bot):
    import scheduler.jobs as jobs
    asyncio.run(jobs._missing_pulse_watchdog_job(bot=bot))


def test_missing_pulse_posts_once():
    from datetime import datetime
    from pytz import timezone as _tz
    from config import settings

    _dbmod, conn = _fresh_db()
    today = datetime.now(_tz(settings.timezone))
    if today.weekday() >= 5:
        _ok("skipped (weekend today) — weekday path covered by the "
            "holiday/weekend guard test")
        return
    import world_context
    if world_context.is_us_market_holiday(today.strftime("%Y-%m-%d")):
        _ok("skipped (holiday today)")
        return

    bot = FakeBot()
    _run_watchdog(bot)
    if len(bot.channel.sent) != 1:
        _fail(f"expected exactly 1 alert, got {len(bot.channel.sent)}")
    if "No market pulse today" not in bot.channel.sent[0]:
        _fail(f"unexpected alert body: {bot.channel.sent[0][:120]}")

    # Idempotency: a second run must not repost.
    _run_watchdog(bot)
    if len(bot.channel.sent) != 1:
        _fail("watchdog reposted on a second run — the idempotency guard "
              "does not hold across worker restarts")
    _ok("missing pulse alerts exactly once per day")


def test_delivered_pulse_stays_quiet():
    from datetime import datetime
    from pytz import timezone as _tz
    from config import settings

    _dbmod, conn = _fresh_db()
    today = datetime.now(_tz(settings.timezone)).strftime("%Y-%m-%d")
    _insert_daily(conn, today)
    bot = FakeBot()
    _run_watchdog(bot)
    if bot.channel.sent:
        _fail("watchdog alerted despite a delivered pulse")
    _ok("no alert when the pulse was delivered")


def test_holiday_and_weekend_are_not_failures():
    import scheduler.jobs as jobs
    src = inspect.getsource(jobs._missing_pulse_watchdog_job)
    if "is_us_market_holiday" not in src:
        _fail("watchdog does not check the market-holiday calendar — it "
              "would cry wolf on every NYSE closure")
    if "weekday() >= 5" not in src:
        _fail("watchdog has no weekend guard")
    _ok("holiday and weekend misses are treated as expected, not failures")


def test_watchdog_is_registered_on_the_bridge_path():
    import scheduler.jobs as jobs
    src = inspect.getsource(jobs.setup_scheduler)
    if "_missing_pulse_watchdog_job" not in src:
        _fail("watchdog is never scheduled — the whole point is that it "
              "runs without a human invoking it")
    if "missing_pulse_watchdog" not in src:
        _fail("watchdog job id missing")
    if "day_of_week=\"mon-fri\"" not in src:
        _fail("watchdog is not restricted to weekdays")
    _ok("watchdog is registered as a weekday cron job")


if __name__ == "__main__":
    test_delivered_pulse_is_detected()
    test_manual_pulse_does_not_count()
    test_missing_pulse_posts_once()
    test_delivered_pulse_stays_quiet()
    test_holiday_and_weekend_are_not_failures()
    test_watchdog_is_registered_on_the_bridge_path()
    print("\nAll missing-pulse watchdog smoke tests passed.")
