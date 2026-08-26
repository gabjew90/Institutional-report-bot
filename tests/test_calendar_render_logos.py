"""Row logos must be purely additive to the layout.

The contract is that a logo DECORATES a row and never sizes it. If the
gutter were sized to the logo, a column would left-align differently
depending on whether Finnhub happened to have artwork for a given name,
and symbols would fail to line up down the column -- which reads as a
rendering bug, not a missing logo.
"""
import io
import sys

from PIL import Image

from report.calendar_data import CalendarDay, EarnRow, EconRow
from report import calendar_render as cr


def _day(with_logos: bool) -> CalendarDay:
    tile = _tile() if with_logos else b""
    mk = lambda s, n, c, m: EarnRow(          # noqa: E731
        s, n, c, m, logo=tile, session_confirmed=True)
    return CalendarDay(
        date_iso="2026-08-27", weekday_label="THURSDAY 8/27",
        is_holiday=False,
        econ=[EconRow("8:30", "Initial Jobless Claims", "medium")],
        bmo=[mk("NVDA", "Nvidia Corp", 3_400_000, 7.2),
             mk("AAPL", "Apple Inc", 3_100_000, 3.1)],
        amc=[mk("AVGO", "Broadcom Inc", 1_500_000, 5.5)],
    )


def _tile() -> bytes:
    from report.calendar_data import _downscale_logo
    src = Image.new("RGBA", (128, 128), (200, 30, 40, 255))
    b = io.BytesIO()
    src.save(b, "PNG")
    return _downscale_logo(b.getvalue())


def test_logos_do_not_change_the_canvas_height():
    """Same rows, logos on and off. Identical geometry."""
    with_l = Image.open(io.BytesIO(cr.render_calendar_png(_day(True))))
    without = Image.open(io.BytesIO(cr.render_calendar_png(_day(False))))
    assert with_l.size == without.size, (
        f"logos changed the layout: {with_l.size} vs {without.size}")


def test_a_sheet_with_no_logos_still_renders():
    png = cr.render_calendar_png(_day(False))
    assert png[:4] == b"\x89PNG"
    assert len(png) > 1000


def test_draw_logo_is_a_noop_on_empty_bytes():
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    before = img.tobytes()
    cr._draw_logo(img, b"", 10, 10)
    assert img.tobytes() == before, "empty logo drew something"


def test_draw_logo_swallows_corrupt_bytes():
    """Cache bytes come from a third-party URL. Corruption must not
    reach the render as an exception."""
    img = Image.new("RGB", (100, 100), (0, 0, 0))
    before = img.tobytes()
    cr._draw_logo(img, b"definitely not a png", 10, 10)
    assert img.tobytes() == before


def test_gutter_matches_the_stored_tile_size():
    """calendar_data stores tiles at LOGO_PX * 2; the renderer paints at
    _LOGO_PX. If these drift the tile is resized on every paste, or
    worse, overlaps the symbol."""
    from report.calendar_data import LOGO_PX
    assert cr._LOGO_PX == LOGO_PX * cr._S
    assert cr._LOGO_GUTTER > cr._LOGO_PX, "tile would touch the symbol"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
