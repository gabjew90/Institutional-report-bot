"""Smoke test: channel reminder system (2026-06-16).

A Claude-edited calendar (reminders/calendar.json) + a daily 3:45 PM ET
cron that posts due reminders to the stonks-yapping channel, dedup-
guarded, with a read-only /reminders command.

Covers:
  - calendar load/validate: defaults, coercion, malformed-skip, missing
    file, non-list file
  - due-date math: lead_days fire on exactly (event_date - lead)
  - upcoming(): future-only, sorted
  - wording: lead_phrase / reminder_title
  - dedup DB roundtrip: reminder_already_sent / mark_reminder_sent
  - job embed build (title + note)
  - wiring: scheduler registers the cron; /reminders command present
"""

import inspect
import json
import os
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _write_cal(entries) -> str:
    f = tempfile.NamedTemporaryFile(
        suffix=".json", delete=False, mode="w", encoding="utf-8")
    json.dump(entries, f)
    f.close()
    return f.name


# === load / validate ===

def test_load_defaults_and_coercion():
    from reminders import calendar as cal
    path = _write_cal([
        {"date": "2026-06-25", "event": "SpaceX unlock", "lead_days": [7, 0]},
        {"date": "2026-07-01", "event": "No lead given"},           # default [1]
        {"date": "2026-07-02", "event": "Messy leads",
         "lead_days": [3, 3, -1, "2", "x"]},                         # -> [2,3]
    ])
    try:
        entries = cal.load_calendar(path)
    finally:
        os.unlink(path)
    by_event = {e["event"]: e for e in entries}
    assert by_event["SpaceX unlock"]["lead_days"] == [0, 7]
    assert by_event["No lead given"]["lead_days"] == [1], "default lead is [1]"
    assert by_event["Messy leads"]["lead_days"] == [2, 3], (
        "leads coerced: non-negative ints, deduped, sorted"
    )
    # id auto-synthesized from event+date
    assert by_event["SpaceX unlock"]["id"] == "spacex-unlock-2026-06-25"
    _ok("load: default lead [1], lead coercion, id slug synthesis")


def test_load_skips_malformed_keeps_rest():
    from reminders import calendar as cal
    path = _write_cal([
        {"date": "2026-06-25", "event": "Good one"},
        {"date": "not-a-date", "event": "Bad date"},   # skip
        {"event": "No date"},                           # skip
        {"date": "2026-06-26"},                         # no event, skip
        "totally not a dict",                           # skip
        {"date": "2026-06-27", "event": "Also good"},
    ])
    try:
        entries = cal.load_calendar(path)
    finally:
        os.unlink(path)
    events = sorted(e["event"] for e in entries)
    assert events == ["Also good", "Good one"], events
    _ok("load: malformed entries skipped, valid ones kept")


def test_load_missing_and_nonlist():
    from reminders import calendar as cal
    assert cal.load_calendar("/no/such/file.json") == []
    path = _write_cal({"not": "a list"})
    try:
        assert cal.load_calendar(path) == []
    finally:
        os.unlink(path)
    _ok("load: missing file -> [], non-list file -> [] (never raises)")


# === due-date math ===

def test_due_fires_on_exact_lead_dates():
    from reminders import calendar as cal
    entries = cal.load_calendar.__wrapped__ if hasattr(
        cal.load_calendar, "__wrapped__") else None
    # Build entries directly via _validate_entry for determinism
    e = cal._validate_entry(
        {"date": "2026-06-25", "event": "Unlock", "lead_days": [7, 1, 0]})
    pool = [e]
    fires = {
        d.isoformat(): [lead for _, lead in cal.due_reminders(pool, d)]
        for d in (date(2026, 6, 18), date(2026, 6, 24), date(2026, 6, 25),
                  date(2026, 6, 20), date(2026, 6, 26))
    }
    assert fires["2026-06-18"] == [7], fires    # 7 days before
    assert fires["2026-06-24"] == [1], fires    # day before
    assert fires["2026-06-25"] == [0], fires    # day of
    assert fires["2026-06-20"] == [], fires     # nothing in between
    assert fires["2026-06-26"] == [], fires     # nothing after
    _ok("due: fires on exactly (event_date - lead), 0 = day-of, nothing else")


def test_upcoming_future_only_sorted():
    from reminders import calendar as cal
    pool = [
        cal._validate_entry({"date": "2026-06-20", "event": "past"}),
        cal._validate_entry({"date": "2026-06-30", "event": "later"}),
        cal._validate_entry({"date": "2026-06-25", "event": "soon"}),
    ]
    up = cal.upcoming(pool, date(2026, 6, 25))
    names = [e["event"] for e in up]
    assert names == ["soon", "later"], names  # past dropped, sorted
    _ok("upcoming: today+future only, sorted by date")


