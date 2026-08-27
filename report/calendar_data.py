"""Data layer for the daily calendar graphic — fetch, filter, rank,
truncate. Pure data: no drawing, no Discord. The renderer consumes the
CalendarDay this module builds.

Spec: docs/superpowers/specs/2026-08-15-daily-calendar-graphic-design.md
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import db
from report import news_data

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
TOP_N = 15  # owner call 2026-08-20: 15 per session (was 20, then 10)


@dataclass
class EconRow:
    time_et: str        # "8:30"
    event: str
    impact: str         # "high" | "medium"


@dataclass
class EarnRow:
    symbol: str
    name: str
    cap_musd: float
    implied_move: float | None = None  # ATM straddle ±% into the print
    logo: bytes = b""             # render-ready PNG, b"" = none. Bytes,
    #                          not a URL: the renderer must never touch
    #                          the network.
    session_confirmed: bool = False  # False = Finnhub hour was blank/dmh —
    #                          rendered with the * flag. Verified
    #                          2026-08-20: ~40-60% of rows are blank
    #                          even in final historical data, so the
    #                          flag is load-bearing, not cosmetic.


@dataclass
class CalendarDay:
    date_iso: str
    weekday_label: str            # "THURSDAY 8/20"
    is_holiday: str | bool        # holiday name or False
    econ: list[EconRow] = field(default_factory=list)
    econ_available: bool = True   # False = FF feed down (vs quiet day)
    bmo: list[EarnRow] = field(default_factory=list)
    amc: list[EarnRow] = field(default_factory=list)
    earnings_available: bool = True
    dropped_bmo: int = 0
    dropped_amc: int = 0


def _weekday_label(date_iso: str) -> str:
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    return f"{d.strftime('%A').upper()} {d.month}/{d.day}"


def _to_et_hhmm(utc_iso: str) -> str:
    """'2026-08-20T12:30:00' (UTC) -> '8:30' ET, DST-correct."""
    dt = datetime.fromisoformat(utc_iso.replace("Z", ""))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    loc = dt.astimezone(_ET)
    return f"{loc.hour}:{loc.minute:02d}"


def _resolve_caps(symbols: list[str]) -> dict:
    """Cache-first market caps: hit symbol_market_cap, fetch only the
    misses from Finnhub (paced), upsert what came back.

    A failed lookup is stored as cap 0 so it is still cached, but
    get_market_caps expires failures by calendar day while successes
    keep the 7-day TTL. That split matters here specifically: this
    function feeds the earnings RANKING, so a cap of 0 does not read as
    "unknown", it reads as "smallest company on the sheet". Under the
    old shared 7-day TTL one transient Finnhub failure sorted a mega-cap
    below micro-caps and dropped it from the calendar for a week."""
    cached = db.get_market_caps(symbols)
    missing = [s for s in symbols if s not in cached]
    if missing:
        log.info(
            f"calendar: fetching {len(missing)} uncached caps "
            f"({len(cached)} cache hits)"
        )
        fetched = news_data.fetch_symbol_profiles(missing)
        db.upsert_market_caps(
            [(s, v["cap"], v["name"]) for s, v in fetched.items()]
        )
        cached.update(fetched)
    return cached


# Logo edge length in FINAL (post-downsample) pixels. The renderer
# supersamples 2x, so it scales this up itself. Stored at render size so
# the render path never resizes.
LOGO_PX = 22


def _downscale_logo(raw: bytes) -> bytes:
    """Square PNG of LOGO_PX*2 (supersample size), or b'' if unusable.

    Returns b'' rather than raising on anything malformed. A broken logo
    must degrade to no logo, never to a broken sheet -- the calendar
    posts unattended at 04:00 and nobody is watching it render.
    """
    import io
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        side = LOGO_PX * 2
        # Flatten onto white: many Finnhub logos are transparent PNGs
        # drawn in near-black, invisible against a dark sheet. A white
        # tile reads as a favicon and matches how these appear
        # everywhere else.
        im = im.convert("RGBA")
        canvas = Image.new("RGBA", im.size, (255, 255, 255, 255))
        canvas.alpha_composite(im)
        canvas = canvas.convert("RGB")
        canvas.thumbnail((side, side), Image.LANCZOS)
        square = Image.new("RGB", (side, side), (255, 255, 255))
        square.paste(canvas, ((side - canvas.width) // 2,
                              (side - canvas.height) // 2))
        # Round the corners. A hard white square punches four bright
        # right angles into a dark sheet and reads as a rendering
        # artifact; a rounded tile reads as a favicon. Saved with alpha
        # so the corners take the sheet's own background rather than a
        # colour this module would have to know.
        from PIL import ImageDraw as _ID
        mask = Image.new("L", (side, side), 0)
        _ID.Draw(mask).rounded_rectangle(
            (0, 0, side - 1, side - 1), radius=max(2, side // 5), fill=255)
        square = square.convert("RGBA")
        square.putalpha(mask)
        buf = io.BytesIO()
        square.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        log.info(f"calendar: logo unusable, skipping ({e})")
        return b""


def _resolve_logos(symbols: list[str], profiles: dict) -> dict:
    """symbol -> render-ready PNG bytes (b'' = no logo).

    Cache-first. `profiles` carries the logo URLs already returned by
    the cap fetch, so a symbol whose cap came from cache has no URL here
    and is simply skipped -- its logo is either already cached or will
    be picked up the next time its profile refreshes. Nothing about
    logos is worth an extra Finnhub call.

    Never raises: the calendar renders without logos rather than not at
    all.
    """
    if not symbols:
        return {}
    try:
        cached = db.get_symbol_logos(symbols)
    except Exception as e:
        log.warning(f"calendar: logo cache read failed ({e})")
        return {}

    fetched: list[tuple] = []
    for sym in symbols:
        if sym in cached:
            continue
        entry = profiles.get(sym) or {}
        if "logo" not in entry:
            # This symbol's cap came from cache, so no profile call was
            # made this run and we learned nothing about its logo.
            # Absence of the KEY is the signal, not an empty value: an
            # empty value means Finnhub answered and has no logo, which
            # IS cacheable. Conflating the two would cache "no logo" for
            # every symbol with a warm cap.
            continue
        url = (entry.get("logo") or "").strip()
        if not url:
            fetched.append((sym, b""))
            cached[sym] = b""
            continue
        raw = _http_bytes(url)
        png = _downscale_logo(raw) if raw else b""
        fetched.append((sym, png))
        cached[sym] = png

    if fetched:
        try:
            db.upsert_symbol_logos(fetched)
        except Exception as e:
            log.warning(f"calendar: logo cache write failed ({e})")
        got = sum(1 for _, b in fetched if b)
        log.info(f"calendar: logos fetched {got}/{len(fetched)} "
                 f"({len(symbols) - len(fetched)} from cache)")
    return cached


def _http_bytes(url: str, timeout: int = 8) -> bytes:
    import urllib.request
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "omnibeta-calendar/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            # Cap the read: a logo is a few KB, and an unbounded read of
            # an unexpected URL is how a render job eats memory.
            return r.read(512 * 1024)
    except Exception as e:
        log.info(f"calendar: logo download failed for {url[:60]} ({e})")
        return b""


# Per-session fetch budget for the implied-move selection: how far past
# TOP_N the backfill may walk. Bounded because every attempt is a paced
# Yahoo chain fetch, and the deep tail of a cap-ranked pool is
# micro-caps that will fail the liquidity guards anyway — an unbounded
# walk spends minutes proving what the ranking already implied.
MOVE_FETCH_BUDGET_EXTRA = 10
_MOVE_PACE_S = 0.6


def _implied_move_fetch(sym: str, date_iso: str) -> float | None:
    """One name's implied move. Module-level indirection so tests can
    patch it; the import stays lazy so a broken implied_move module
    degrades to None instead of breaking calendar_data at import."""
    from report.implied_move import implied_move_pct
    return implied_move_pct(sym, date_iso)


def _select_priced(ranked: list[str],
                   date_iso: str) -> list[tuple[str, float]]:
    """First TOP_N names from the cap-ranked pool whose ATM straddle
    prices honestly, in rank order: [(symbol, move_pct), ...].

    A name whose chain cannot be priced (zero-bid leg, spread wider
    than the mid, no ATM put, no options at all) is SKIPPED and the
    next-ranked name takes its slot — owner call 2026-08-27, replacing
    the earlier render-a-dash behaviour. Attempts are capped at
    TOP_N + MOVE_FETCH_BUDGET_EXTRA per session.

    One bad symbol never aborts the walk, and every skip is logged by
    name — a row silently vanishing from the sheet must be explicable
    from the log.
    """
    import time as _time
    kept: list[tuple[str, float]] = []
    skipped: list[str] = []
    budget = TOP_N + MOVE_FETCH_BUDGET_EXTRA
    for i, sym in enumerate(ranked[:budget]):
        if len(kept) >= TOP_N:
            break
        if i > 0 and _MOVE_PACE_S:
            _time.sleep(_MOVE_PACE_S)
        try:
            mv = _implied_move_fetch(sym, date_iso)
        except Exception as e:
            log.info(f"calendar: implied move {sym} raised ({e}) — "
                     f"treated as unpriceable")
            mv = None
        if mv is None:
            skipped.append(sym)
            continue
        kept.append((sym, mv))
    if skipped:
        log.info(
            f"calendar: dropped {len(skipped)} unpriceable name(s): "
            f"{', '.join(skipped)}")
    leftover = len(ranked) - min(len(ranked), TOP_N + MOVE_FETCH_BUDGET_EXTRA)
    if len(kept) < TOP_N and leftover > 0:
        log.info(
            f"calendar: fetch budget exhausted with {len(kept)}/{TOP_N} "
            f"kept — {leftover} ranked name(s) never attempted")
    return kept


def build_calendar_day(date_iso: str) -> CalendarDay:
    """Assemble everything the renderer needs for one session date."""
    from world_context import is_us_market_holiday

    day = CalendarDay(
        date_iso=date_iso,
        weekday_label=_weekday_label(date_iso),
        is_holiday=is_us_market_holiday(date_iso) or False,
    )

    # --- economic events (FF; None = feed down, [] = quiet day) ---
    econ = news_data.fetch_us_econ_events_for_date(date_iso)
    if econ is None:
        day.econ_available = False
    else:
        rows = sorted(econ, key=lambda e: e.get("time") or "")
        day.econ = [
            EconRow(
                time_et=_to_et_hhmm(e["time"]),
                event=(e.get("event") or "").strip(),
                impact=(e.get("impact") or "").lower(),
            )
            for e in rows
            if e.get("time") and e.get("event")
        ]

    # --- earnings (holiday closed-card renders no earnings columns) ---
    if day.is_holiday:
        return day

    raw = news_data.fetch_earnings_calendar_all(date_iso)
    if raw is None:
        day.earnings_available = False
        return day

    bmo_syms, amc_syms = [], []
    confirmed: dict[str, bool] = {}
    # Finnhub can return a symbol more than once for one date (a revised
    # row, or the same name under two sessions). Without a seen set the
    # duplicate is ranked and rendered twice, and since ranking is by
    # cap the two copies sort ADJACENT -- the sheet shows the same
    # company on consecutive lines. First row wins.
    _seen: set[str] = set()
    for r in raw:
        sym = (r.get("symbol") or "").strip()
        if not sym or sym in _seen:
            continue
        _seen.add(sym)
        hour = (r.get("hour") or "").lower()
        # blank / dmh (during market hours) land in AMC, FLAGGED —
        # that's where a reader looks for them last, and the flag keeps
        # the sheet honest that the session is Finnhub's gap, not fact.
        if hour == "bmo":
            bmo_syms.append(sym)
            confirmed[sym] = True
        else:
            amc_syms.append(sym)
            confirmed[sym] = hour == "amc"

    caps = _resolve_caps(bmo_syms + amc_syms)

    # A name earns its row by having an honestly-priceable implied move
    # (owner call 2026-08-27: the 8 dash rows on that sheet were all
    # illiquid tail names — zero-bid legs, spreads wider than the mid,
    # no ATM put — and the owner would rather not show them at all).
    # Walk each session's cap-ranked pool and keep the first TOP_N
    # names whose ATM straddle prices, backfilling past rank TOP_N
    # where needed, within a bounded fetch budget.
    def _rank(syms: list[str]) -> tuple[list[EarnRow], int]:
        # Confirmed sessions only (owner call 2026-08-27, extending the
        # priced-move rule): a blank/dmh Finnhub hour meant the row
        # rendered under AFTER CLOSE with a * flag, which was honest but
        # noisy — 40-60% of raw rows lack an hour, skewing small-cap.
        # The owner would rather not show them. The unconfirmed names
        # still count toward "+N more", so the sheet stays honest about
        # what it is not showing.
        ranked = sorted(
            (s for s in syms if confirmed.get(s)),
            key=lambda s: -(caps.get(s, {}).get("cap") or 0)
        )
        kept = _select_priced(ranked, date_iso)
        if not kept and ranked:
            # WHOLESALE failure — Yahoo down, module broken (the
            # 2026-08-27 numpy/LD_LIBRARY_PATH incident), or a session
            # of pure micro-caps. An empty earnings column under a
            # "Before Open" band claims nobody reports tomorrow, which
            # is a lie; the old dash rows only claimed we couldn't
            # price them. Degrade to the dashes, never to the lie.
            log.warning(
                f"calendar: 0/{len(ranked)} names priced an implied "
                f"move — falling back to unpriced top-{TOP_N} rows")
            kept = [(s, None) for s in ranked[:TOP_N]]
        rows = [
            EarnRow(
                symbol=s,
                name=str(caps.get(s, {}).get("name") or s),
                cap_musd=float(caps.get(s, {}).get("cap") or 0),
                session_confirmed=confirmed.get(s, False),
                implied_move=mv,
            )
            for s, mv in kept
        ]
        return rows, max(0, len(syms) - len(rows))

    day.bmo, day.dropped_bmo = _rank(bmo_syms)
    day.amc, day.dropped_amc = _rank(amc_syms)

    # Logos for the names that actually made the sheet. Resolved AFTER
    # selection so an excluded name costs no artwork fetch. Cache-first
    # and best-effort: a sheet with no logos is fine, a sheet that
    # failed to render is not.
    try:
        shown = [r.symbol for r in day.bmo + day.amc]
        logos = _resolve_logos(shown, caps)
        for r in day.bmo + day.amc:
            r.logo = logos.get(r.symbol) or b""
    except Exception as e:
        log.warning(f"calendar: logos unavailable ({e})")
    return day
