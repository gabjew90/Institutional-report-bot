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
# Row logos. _LOGO_PX must match calendar_data.LOGO_PX * _S — the cache
# stores tiles already at this size so the render path never resizes.
# The gutter is wider than the tile, and is reserved on EVERY row
# whether or not a logo exists, so symbols stay aligned down the column.
_LOGO_PX = 22 * _S
_LOGO_GUTTER = 30 * _S
_LOGO_DY = 3 * _S           # nudge onto the symbol's optical line


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
                "evb": _font("Inter-SemiBold.ttf", 23),   # important econ rows
                "sym": _font("JetBrainsMono-Bold.ttf", 23),
                "nm": _font("Inter-Regular.ttf", 22),
                "nmb": _font("Inter-SemiBold.ttf", 22),   # important earnings rows
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


def _wrap(d, text, font, max_w) -> list[str]:
    """Word-wrap to max_w. The two-column events layout wraps rather
    than truncates (owner call 2026-09-09): a column half the sheet wide
    cut 'Prelim Benchmark Payrolls Revision' in 2026-08 and that is why
    econ went single-column; wrapping makes the second column
    affordable again."""
    words, lines, cur = (text or "").split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if d.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


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


EMPTY_BAND_TEXT = "no names at scale confirmed"
# Friday's sheet covers Monday, past the end of the ForexFactory week:
# the econ rows are FRED's scheduled majors only (2026-09-25).
ECON_PARTIAL_EMPTY = "no major US releases scheduled · full list posts Sunday"
ECON_PARTIAL_NOTE = "major releases only · full list posts Sunday"


