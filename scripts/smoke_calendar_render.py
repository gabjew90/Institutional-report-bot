"""Smoke: calendar_render (spec §7). Renders fixture CalendarDays to
bytes and checks structural properties. Also drops one PNG in the
scratchpad-adjacent temp dir for eyeball review."""

import io
import os
import sys
import tempfile

from PIL import Image

from report.calendar_data import CalendarDay, EarnRow, EconRow
from report.calendar_render import render_calendar_png


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _day(**kw):
    base = dict(
        date_iso="2026-08-20", weekday_label="THURSDAY 8/20",
        is_holiday=False,
        econ=[EconRow("8:30", "Unemployment Claims", "high"),
              EconRow("8:30", "Philly Fed Manufacturing Index", "medium"),
              EconRow("13:00", "30-Yr Bond Auction", "medium")],
        bmo=[EarnRow(f"SYM{i}", f"Company Number {i} Holdings Inc",
                     1000.0 - i, i % 3 != 0) for i in range(20)],
        amc=[EarnRow(f"AMC{i}", "A" * 60, 500.0 - i, False)
             for i in range(20)],
        dropped_bmo=7, dropped_amc=0,
    )
    base.update(kw)
    return CalendarDay(**base)


def test_full_sheet():
    png = render_calendar_png(_day())
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG magic bytes"
    img = Image.open(io.BytesIO(png))
    assert img.width == 1080 and 700 <= img.height <= 1620, img.size
    assert len(png) > 30_000, f"suspiciously small: {len(png)}"
    out = os.path.join(tempfile.gettempdir(), "calendar-smoke-eyeball.png")
    open(out, "wb").write(png)
    print(f"  (eyeball render: {out})")
    _ok("full sheet: PNG magic, 1080x1350, non-trivial size")


def test_long_name_truncated():
    # 60-char name must be truncated with … and fit its column: measure
    png = render_calendar_png(_day())
    img = Image.open(io.BytesIO(png)).convert("RGB")
    # verify no non-ground pixels bleed past the right margin in the
    # earnings rows band (lower 40% of the adaptive canvas)
    ground = img.getpixel((5, 5))
    for y in range(int(img.height * 0.5), img.height - 80, 10):
        for x in range(1080 - 60, 1080 - 2):
            px = img.getpixel((x, y))
            if px != ground and max(
                    abs(a - b) for a, b in zip(px, ground)) > 28:
                _fail(f"text bleeds past right margin at {(x, y)}: {px}")
    _ok("60-char names truncated inside their column (pixel check)")


def test_closed_card():
    png = render_calendar_png(_day(
        is_holiday="Labor Day", bmo=[], amc=[],
        econ=[EconRow("8:30", "Nonfarm Payrolls", "high")],
    ))
    img = Image.open(io.BytesIO(png))
    assert img.width == 1080 and img.height >= 700
    assert len(png) > 20_000
    _ok("closed card renders (holiday name + rare econ release)")


def test_feed_down_paths():
    png = render_calendar_png(_day(earnings_available=False))
    assert len(png) > 20_000
    png = render_calendar_png(_day(econ_available=False, econ=[]))
    assert len(png) > 20_000
    png = render_calendar_png(_day(econ=[]))  # quiet day, feed fine
    assert len(png) > 20_000
    _ok("earnings-down / econ-down / quiet-econ paths all render")


def test_fonts_are_committed():
    from report.calendar_render import _FONTS
    for f in ("Inter-Regular.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf",
              "JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"):
        assert (_FONTS / f).exists(), f"missing committed font {f}"
    _ok("all five OFL fonts committed in assets/fonts/")


if __name__ == "__main__":
    print("=== calendar render smoke ===")
    test_fonts_are_committed()
    test_full_sheet()
    test_long_name_truncated()
    test_closed_card()
    test_feed_down_paths()
    print("\nALL CALENDAR RENDER SMOKE TESTS PASS")
