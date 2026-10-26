"""Foreign exchange hours computed in code (2026-10-26 audit A7/A8: at 8:15 PM
ET the bot said Seoul was at 9:15 PM and KRX closed; it was 9:15 AM, open)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from discord_bot.world_clock import clock_note

UTC = ZoneInfo("UTC")


def test_krx_open_at_915_kst():
    n = clock_note("what time is in Korea what time the market opens", datetime(2026, 10, 21, 0, 15, tzinfo=UTC))
    assert "09:15 AM Wed Oct 21" in n and "OPEN, regular session" in n and "08:15 PM Tue Oct 20 ET" in n


def test_closed_overnight_and_weekend():
    assert "CLOSED" in clock_note("is the korean market open", datetime(2026, 10, 21, 12, 0, tzinfo=UTC))
    assert "CLOSED (weekend)" in clock_note("is tokyo open right now", datetime(2026, 10, 24, 2, 0, tzinfo=UTC))


def test_no_note_without_a_time_question_or_a_named_market():
    assert clock_note("how much of X revenue comes from India") == ""
    assert clock_note("is the market open right now") == ""
