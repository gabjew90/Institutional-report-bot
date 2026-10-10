"""Days stocks trade but bonds are shut are said on the calendar sheet
(2026-10-10 audit: Columbus Day's sheet said nothing)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import world_context as W  # noqa: E402


def test_bond_only_closures():
    assert W.bond_market_closed("2026-10-12") == "Columbus Day"
    assert W.bond_market_closed("2026-11-11") == "Veterans Day"
    assert W.bond_market_closed("2026-10-13") == ""
    # never a day the stock market itself is closed
    assert not set(W.BOND_ONLY_CLOSURES) & set(W.US_MARKET_HOLIDAYS)


def test_the_sheet_renders_with_the_note():
    from report.calendar_data import CalendarDay
    from report.calendar_render import render_calendar_png
    day = CalendarDay(date_iso="2026-10-12", weekday_label="MONDAY 10/12",
                      is_holiday=False, bond_closed="Columbus Day")
    assert render_calendar_png(day)[:4] == b"\x89PNG"
