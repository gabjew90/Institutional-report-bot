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
TOP_N = 20


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
    session_confirmed: bool  # False = Finnhub hour was blank/dmh —
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
    """Cache-first market caps: hit symbol_market_cap (7-day TTL), fetch
    only the misses from Finnhub (paced), upsert what came back. A
    failed lookup is stored as cap 0 so it gets the TTL too instead of
    re-fetching every night."""
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
    for r in raw:
        sym = (r.get("symbol") or "").strip()
        if not sym:
            continue
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

    def _rank(syms: list[str]) -> tuple[list[EarnRow], int]:
        ranked = sorted(
            syms, key=lambda s: -(caps.get(s, {}).get("cap") or 0)
        )
        rows = [
            EarnRow(
                symbol=s,
                name=str(caps.get(s, {}).get("name") or s),
                cap_musd=float(caps.get(s, {}).get("cap") or 0),
                session_confirmed=confirmed.get(s, False),
            )
            for s in ranked[:TOP_N]
        ]
        return rows, max(0, len(syms) - TOP_N)

    day.bmo, day.dropped_bmo = _rank(bmo_syms)
    day.amc, day.dropped_amc = _rank(amc_syms)
    return day