def test_wording():
    from reminders import calendar as cal
    assert cal.lead_phrase(0) == "Today"
    assert cal.lead_phrase(1) == "Tomorrow"
    assert cal.lead_phrase(7) == "In 7 days"
    e = cal._validate_entry(
        {"date": "2026-06-25", "event": "SpaceX unlock", "lead_days": [1]})
    title = cal.reminder_title(e, 1)
    assert title == "📅 Tomorrow — SpaceX unlock (Jun 25)", title
    _ok("wording: lead_phrase + reminder_title (no leading-zero date)")


# === dedup DB roundtrip ===

def test_dedup_roundtrip():
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        from config import settings as _s
        _s.db_path = tmp.name
        db._conn = None
        db.get_connection()
        assert not db.reminder_already_sent("2026-06-24", "unlock", 1)
        db.mark_reminder_sent("2026-06-24", "unlock", 1)
        assert db.reminder_already_sent("2026-06-24", "unlock", 1)
        # different lead / date / event are independent
        assert not db.reminder_already_sent("2026-06-24", "unlock", 0)
        assert not db.reminder_already_sent("2026-06-25", "unlock", 1)
        # idempotent re-mark doesn't raise
        db.mark_reminder_sent("2026-06-24", "unlock", 1)
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass
    _ok("dedup: already_sent/mark roundtrip, per (date,event,lead), idempotent")


# === job embed ===

def test_job_embed_build():
    from reminders import calendar as cal
    from reminders.job import _build_embed
    e = cal._validate_entry({
        "date": "2026-06-25", "event": "SpaceX unlock",
        "lead_days": [0], "note": "Lockup expiry."})
    embed = _build_embed(e, 0)
    assert embed.title == "📅 Today — SpaceX unlock (Jun 25)", embed.title
    assert embed.description == "Lockup expiry."
    assert embed.color.value == 0xF1C40F
    _ok("job: embed title/note/color")


# === wiring ===

def test_job_pings_everyone_and_dedups():
    """The job posts @everyone + the embed in one message with explicit
    AllowedMentions(everyone=True), and respects the dedup guard."""
    import asyncio
    from datetime import date
    from unittest.mock import patch
    import reminders.job as job
    from reminders import calendar as cal

    today_entry = cal._validate_entry({
        "date": date.today().isoformat(), "event": "$SPCX unlock — Wave 1",
        "lead_days": [0], "note": "+20% unlock."})

    sent = []

    class _FakeChannel:
        name = "stonks-yapping"
        async def send(self, content=None, embed=None, allowed_mentions=None):
            sent.append({"content": content, "embed": embed,
                         "allowed_mentions": allowed_mentions})
            return object()

    class _FakeBot:
        def get_channel(self, cid):
            return _FakeChannel()

    marked = []
    with patch("config.settings.reminder_channel_id", "1317587853282119745"), \
         patch("reminders.calendar.load_calendar", return_value=[today_entry]), \
         patch("reminders.job.db.reminder_already_sent", return_value=False), \
         patch("reminders.job.db.mark_reminder_sent",
               side_effect=lambda *a: marked.append(a)):
        asyncio.run(job.reminder_check_job(_FakeBot()))

    assert len(sent) == 1, sent
    assert sent[0]["content"] == "@everyone", sent[0]
    assert sent[0]["allowed_mentions"] is not None
    assert sent[0]["allowed_mentions"].everyone is True, "must permit @everyone"
    assert sent[0]["embed"].title.startswith("📅 Today"), sent[0]["embed"].title
    assert len(marked) == 1, "must mark sent on success"

    # Dedup: already-sent → no post
    sent.clear()
    with patch("config.settings.reminder_channel_id", "1317587853282119745"), \
         patch("reminders.calendar.load_calendar", return_value=[today_entry]), \
         patch("reminders.job.db.reminder_already_sent", return_value=True):
        asyncio.run(job.reminder_check_job(_FakeBot()))
    assert sent == [], "already-sent reminder must not re-post"
    _ok("job: posts @everyone + embed with AllowedMentions(everyone), "
        "dedup-guarded")


def test_scheduler_registers_cron():
    import scheduler.jobs as sj
    src = inspect.getsource(sj)
    assert "reminder_check_job" in src
    assert "hour=15, minute=45" in src, "must be the 3:45 PM ET cron"
    _ok("scheduler: reminder_check cron registered at 15:45 ET")


def test_reminders_command_present():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod)
    assert 'name="reminders"' in src
    assert "_check_pulse_channel" in src
    _ok("/reminders command present + channel-allowlisted")


if __name__ == "__main__":
    print("=== reminder system smoke ===")
    test_load_defaults_and_coercion()
    test_load_skips_malformed_keeps_rest()
    test_load_missing_and_nonlist()
    test_due_fires_on_exact_lead_dates()
    test_upcoming_future_only_sorted()
    test_wording()
    test_dedup_roundtrip()
    test_job_embed_build()
    test_job_pings_everyone_and_dedups()
    test_scheduler_registers_cron()
    test_reminders_command_present()
    print("\nALL REMINDER SYSTEM SMOKE TESTS PASS")
