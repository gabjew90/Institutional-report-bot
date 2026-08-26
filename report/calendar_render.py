"""Pillow renderer for the daily calendar graphic. Pure rendering: no
network I/O; the only file reads are committed assets (fonts, logo).

render_calendar_png(day) -> PNG bytes, 1080 wide, height adaptive
to content (700 min, 1620 max), drawn at 2x and downsampled.

Fonts are committed OFL files in assets/fonts/ (the Railway container
has no system fonts). A missing font is a DEPLOY defect and raises at
import; a missing logo is survivable and renders wordmark-only.

Spec: docs/superpowers/specs/2026-08-15-daily-calendar-graphic-design.md
"""

import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from report.calendar_data import CalendarDay

log = logging.getLogger(__name__)

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_FONTS = _ASSETS / "fonts"
_MARK = _ASSETS / "brand" / "omnibeta-mark.png"

# ---- palette (assets/brand/ground.txt + spec §4) ----
GROUND = "#273632"
GOLD = "#E5A93F"
SAGE = "#A8CBA0"
TEAL = "#6CC9BE"
TEXT = "#E8F0EA"
WHITE = "#FFFFFF"

_S = 2                       # supersample factor
_W, _H = 1080 * _S, 1620 * _S  # 1620 = 2:3 hard max; the
# adaptive crop in _finish keeps typical days ~1100-1250 tall
# (4:5 or shorter). Only a packed econ day + two full 15-row
# columns stretches past 1350.
_MARGIN = 66 * _S


def _hex2rgb(h: str) -> tuple:
    return (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16))


_GROUND_RGB = _hex2rgb(GROUND)


def _dim(hex_color: str, alpha: float) -> tuple:
    """Blend a color toward the ground — flat-color 'opacity'."""
    c = _hex2rgb(hex_color)
    return tuple(
        int(g + (v - g) * alpha) for g, v in zip(_GROUND_RGB, c)
    )


def _font(name: str, px: int) -> ImageFont.FreeTypeFont:
    p = _FONTS / name
    if not p.exists():
        # Deploy defect, not a runtime condition — fail loudly (spec §6).
        raise FileNotFoundError(
            f"calendar font missing: {p} — assets/fonts must be "
            f"committed and deployed"
        )
    return ImageFont.truetype(str(p), px * _S)


class _Fonts:
    """Lazy so importing the module never touches disk; first render
    does (and raises clearly if fonts are missing)."""
    _cache: dict = {}

    @classmethod
    def get(cls):
        if not cls._cache:
            cls._cache = {
                "word": _font("Inter-SemiBold.ttf", 30),
                "day": _font("Inter-Bold.ttf", 54),
                "sub": _font("Inter-SemiBold.ttf", 17),
                "band": _font("Inter-SemiBold.ttf", 21),
                "time": _font("JetBrainsMono-Bold.ttf", 22),
                "ev": _font("Inter-Regular.ttf", 23),
                "sym": _font("JetBrainsMono-Bold.ttf", 23),
                "nm": _font("Inter-Regular.ttf", 22),
                "foot": _font("Inter-SemiBold.ttf", 15),
                "closed": _font("Inter-Bold.ttf", 44),
                "note": _font("Inter-Regular.ttf", 20),
                "mv": _font("JetBrainsMono-Bold.ttf", 19),
            }
        return cls._cache


def _tracked(d, y, text, font, fill, tracking, center_x):
    widths = [d.textlength(c, font=font) for c in text]
    total = sum(widths) + tracking * (len(text) - 1)
    x = center_x - total / 2
    for c, w in zip(text, widths):
        d.text((x, y), c, font=font, fill=fill)
        x += w + tracking


def _truncate(d, text, font, max_w):
    if d.textlength(text, font=font) <= max_w:
        return text
    while text and d.textlength(text + "…", font=font) > max_w:
        text = text[:-1].rstrip()
    return text + "…"


