"""The consensus for a report, recorded before the report (2026-11-14, owner).

Yahoo lists the estimates for the quarter "to be reported next". Within
hours of a report that row moves on to the following quarter, and from
then no feed carries what the reported quarter was expected to earn. The
result lines (discord_bot/print_block.py) need the revenue estimate to say
whether revenue beat or missed. Until now it came from a news line when
one happened to state it, and two runs of the same AMAT question gave two
estimates ($10.33B "cited by StockStory", then $10.30B) on 2026-11-13.

So the consensus is stored while it is still the coming quarter's, keyed
by symbol and report date, and read back once the report is out:

- a daily job in the batch process records it for the companies reporting
  after today's close and over the next three days (`capture`), with the
  quarter after as well, which is what guidance is set against;
- /ask records it for any stock it reads the snapshot of within two weeks
  of its report (`from_growth`), so a name the room asked about before the
  report is covered whatever its size;
- after the report `needs` and `apply` put the stored revenue estimate on
  the snapshot's last report.

Nothing is recorded once the report's time has come: Yahoo's row may
already describe the next quarter.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
DAYS_AHEAD = 3            # a Friday run reaches Monday's reports
ASK_WINDOW_DAYS = 14      # /ask records a stock this close to its report
READ_BACK_DAYS = 120      # a stored estimate is read for a report this recent
MAX_NAMES = 150           # per run, the largest by expected revenue
MIN_REVENUE_EST = 50e6    # the calendar feed's quarterly revenue estimate
PAUSE_S = 0.3             # between companies, for Yahoo's sake
# A record is this report's when its EPS consensus is this close to the
# estimate the report was set against. Tight on purpose: the quarter after
# is often expected to earn within a tenth of this one (AMAT, $4.05 then
# $4.35), and a rejected record only costs the estimate, not a wrong line.
EPS_MATCH_ABS = 0.02
EPS_MATCH_REL = 0.03
RECORD_EVERY_S = 6 * 3600  # /ask records one stock's report at most this often
_US_SYMBOL_RE = re.compile(r"^[A-Z]{1,5}$")
_AFTER_CLOSE = ("after the close", "amc")
_CLOSE_MINUTE = 16 * 60


def capturable(report_iso: str, session: str = "", now: datetime | None = None) -> bool:
    """Whether the coming quarter's estimates still describe this report:
    its date is ahead, or it is today after the close and the close has
    not come."""
    try:
        d = date.fromisoformat(str(report_iso)[:10])
    except ValueError:
        return False
    et = (now or datetime.now(timezone.utc)).astimezone(ET)
    if d > et.date():
        return True
    return (d == et.date() and (session or "").lower() in _AFTER_CLOSE
            and et.hour * 60 + et.minute < _CLOSE_MINUTE)


def from_growth(symbol: str, growth: dict | None, now: datetime | None = None) -> dict | None:
    """The row to store from a snapshot's growth block, or None when there
    is no coming report close enough, or no estimate for it."""
    g = growth or {}
    nr, nq = g.get("next_report") or {}, g.get("next_quarter") or {}
    sym = (symbol or "").upper()
    if not sym or not nr.get("date") or (nq.get("eps") is None and nq.get("revenue") is None):
        return None
    if not capturable(nr["date"], nr.get("session") or "", now):
        return None
    et = (now or datetime.now(timezone.utc)).astimezone(ET)
    if date.fromisoformat(str(nr["date"])[:10]) - et.date() > timedelta(days=ASK_WINDOW_DAYS):
        return None
    return {"symbol": sym, "report_date": str(nr["date"])[:10],
            "eps": nq.get("eps"), "eps_year_ago": nq.get("eps_year_ago"),
            "revenue": nq.get("revenue"), "revenue_year_ago": nq.get("revenue_year_ago")}


_recorded: dict[tuple[str, str], float] = {}


def due(row: dict, clock: float | None = None) -> bool:
    """Whether /ask should store this row now: once per RECORD_EVERY_S for
    one stock's report, so a busy name does not write on every question."""
    key = (row["symbol"], row["report_date"])
    now = time.monotonic() if clock is None else clock
    last = _recorded.get(key)
    if last is not None and now - last < RECORD_EVERY_S:
        return False
    _recorded[key] = now
    return True


def needs(snapshot: dict | None, today: date | None = None) -> tuple[str, str] | None:
    """(symbol, report date) when the snapshot's last report has no revenue
    estimate of its own and is recent enough to look one up."""
    snap = snapshot or {}
    last = (snap.get("growth") or {}).get("last_report") or {}
    sym = str(snap.get("symbol") or "").upper()
    if not sym or not last.get("date") or last.get("revenue_estimate"):
        return None
    try:
        d = date.fromisoformat(str(last["date"])[:10])
    except ValueError:
        return None
    today = today or datetime.now(timezone.utc).astimezone(ET).date()
    if not timedelta(0) <= today - d <= timedelta(days=READ_BACK_DAYS):
        return None
    return sym, d.isoformat()


