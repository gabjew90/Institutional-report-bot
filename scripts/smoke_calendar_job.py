"""Smoke: the daily calendar scheduler job (spec §7). send mocked;
asserts idempotency, both-feeds-down fallback, and cron registration."""

import asyncio
import sys
from unittest.mock import patch, AsyncMock

import scheduler.jobs as jobs
from report.calendar_data import CalendarDay


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _run(day, already_posted=False, bot=object()):
    events = []
    sent = {"png": 0, "plain": 0}

    async def _fake_send_file(bot_, png, filename, caption=""):
        sent["png"] += 1
        return 2

    async def _fake_send_plain(ch, msgs, **kw):
        sent["plain"] += 1
        return True

    class _FakeBot:
        def get_channel(self, cid):
            return object()

    import db
    import discord_bot.sender as sender
    with patch.object(db, "calendar_already_posted",
                      lambda d: already_posted), \
         patch.object(db, "record_pipeline_event",
                      lambda *a, **kw: events.append((a, kw))), \
         patch("report.calendar_data.build_calendar_day",
               lambda d: day), \
         patch("report.calendar_render.render_calendar_png",
               lambda d: b"\x89PNG fakebytes"), \
         patch.object(sender, "send_file_to_channels", _fake_send_file), \
         patch.object(sender, "send_plain_messages", _fake_send_plain), \
         patch.object(jobs.settings, "discord_channel_id", "123,456"):
        asyncio.run(jobs._daily_calendar_job(
            bot=_FakeBot() if bot is not None else None))
    return events, sent


def test_posts_once_and_records():
    day = CalendarDay(date_iso="2026-08-20",
                      weekday_label="THURSDAY 8/20", is_holiday=False)
    events, sent = _run(day)
    assert sent["png"] == 1, sent
    assert any("calendar_posted" in str(e) for e in events), events
    _ok("normal night: renders, posts once, records calendar_posted")


def test_idempotent_reboot():
    day = CalendarDay(date_iso="2026-08-20",
                      weekday_label="THURSDAY 8/20", is_holiday=False)
    events, sent = _run(day, already_posted=True)
    assert sent["png"] == 0 and not events, (sent, events)
    _ok("already-posted date: no re-render, no re-post (idempotent)")


def test_both_feeds_down_plain_fallback():
    day = CalendarDay(date_iso="2026-08-20",
                      weekday_label="THURSDAY 8/20", is_holiday=False,
                      earnings_available=False, econ_available=False)
    events, sent = _run(day)
    assert sent["png"] == 0, "must NOT post an empty sheet"
    assert sent["plain"] == 2, f"plain fallback to both channels: {sent}"
    assert any("calendar_watchdog" in str(e) for e in events), events
    _ok("both feeds down: plain fallback, watchdog event, no image")


def test_holiday_still_posts_closed_card():
    day = CalendarDay(date_iso="2026-09-07",
                      weekday_label="MONDAY 9/7", is_holiday="Labor Day")
    events, sent = _run(day)
    assert sent["png"] == 1, "closed card must still post"
    _ok("holiday: closed card posts (never a silent skip)")


def test_cron_registered_utc_monfri():
    import inspect
    src = inspect.getsource(jobs.setup_scheduler)
    assert 'id="daily_calendar"' in src, "job not registered"
    assert 'day_of_week="mon-fri"' in src
    assert "timezone=_utc" in src, \
        "calendar cron must be UTC, not the scheduler's local tz"
    assert "misfire_grace_time=3600" in src.split('id="daily_calendar"')[1][:400]
    _ok("cron: mon-fri 00:00 UTC, misfire grace 3600")


if __name__ == "__main__":
    print("=== calendar job smoke ===")
    test_posts_once_and_records()
    test_idempotent_reboot()
    test_both_feeds_down_plain_fallback()
    test_holiday_still_posts_closed_card()
    test_cron_registered_utc_monfri()
    print("\nALL CALENDAR JOB SMOKE TESTS PASS")
