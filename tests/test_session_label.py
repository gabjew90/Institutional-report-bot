"""The session label knows market holidays and early closes (2026-11-22).

Until then only weekends were known: a quote on Thanksgiving would have
read as a live session's move, and one at 2 PM on the half day after it as
still trading. Dates are the NYSE's own (nyse.com/trade/hours-calendars)."""
from datetime import date, datetime

import pytz

import world_context as W
from report import market_data as M

ET = pytz.timezone("America/New_York")


def _at(y, mo, d, h, mi=0):
    return ET.localize(datetime(y, mo, d, h, mi))


def test_a_market_holiday_is_a_closed_day_and_names_the_last_session():
    code, note = M._session_label(_at(2026, 11, 26, 12))          # Thanksgiving
    assert code == "WEEKEND-CLOSED"
    assert "Thanksgiving Day" in note and "Wednesday's full session" in note and "Friday" not in note
    assert M.last_session_day(_at(2026, 11, 26, 12)) == date(2026, 11, 25)


def test_the_half_day_ends_at_one():
    assert M._session_label(_at(2026, 11, 27, 12))[0] == "OPEN"
    assert "early close" in M._session_label(_at(2026, 11, 27, 12))[1]
    code, note = M._session_label(_at(2026, 11, 27, 14))
    assert code == "AFTER-HOURS" and "closed early today, at 1:00 PM ET" in note
    assert M._session_label(_at(2026, 12, 24, 13, 30))[0] == "AFTER-HOURS"
    # an ordinary day still closes at four
    assert M._session_label(_at(2026, 11, 25, 15))[0] == "OPEN"
    assert M._session_label(_at(2026, 11, 25, 16, 5))[0] == "AFTER-HOURS"


def test_before_the_open_the_close_is_the_last_sessions_not_always_yesterdays():
    # the morning after Thanksgiving: Wednesday's close
    code, note = M._session_label(_at(2026, 11, 27, 8))
    assert code == "PRE-MARKET" and "Wednesday's full session" in note and "YESTERDAY" not in note
    # a Monday morning: Friday's
    assert "Friday's full session" in M._session_label(_at(2026, 11, 30, 8))[1]
    # a Tuesday morning: yesterday's, as before
    assert "YESTERDAY'S full session" in M._session_label(_at(2026, 12, 1, 8))[1]


def test_a_weekend_after_a_friday_holiday_names_thursday():
    code, note = M._session_label(_at(2026, 7, 4, 11))            # Friday July 3 was the holiday
    assert code == "WEEKEND-CLOSED" and "Thursday's full session" in note
    # an ordinary weekend keeps its wording
    assert "Friday's full session" in M._session_label(_at(2026, 11, 28, 11))[1]


def test_the_calendar_helpers():
    assert W.market_close_time("2026-11-27") == (13, 0) and W.market_close_time("2026-11-25") == (16, 0)
    assert not W.is_trading_day("2026-11-26") and not W.is_trading_day("2026-11-28") and W.is_trading_day("2026-11-27")
    assert W.previous_trading_day("2026-11-27") == "2026-11-25"
    assert W.previous_trading_day("2026-11-30") == "2026-11-27"
    # the early closes fall on trading days
    assert all(W.is_trading_day(d) for d in W.EARLY_CLOSES)
