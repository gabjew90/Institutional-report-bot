"""A profile refresh whose slot passed while the worker was restarting is
run once on start (2026-10-30 audit M13: the 2026-10-28 01:00 UTC refresh
was cut off by the 01:50 deploy and nothing ran that night)."""
from datetime import datetime, timezone

from pytz import timezone as pytz_tz

from scheduler.jobs import missed_profile_slot

NY = pytz_tz("America/New_York")


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_the_october_28_night_is_caught_up():
    last = datetime(2026, 10, 27, 1, 0, 49)                       # 10-26 21:00 ET run
    slot = missed_profile_slot("21", NY, last, _utc(2026, 10, 28, 1, 51))
    assert slot is not None and slot.astimezone(timezone.utc) == _utc(2026, 10, 28, 1, 0)


def test_a_completed_slot_is_not_run_again():
    last = datetime(2026, 10, 31, 1, 2, 34)
    assert missed_profile_slot("21", NY, last, _utc(2026, 10, 31, 15, 0)) is None
    # just before tonight's slot: yesterday's completed, tonight's is the cron's
    assert missed_profile_slot("21", NY, last, _utc(2026, 11, 1, 0, 59)) is None


def test_a_start_just_after_the_slot_runs_it():
    """A cron job added after its time waits a day; the catch-up covers it."""
    last = datetime(2026, 10, 31, 1, 2, 34)
    assert missed_profile_slot("21", NY, last, _utc(2026, 11, 1, 1, 0, 20)) is not None


def test_several_hours_and_no_history():
    last = datetime(2026, 10, 31, 13, 0, 30)                      # the 09:00 ET run
    slot = missed_profile_slot("3,9,15,21", NY, last, _utc(2026, 10, 31, 20, 0))
    assert slot.astimezone(NY).hour == 15
    assert missed_profile_slot("21", NY, None, _utc(2026, 10, 31, 20, 0)) is None


def test_the_slot_is_local_time_across_the_dst_change():
    # 2026-11-01 is the fall-back day: 21:00 ET is 02:00 UTC from then on
    last = datetime(2026, 11, 1, 1, 1, 5)
    slot = missed_profile_slot("21", NY, last, _utc(2026, 11, 2, 2, 30))
    assert slot.astimezone(timezone.utc) == _utc(2026, 11, 2, 2, 0)


def test_a_slot_is_retried_once_and_only_where_the_job_runs(monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import db
    from scheduler import jobs
    events = {"profile_refresh": [{"created_at": "2026-10-27 01:00:49"}],
              "profile_refresh_catchup": []}
    monkeypatch.setattr(db, "get_recent_pipeline_events",
                        lambda kind, limit=20: events[kind][:limit])

    class _Now(datetime):
        @classmethod
        def now(cls, tz=None):
            return _utc(2026, 10, 28, 1, 51)
    monkeypatch.setattr("datetime.datetime", _Now)
    s = AsyncIOScheduler(timezone=NY)
    jobs._schedule_missed_profile_refresh(s, None, NY)
    assert s.get_job("user_profile_refresh_catchup") is not None
    events["profile_refresh_catchup"] = [{"created_at": "2026-10-28 01:53:00"}]
    s = AsyncIOScheduler(timezone=NY)
    jobs._schedule_missed_profile_refresh(s, None, NY)
    assert s.get_job("user_profile_refresh_catchup") is None, "already retried once"