def apply(snapshot: dict, row: dict | None) -> dict:
    """The snapshot with the stored revenue estimate on its last report. A
    copy: the snapshot is cached and shared between turns."""
    est = (row or {}).get("revenue")
    last = ((snapshot or {}).get("growth") or {}).get("last_report")
    if not est or not last:
        return snapshot
    # The record must be for this report. Yahoo's report dates carry the EPS
    # estimate the result was set against, and the record carries the EPS
    # consensus taken with the revenue's: when the two disagree the record
    # was taken after Yahoo had moved on to the next quarter, or for a
    # report whose date was wrong, and its revenue figure is not this one's.
    rec_eps, rep_eps = row.get("eps"), last.get("estimate")
    if rec_eps is not None and rep_eps is not None and (
            abs(float(rec_eps) - float(rep_eps)) > max(EPS_MATCH_ABS, EPS_MATCH_REL * abs(float(rep_eps)))):
        return snapshot
    new_last = {**last, "revenue_estimate": float(est), "revenue_estimate_source": "recorded"}
    if row.get("revenue_year_ago") and not last.get("revenue_year_ago"):
        new_last["revenue_year_ago"] = float(row["revenue_year_ago"])
    return {**snapshot, "growth": {**snapshot["growth"], "last_report": new_last}}


def _yahoo_row(symbol: str) -> dict | None:
    """Yahoo's estimates for the quarter to be reported next and the one
    after, or None when it has neither figure for the coming quarter."""
    import yfinance as yf
    from report.ticker_snapshot import _num, _table
    t = yf.Ticker(symbol)
    eps, rev = _table(t.earnings_estimate), _table(t.revenue_estimate)
    e0, r0 = eps.get("0q") or {}, rev.get("0q") or {}
    e1, r1 = eps.get("+1q") or {}, rev.get("+1q") or {}
    row = {"eps": _num(e0.get("avg")), "eps_year_ago": _num(e0.get("yearAgoEps")),
           "revenue": _num(r0.get("avg")), "revenue_year_ago": _num(r0.get("yearAgoRevenue")),
           "next_eps": _num(e1.get("avg")), "next_revenue": _num(r1.get("avg"))}
    if row["eps"] is None and row["revenue"] is None:
        return None
    return row


def names_for(rows: list[dict] | None, report_iso: str, now: datetime | None = None) -> list[str]:
    """The calendar feed's rows for one date as symbols worth recording:
    US-listed, not yet reported, largest expected revenue first."""
    keep = []
    for r in rows or []:
        sym = str(r.get("symbol") or "").upper()
        est = r.get("revenueEstimate") or 0
        if not _US_SYMBOL_RE.match(sym) or est < MIN_REVENUE_EST:
            continue
        if not capturable(report_iso, str(r.get("hour") or ""), now):
            continue
        keep.append((est, sym))
    return [s for _, s in sorted(keep, reverse=True)]


def capture(now: datetime | None = None, fetch_rows=None, yahoo=None, store=None,
            pause_s: float = PAUSE_S) -> dict:
    """Record the consensus for the companies reporting after today's close
    and over the next DAYS_AHEAD days. Returns {dates, names, stored,
    failed, feed_down}. The three callables are the calendar feed, the
    Yahoo read and the database write, passed in by tests."""
    if fetch_rows is None:
        from report.news_data import fetch_earnings_calendar_all as fetch_rows
    yahoo = yahoo or _yahoo_row
    if store is None:
        import db
        store = db.upsert_report_consensus
    et = (now or datetime.now(timezone.utc)).astimezone(ET)
    out = {"dates": [], "names": 0, "stored": 0, "failed": 0, "feed_down": []}
    todo: list[tuple[str, str]] = []
    for offset in range(DAYS_AHEAD + 1):
        iso = (et.date() + timedelta(days=offset)).isoformat()
        rows = fetch_rows(iso)
        if rows is None:
            out["feed_down"].append(iso)
            continue
        out["dates"].append(iso)
        todo.extend((sym, iso) for sym in names_for(rows, iso, now))
    seen: set[str] = set()
    for sym, iso in todo:
        if sym in seen or len(seen) >= MAX_NAMES:
            continue
        seen.add(sym)
        try:
            row = yahoo(sym)
        except Exception as e:
            log.info(f"report consensus {sym}: Yahoo read failed ({e})")
            row = None
        if not row:
            out["failed"] += 1
        else:
            store(symbol=sym, report_date=iso, **row)
            out["stored"] += 1
        if pause_s:
            time.sleep(pause_s)
    out["names"] = len(seen)
    return out
