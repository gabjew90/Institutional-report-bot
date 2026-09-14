"""Earnings names wrap to two lines instead of being cut off (owner,
2026-09-14: the Monday sheet printed "Cracker Barrel Old Co…" and
"Dave and Buster's E…")."""
import io
import sys
from unittest.mock import patch

from PIL import Image, ImageDraw

from report import calendar_render as cr
from report.calendar_data import CalendarDay, EarnRow


def _day(name: str) -> CalendarDay:
    return CalendarDay(
        date_iso="2026-09-14", weekday_label="MONDAY 9/14", is_holiday=False,
        bmo=[EarnRow(symbol="CBRL", name=name, cap_musd=1102.0, implied_move=5.9,
                     session_confirmed=True)],
        amc=[EarnRow(symbol="PLAY", name="Dave and Buster's Entertainment, Inc",
                     cap_musd=287.0, implied_move=17.5, session_confirmed=True)])


def _render_capturing(day):
    drawn = []
    real = ImageDraw.ImageDraw.text

    def spy(self, xy, text, *a, **kw):
        drawn.append(str(text))
        return real(self, xy, text, *a, **kw)

    with patch.object(ImageDraw.ImageDraw, "text", spy):
        png = cr.render_calendar_png(day)
    return png, drawn


def test_a_long_name_wraps_to_two_full_lines():
    png, drawn = _render_capturing(_day("Cracker Barrel Old Country Store Inc"))
    joined = " ".join(drawn)
    assert "…" not in joined, [t for t in drawn if "…" in t]
    assert any(t.startswith("Cracker Barrel") for t in drawn)
    assert "Country Store Inc" in joined or "Store Inc" in joined
    assert any("Dave and Buster's" in t for t in drawn)
    assert "Entertainment, Inc" in joined


def test_wrapping_grows_the_canvas_and_a_short_name_stays_one_line():
    short, drawn_short = _render_capturing(_day("Cbrl"))
    long_, _ = _render_capturing(_day("Cracker Barrel Old Country Store Inc"))
    h_short = Image.open(io.BytesIO(short)).height
    h_long = Image.open(io.BytesIO(long_)).height
    assert h_long >= h_short
    assert "Cbrl" in drawn_short


def test_a_name_past_two_lines_truncates_on_line_two_only():
    very = "The Extraordinarily Long Named Holding Company Of Greater Metropolitan Areas Incorporated"
    _, drawn = _render_capturing(_day(very))
    # the wordmark is drawn one glyph at a time, spaces included
    name_lines = [t for t in drawn if t.strip() and t.split()[0] in very.split()]
    assert len(name_lines) == 2, name_lines
    assert name_lines[0].startswith("The Extraordinarily")
    assert name_lines[1].endswith("…"), name_lines


def test_a_single_unbreakable_word_is_truncated_not_bled():
    """_wrap keeps an over-wide single word on its own line; the render
    smoke's sixty A's bled past the right margin until each wrapped
    line was also truncated to the column width."""
    _, drawn = _render_capturing(_day("A" * 60))
    # an all-caps name is title-cased before drawing, so "AAAA…" renders "Aaaa…"
    a_lines = [t for t in drawn if t.startswith("Aaaa")]
    assert len(a_lines) == 1 and a_lines[0].endswith("…"), a_lines


def test_a_column_that_would_overflow_keeps_one_line_names_and_its_footer():
    """Twenty wrapped rows would push the footer off the 2:3 canvas.
    That column falls back to one-line truncation instead."""
    rows = [EarnRow(symbol=f"S{i}", name=f"Company Number {i} Holdings Incorporated",
                    cap_musd=1000.0 - i, implied_move=5.0, session_confirmed=True)
            for i in range(20)]
    day = CalendarDay(date_iso="2026-09-14", weekday_label="MONDAY 9/14",
                      is_holiday=False, bmo=rows, amc=list(rows))
    with patch.object(cr.log, "warning") as warn:
        _, drawn = _render_capturing(day)
    assert not any("footer is being cropped" in str(c) for c in warn.call_args_list), \
        warn.call_args_list
    name_lines = [t for t in drawn if t.startswith("Company Number")]
    assert len(name_lines) == 40, len(name_lines)
    assert all(t.endswith("…") for t in name_lines)


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
