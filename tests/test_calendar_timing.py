"""Calendar timing (2026-09-01): post at 4:20 PM ET for the next session,
refresh in place at 7:30 AM ET when the lineup changed."""
import asyncio
import sys

import db
from report.calendar_data import CalendarDay, EarnRow, EconRow, lineup_signature
from world_context import next_trading_day


# ------------------------------------------------------ next_trading_day

def test_friday_rolls_to_monday():
    assert next_trading_day("2026-09-04") == "2026-09-08"  # Mon 9/7 is Labor Day


def test_weekday_rolls_to_next_weekday():
    assert next_trading_day("2026-09-01") == "2026-09-02"


def test_holiday_is_skipped():
    assert next_trading_day("2026-09-06") == "2026-09-08"  # Sun -> skip Labor Day


def test_uncovered_year_never_skips_a_weekday():
    """A year the holiday list does not cover must roll Fri -> Mon and
    nothing else: a stale list must never cause a skipped sheet."""
    from datetime import date, timedelta
    d = date(2031, 6, 2)
    while d.weekday() != 4:
        d += timedelta(days=1)
    assert next_trading_day(d.isoformat()) == (d + timedelta(days=3)).isoformat()


# ------------------------------------------------------ lineup_signature

def _day(moves=(("AVGO", 8.4),), econ=(("8:30", "CPI"),)):
    d = CalendarDay(date_iso="2026-09-02", weekday_label="WED 9/2", is_holiday=False)
    d.amc = [EarnRow(symbol=s, name=s, cap_musd=1.0, implied_move=m, session_confirmed=True)
             for s, m in moves]
    d.econ = [EconRow(time_et=t, event=e, impact="high") for t, e in econ]
    return d


def test_signature_is_stable_for_the_same_lineup():
    assert lineup_signature(_day()) == lineup_signature(_day())


def test_signature_changes_when_a_move_prices_or_a_name_appears():
    base = lineup_signature(_day())
    assert lineup_signature(_day(moves=(("AVGO", None),))) != base
    assert lineup_signature(_day(moves=(("AVGO", 8.4), ("MDB", 12.0)))) != base
    assert lineup_signature(_day(econ=())) != base


# ------------------------------------------------------ refresh job

def _run_refresh(posts, day, edits):
    import scheduler.jobs as J
    from datetime import datetime
    from report import calendar_data as cd
    from report import calendar_render as cr
    from discord_bot import sender
    o = (db.get_calendar_posts, db.mark_calendar_refreshed, db.record_pipeline_event,
         cd.build_calendar_day, cr.render_calendar_png, sender.edit_file_message)
    marked = []

    async def fake_edit(bot, cid, mid, png, fn):
        edits.append((cid, mid)); return True
    try:
        db.get_calendar_posts = lambda d: posts
        db.mark_calendar_refreshed = lambda d, h: marked.append(h)
        db.record_pipeline_event = lambda *a, **k: None
        cd.build_calendar_day = lambda d: day
        cr.render_calendar_png = lambda d: b"png"
        sender.edit_file_message = fake_edit
        asyncio.run(J._calendar_refresh_job(bot=object()))
    finally:
        (db.get_calendar_posts, db.mark_calendar_refreshed, db.record_pipeline_event,
         cd.build_calendar_day, cr.render_calendar_png, sender.edit_file_message) = o
    return marked


def test_refresh_edits_in_place_when_lineup_changed():
    day = _day()
    edits = []
    marked = _run_refresh([{"channel_id": 1, "message_id": 10, "lineup_hash": "stale"}], day, edits)
    assert edits == [(1, 10)]
    assert marked == [lineup_signature(day)]


def test_refresh_is_silent_when_lineup_unchanged():
    day = _day()
    edits = []
    marked = _run_refresh([{"channel_id": 1, "message_id": 10,
                            "lineup_hash": lineup_signature(day)}], day, edits)
    assert edits == [] and marked == []


def test_refresh_with_no_posted_sheet_does_nothing():
    edits = []
    assert _run_refresh([], _day(), edits) == [] and edits == []


# ------------------------------------------------------ post registry

def test_posts_round_trip_and_idempotency_check():
    db.record_calendar_posts("2099-01-02", [(1, 10), (2, 20)], "abc")
    got = db.get_calendar_posts("2099-01-02")
    assert {(r["channel_id"], r["message_id"]) for r in got} == {(1, 10), (2, 20)}
    assert db.calendar_already_posted("2099-01-02") is True
    db.mark_calendar_refreshed("2099-01-02", "def")
    assert all(r["lineup_hash"] == "def" and r["refreshed_at"] for r in db.get_calendar_posts("2099-01-02"))


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