def _et_abbrev(date_str: str) -> str:
    """EDT or EST for a given ET date. Never a hardcoded guess."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(
            hour=12, tzinfo=ZoneInfo("America/New_York"))
        return dt.tzname() or "ET"
    except Exception:
        return "ET"


def _band(d, label, x0, x1, y, f):
    d.text((x0, y), label.upper(), font=f["band"], fill=GOLD)
    ly = y + 34 * _S
    d.rectangle([x0, ly, x1, ly + max(1, _S // 2)], fill=_dim(GOLD, 0.38))
    return ly + 14 * _S


def render_calendar_png(day: CalendarDay) -> bytes:
    f = _Fonts.get()
    img = Image.new("RGB", (_W, _H), GROUND)
    d = ImageDraw.Draw(img)
    y = 54 * _S

    # 1. logo mark (survivable if missing — spec §6)
    if _MARK.exists():
        mark = Image.open(_MARK).convert("RGBA")
        mh = 96 * _S
        mw = int(mark.width * mh / mark.height)
        mark = mark.resize((mw, mh), Image.LANCZOS)
        img.paste(mark, ((_W - mw) // 2, y), mark)
        y += mh + 18 * _S
    else:
        log.warning("calendar: omnibeta-mark.png missing — wordmark only")
        y += 24 * _S

    # 2. wordmark
    _tracked(d, y, "OMNIBETA", f["word"], WHITE, int(13 * _S), _W / 2)
    y += 46 * _S

    # 3. gradient hairline: transparent -> gold -> sage -> teal -> transparent
    stops = [(0.0, None), (0.18, GOLD), (0.5, SAGE), (0.82, TEAL), (1.0, None)]
    for px in range(_MARGIN, _W - _MARGIN):
        t = (px - _MARGIN) / (_W - 2 * _MARGIN)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                fr = (t - t0) / (t1 - t0)
                a = _hex2rgb(c0) if c0 else _GROUND_RGB
                b = _hex2rgb(c1) if c1 else _GROUND_RGB
                col = tuple(int(av + (bv - av) * fr) for av, bv in zip(a, b))
                d.rectangle([px, y, px, y + _S - 1], fill=col)
                break
    y += 30 * _S

    # 4. day + date + kicker
    d.text((_W / 2, y), day.weekday_label, font=f["day"], fill=TEXT,
           anchor="ma")
    y += 74 * _S
    _tracked(d, y, "MARKET CALENDAR", f["sub"], _dim(TEXT, 0.5),
             int(6 * _S), _W / 2)
    y += 52 * _S

    col_w = (_W - 2 * _MARGIN - 40 * _S) // 2
    x_l, x_r = _MARGIN, _MARGIN + col_w + 40 * _S

    # --- holiday closed card (spec §6) ---
    if day.is_holiday:
        y += 60 * _S
        d.text((_W / 2, y), "MARKETS CLOSED", font=f["closed"],
               fill=GOLD, anchor="ma")
        y += 70 * _S
        if isinstance(day.is_holiday, str):
            d.text((_W / 2, y), day.is_holiday, font=f["ev"],
                   fill=_dim(TEXT, 0.7), anchor="ma")
            y += 60 * _S
        # a rare release on a closure day still renders under it
        if day.econ:
            y += 20 * _S
            y = _econ_block(d, day, f, y, col_w, x_l, x_r)
        return _finish(img, _footer(d, f, y))

    # 5. ECONOMIC
    if not day.econ_available:
        y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
        d.text((_MARGIN, y), "unavailable tonight", font=f["ev"],
               fill=_dim(TEXT, 0.5))
        y += 52 * _S
    elif day.econ:
        y = _econ_block(d, day, f, y, col_w, x_l, x_r)
    else:
        y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
        d.text((_MARGIN, y), "no notable US releases", font=f["ev"],
               fill=_dim(TEXT, 0.5))
        y += 52 * _S

    # 6. BEFORE OPEN / AFTER CLOSE
    any_flag = False
    any_move = False
    if not day.earnings_available:
        y = _band(d, "Earnings", _MARGIN, _W - _MARGIN, y, f)
        d.text((_MARGIN, y), "unavailable tonight", font=f["ev"],
               fill=_dim(TEXT, 0.5))
        y += 52 * _S
    else:
        yl = _band(d, "Before Open", x_l, x_l + col_w, y, f)
        _band(d, "After Close", x_r, x_r + col_w, y, f)
        col_bottom = yl
        for cx, rows, dropped in (
            (x_l, day.bmo, day.dropped_bmo),
            (x_r, day.amc, day.dropped_amc),
        ):
            cy = yl
            for r in rows:
                sym = r.symbol + ("" if r.session_confirmed else "*")
                any_flag = any_flag or not r.session_confirmed
                any_move = any_move or r.implied_move is not None
                d.text((cx, cy), sym, font=f["sym"], fill=TEXT)
                # Implied move, right-aligned at the column edge. A name
                # with no honest straddle gets a dash, never a guess
                # (2026-08-25) — same discipline as the session flag.
                mv = (f"±{r.implied_move:.1f}%"
                      if r.implied_move is not None else "—")
                mv_w = d.textlength(mv, font=f["mv"])
                d.text((cx + col_w - mv_w, cy + 2 * _S), mv, font=f["mv"],
                       fill=GOLD if r.implied_move is not None
                       else _dim(TEXT, 0.28))
                nm = r.name.title() if r.name.isupper() else r.name
                d.text(
                    (cx + 108 * _S, cy + 1 * _S),
                    _truncate(d, nm, f["nm"],
                              col_w - 116 * _S - mv_w - 14 * _S),
                    font=f["nm"], fill=_dim(TEXT, 0.55),
                )
                cy += 40 * _S
            if dropped:
                d.text((cx, cy + 2 * _S), f"+{dropped} more",
                       font=f["note"], fill=_dim(TEXT, 0.4))
                cy += 40 * _S
            col_bottom = max(col_bottom, cy)
        y = col_bottom

    footer_y = _footer(d, f, y)
    if any_flag:
        # footer baseline, left-aligned — the centered ALL TIMES ET
        # leaves the left margin clear
        d.text((_MARGIN, footer_y), "* session not confirmed",
               font=f["note"], fill=_dim(TEXT, 0.4))
    if any_move:
        # Name the method: a bare ±% invites "implied by what?".
        # Right-aligned, under the move column it explains.
        legend = "± = ATM STRADDLE"
        lw = d.textlength(legend, font=f["note"])
        d.text((_W - _MARGIN - lw, footer_y), legend,
               font=f["note"], fill=_dim(TEXT, 0.34))
    return _finish(img, footer_y)


def _econ_block(d, day: CalendarDay, f, y, col_w, x_l, x_r) -> int:
    y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
    rows = day.econ
    half = (len(rows) + 1) // 2
    for ci, chunk in enumerate([rows[:half], rows[half:]]):
        cy = y
        cx = x_l if ci == 0 else x_r
        for r in chunk:
            # White, with the zone spelled out after the time.
            # The abbreviation is DERIVED, not hardcoded: the room
            # is on ET, which is EDT from March to November and EST
            # the rest of the year. Printing a flat "EST" in August
            # would put a wrong label on a correct time.
            _t = f"{r.time_et} {_et_abbrev(day.date_iso)}"
            d.text((cx, cy), _t, font=f["time"], fill=TEXT)
            d.text(
                (cx + 132 * _S, cy + 1 * _S),
                _truncate(d, r.event, f["ev"], col_w - 138 * _S),
                font=f["ev"], fill=_dim(TEXT, 0.92),
            )
            cy += 38 * _S
    return y + max(1, half) * 38 * _S + 34 * _S


def _footer(d, f, content_bottom: int) -> int:
    """Draw the centered footer just below the content and return its
    y. The canvas is cropped to the footer (adaptive height) — a
    10-15-row day no longer leaves a third of the sheet empty."""
    fy = content_bottom + 34 * _S
    _tracked(d, fy, "ALL TIMES ET", f["foot"],
             _dim(TEXT, 0.38), int(4 * _S), _W / 2)
    return fy


def _finish(img: Image.Image, footer_y: int) -> bytes:
    """Crop to content height (min 700px, max the full 4:5 1350px),
    then downsample from the 2x supersample."""
    h2 = min(_H, max(700 * _S, footer_y + 56 * _S))
    img = img.crop((0, 0, _W, h2))
    img = img.resize((1080, h2 // _S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