def _econ_empty_text(day) -> str:
    return ECON_PARTIAL_EMPTY if getattr(day, "econ_partial", False) else "no notable US releases"


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

    # 5. ECONOMIC (+ INDUSTRY EVENTS beside it when the corpus carries a
    # verified conference schedule for the day, spec 2026-09-09; the
    # owner picked the two-column layout on 2026-09-09).
    if getattr(day, "conferences", None):
        y = _events_block(d, day, f, y, col_w, x_l, x_r)
    elif not day.econ_available:
        y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
        d.text((_MARGIN, y), "unavailable tonight", font=f["ev"],
               fill=_dim(TEXT, 0.5))
        y += 52 * _S
    elif day.econ:
        y = _econ_block(d, day, f, y, col_w, x_l, x_r)
    else:
        y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
        d.text((_MARGIN, y), _econ_empty_text(day), font=f["ev"],
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
            # Name layout for the whole column first (2026-09-14). Names
            # wrap to two lines, but only while the column still ends
            # above the footer: twenty wrapped rows add ~520px and would
            # push the footer off the fixed 2:3 canvas (the footer sits
            # 34px under the content and _finish needs 56px after it), so
            # a column that would overflow keeps one-line truncation.
            _plans = []
            for r in rows:
                _mv = (f"±{r.implied_move:.1f}%"
                       if r.implied_move is not None else "—")
                _mv_w = d.textlength(_mv, font=f["mv"])
                _nmf = f["nmb"] if getattr(r, "important", False) else f["nm"]
                _nm = r.name.title() if r.name.isupper() else r.name
                _w = ((cx + col_w - _mv_w - 14 * _S)
                      - (cx + _LOGO_GUTTER + 108 * _S))
                _raw = _wrap(d, _nm, _nmf, _w)
                if len(_raw) > 2:
                    _raw = [_raw[0], " ".join(_raw[1:])]
                # a single word wider than the column must still fit
                _plans.append(([_truncate(d, ln, _nmf, _w) for ln in _raw],
                               _truncate(d, _nm, _nmf, _w)))
            _n_wrapped = sum(1 for lines, _one in _plans if len(lines) > 1)
            _wrap_ok = (yl + len(rows) * 40 * _S + _n_wrapped * 26 * _S
                        <= _H - 90 * _S)
            for _ri, r in enumerate(rows):
                sym = r.symbol + ("" if r.session_confirmed else "*")
                any_flag = any_flag or not r.session_confirmed
                any_move = any_move or r.implied_move is not None
                # The logo gutter is reserved UNCONDITIONALLY and the
                # logo is drawn into it only when present. Sizing the
                # row to the logo instead would left-align rows
                # differently depending on whether Finnhub happened to
                # have artwork, and a column whose symbols do not line
                # up reads as a rendering bug.
                _draw_logo(img, getattr(r, "logo", b""), cx, cy)
                d.text((cx + _LOGO_GUTTER, cy), sym, font=f["sym"],
                       fill=TEXT)
                # Implied move, right-aligned at the column edge. A name
                # with no honest straddle gets a dash, never a guess
                # (2026-08-25) — same discipline as the session flag.
                mv = (f"±{r.implied_move:.1f}%"
                      if r.implied_move is not None else "—")
                mv_w = d.textlength(mv, font=f["mv"])
                d.text((cx + col_w - mv_w, cy + 2 * _S), mv, font=f["mv"],
                       fill=GOLD if r.implied_move is not None
                       else _dim(TEXT, 0.28))
                _nm_x = cx + _LOGO_GUTTER + 108 * _S
                # Important rows (major-ticker list, mega-cap, or a
                # bank wrote earnings content about it this week) get
                # the semibold name at full brightness; the rest stay
                # regular and dimmed (owner call 2026-09-02).
                _imp = getattr(r, "important", False)
                nm_font = f["nmb"] if _imp else f["nm"]
                # Wrapped to two lines (owner, 2026-09-14: "Cracker
                # Barrel Old Co…"), or the one-line truncation when the
                # column would overflow the canvas; see the plan pass.
                _lines = _plans[_ri][0] if _wrap_ok else [_plans[_ri][1]]
                for _i, _line in enumerate(_lines):
                    d.text((_nm_x, cy + 1 * _S + _i * 26 * _S), _line,
                           font=nm_font, fill=(TEXT if _imp else _dim(TEXT, 0.55)))
                cy += 40 * _S + (26 * _S if len(_lines) > 1 else 0)
            if not rows:
                # An empty band says so (2026-09-17: the mid-September
                # sheets were blank between earnings seasons and read as
                # a feed failure). The feed-down case above has its own
                # line; this one means the sources agree nobody at scale
                # reports in this session.
                d.text((cx + _LOGO_GUTTER, cy), EMPTY_BAND_TEXT, font=f["nm"],
                       fill=_dim(TEXT, 0.4))
                cy += 40 * _S
            # "+N more" is NOT rendered (owner call 2026-08-27). The
            # dropped counts stay in CalendarDay and the pipeline event
            # for QC, but the published sheet shows only the names that
            # earned a row.
            _ = dropped
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
    """Single full-width column (owner call 2026-08-27). The old
    two-column layout gave each event name roughly half the sheet and
    truncated routine names ("Prelim Benchmark Payrolls Rev..."). Econ
    days are short — 9 events on the heaviest recorded day — so the
    height cost of one row per event is small and the full name always
    fits. The _truncate stays as a safety net only."""
    y = _band(d, "Economic", _MARGIN, _W - _MARGIN, y, f)
    cy = y
    for r in day.econ:
        # White, with the zone spelled out after the time.
        # The abbreviation is DERIVED, not hardcoded: the room
        # is on ET, which is EDT from March to November and EST
        # the rest of the year. Printing a flat "EST" in August
        # would put a wrong label on a correct time.
        _t = f"{r.time_et} {_et_abbrev(day.date_iso)}"
        d.text((_MARGIN, cy), _t, font=f["time"], fill=TEXT)
        # Important rows (Tier-1 series or feed-rated high impact) are
        # bold and full-bright; the rest stay regular and slightly
        # dimmed so the eye lands on the prints that move the tape
        # (owner call 2026-09-02).
        ev_font = f["evb"] if getattr(r, "important", False) else f["ev"]
        d.text(
            (_MARGIN + 132 * _S, cy + 1 * _S),
            _truncate(d, r.event, ev_font,
                      _W - 2 * _MARGIN - 138 * _S),
            font=ev_font,
            fill=TEXT if getattr(r, "important", False) else _dim(TEXT, 0.80),
        )
        cy += 38 * _S
    if getattr(day, "econ_partial", False):
        d.text((_MARGIN, cy), ECON_PARTIAL_NOTE, font=f["ev"], fill=_dim(TEXT, 0.5))
        cy += 38 * _S
    return cy + 34 * _S


def _events_block(d, day: CalendarDay, f, y, col_w, x_l, x_r) -> int:
    """Economic in the LEFT column, Industry Events in the RIGHT, sharing
    the earnings columns' geometry so everything lines up down the
    sheet. Each conference is ONE entry in the same shape as an econ
    row: ET start time in the gutter, the conference name beside it,
    and the admitted names hanging under the name in the symbol font,
    market-cap order (owner calls 2026-09-09 and 2026-09-10). Names
    wrap; nothing truncates."""
    tz = _et_abbrev(day.date_iso)
    t_w = 144 * _S                      # room for '13:01 EDT' plus a gap
    name_w = col_w - t_w - 6 * _S

    # ---- left: economic
    cy = _band(d, "Economic", x_l, x_l + col_w, y, f)
    if not day.econ_available:
        d.text((x_l, cy), "unavailable tonight", font=f["ev"], fill=_dim(TEXT, 0.5))
        cy += 40 * _S
    elif not day.econ:
        for line in _wrap(d, _econ_empty_text(day), f["ev"], col_w):
            d.text((x_l, cy), line, font=f["ev"], fill=_dim(TEXT, 0.5))
            cy += 30 * _S
        cy += 10 * _S
    for r in day.econ:
        d.text((x_l, cy), f"{r.time_et} {tz}", font=f["time"], fill=TEXT)
        imp = getattr(r, "important", False)
        font = f["evb"] if imp else f["ev"]
        fill = TEXT if imp else _dim(TEXT, 0.80)
        for line in _wrap(d, r.event, font, name_w):
            d.text((x_l + t_w, cy + 1 * _S), line, font=font, fill=fill)
            cy += 30 * _S
        cy += 8 * _S
    if day.econ and getattr(day, "econ_partial", False):
        for line in _wrap(d, ECON_PARTIAL_NOTE, f["ev"], name_w + t_w):
            d.text((x_l, cy), line, font=f["ev"], fill=_dim(TEXT, 0.5))
            cy += 30 * _S
    left_bottom = cy

    # ---- right: industry events
    cy = _band(d, "Industry Events", x_r, x_r + col_w, y, f)
    x_txt = x_r + t_w
    for c in day.conferences:
        if c.time_et:
            d.text((x_r, cy), f"{c.time_et} {tz}", font=f["time"], fill=TEXT)
        name_font = f["evb"] if getattr(c, "important", False) else f["ev"]
        for line in _wrap(d, c.conference, name_font, name_w):
            d.text((x_txt, cy + 1 * _S), line, font=name_font, fill=TEXT)
            cy += 30 * _S
        cy += 4 * _S
        for line in _wrap(d, "  ".join(c.tickers), f["sym"], name_w):
            d.text((x_txt, cy), line, font=f["sym"], fill=TEXT)
            cy += 32 * _S
        cy += 12 * _S
    right_bottom = cy
    return max(left_bottom, right_bottom) + 26 * _S


def _footer(d, f, content_bottom: int) -> int:
    """Draw the centered footer just below the content and return its
    y. The canvas is cropped to the footer (adaptive height) — a
    10-15-row day no longer leaves a third of the sheet empty."""
    fy = content_bottom + 34 * _S
    _tracked(d, fy, "ALL TIMES ET", f["foot"],
             _dim(TEXT, 0.38), int(4 * _S), _W / 2)
    return fy


def _draw_logo(img: Image.Image, png: bytes, cx: int, cy: int) -> None:
    """Paste a row logo into the reserved gutter. No-op when absent.

    Swallows every failure. These bytes come from a third-party URL
    through a cache, the sheet renders unattended at 04:00, and no logo
    is worth failing a calendar over.
    """
    if not png:
        return
    try:
        import io
        tile = Image.open(io.BytesIO(png))
        tile.load()
        if tile.mode != "RGBA":
            tile = tile.convert("RGBA")
        if tile.size != (_LOGO_PX, _LOGO_PX):
            # Cached at the wrong size (a changed LOGO_PX, an older
            # row). Correct it here rather than pasting something that
            # overlaps the symbol.
            tile = tile.resize((_LOGO_PX, _LOGO_PX), Image.LANCZOS)
        # Vertically nudged onto the symbol's optical line rather than
        # the row box. Pasted THROUGH its own alpha so the rounded
        # corners take the sheet's background instead of white.
        img.paste(tile, (int(cx), int(cy + _LOGO_DY)), tile)
    except Exception as e:
        log.info(f"calendar render: logo paste skipped ({e})")


def _finish(img: Image.Image, footer_y: int) -> bytes:
    """Crop to content height (min 700px, max the full 2:3 1620px),
    then downsample from the 2x supersample.

    The docstring said "4:5 1350px" until 2026-08-26. The canvas has
    been 2:3 / 1620 since the aspect change, so the comment described a
    crop bound that had not existed for some time -- exactly the kind of
    stale note that gets trusted instead of the code.
    """
    want = footer_y + 56 * _S
    if want > _H:
        # The min() below silently crops whatever does not fit, so the
        # footer legend disappears with no error anywhere. Say so.
        log.warning(
            f"calendar render: content needs {want // _S}px but the "
            f"canvas is {_H // _S}px — the footer is being cropped off. "
            f"Reduce TOP_N or raise the canvas.")
    h2 = min(_H, max(700 * _S, want))
    img = img.crop((0, 0, _W, h2))
    img = img.resize((1080, h2 // _S), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
