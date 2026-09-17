"""An empty earnings band says so (2026-09-17). The 9/17 and 9/18 sheets
fell in the trough between earnings seasons and both bands were blank,
which the owner read as a feed problem. The line is drawn only when
the feed was up and the band has no rows."""
from unittest.mock import patch

from PIL import ImageDraw

from report import calendar_render as cr
from report.calendar_data import CalendarDay, EarnRow, EconRow


def _render_capturing(day):
    drawn = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append(str(text))
        return real(self, xy, text, *a, **kw)
    with patch.object(ImageDraw.ImageDraw, "text", spy):
        cr.render_calendar_png(day)
    return drawn


def _day(bmo, amc, available=True):
    return CalendarDay(date_iso="2026-09-18", weekday_label="FRIDAY 9/18", is_holiday=False,
                       econ=[EconRow("9:15", "Industrial Production m/m", "medium")],
                       bmo=bmo, amc=amc, earnings_available=available)


ROW = EarnRow(symbol="COST", name="Costco Wholesale Corp", cap_musd=396_000.0,
              implied_move=4.2, session_confirmed=True)


def test_each_empty_band_gets_the_line_and_a_filled_one_does_not():
    assert _render_capturing(_day([], [])).count(cr.EMPTY_BAND_TEXT) == 2
    assert _render_capturing(_day([], [ROW])).count(cr.EMPTY_BAND_TEXT) == 1
    assert _render_capturing(_day([ROW], [ROW])).count(cr.EMPTY_BAND_TEXT) == 0


def test_a_feed_outage_keeps_its_own_line():
    drawn = _render_capturing(_day([], [], available=False))
    assert cr.EMPTY_BAND_TEXT not in drawn
    assert "unavailable tonight" in drawn
