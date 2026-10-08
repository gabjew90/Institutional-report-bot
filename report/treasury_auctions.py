"""US Treasury auction results and schedule from TreasuryDirect (2026-10-08 audit).

On 2026-10-07 the room asked how the 10-year auction went two minutes after
it closed. The bot had no auction data, said results were "rolling across
terminal feeds", and gave a market yield of 5.34-5.35% that read as the
auction's. The auction cleared at a 5.300% high yield with 2.77 bids per
dollar offered, posted by TreasuryDirect at about 1:01 PM ET.

TreasuryDirect's public JSON (no key):
  /TA_WS/securities/auctioned?format=json&days=N   results
  /TA_WS/securities/upcoming?format=json            the schedule
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request

log = logging.getLogger(__name__)

BASE = "https://www.treasurydirect.gov/TA_WS/securities"
_UA = {"User-Agent": "Mozilla/5.0 (omnibeta research bot)"}


def _get(path: str, timeout: float = 8.0) -> list[dict]:
    try:
        req = urllib.request.Request(f"{BASE}/{path}", headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        log.info(f"treasury auctions: {path} failed: {e}")
        return []


_HISTORY: tuple[float, list[dict]] = (0.0, [])
_HISTORY_TTL_S = 600


def _history() -> list[dict]:
    """200 days of results for the previous-auction comparison, kept for
    ten minutes (it changes only when an auction closes)."""
    import time
    global _HISTORY
    if time.monotonic() - _HISTORY[0] < _HISTORY_TTL_S and _HISTORY[1]:
        return _HISTORY[1]
    rows = _get("auctioned?format=json&days=200")
    if rows:
        _HISTORY = (time.monotonic(), rows)
    return rows


def tenor(row: dict) -> str:
    """'10-Year' for a 10-year note even when reopened ('9-Year 10-Month');
    a bill keeps its own term ('13-Week')."""
    if (row.get("securityType") or "").lower() == "bill":
        return row.get("securityTerm") or ""
    base = row.get("term") or row.get("originalSecurityTerm") or row.get("securityTerm") or ""
    # an inflation-protected or floating-rate issue is its own series: a
    # 10-year TIPS at 2.65% is not the 10-year note's previous auction
    if (row.get("tips") or "").lower() == "yes":
        return f"{base} TIPS"
    if (row.get("floatingRate") or "").lower() == "yes":
        return f"{base} FRN"
    return base


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def summarize(row: dict) -> dict:
    total = _num(row.get("totalAccepted"))

    def share(k):
        v = _num(row.get(k))
        return round(v / total * 100, 1) if v is not None and total else None
    bill = (row.get("securityType") or "").lower() == "bill"
    return {
        "security": f"{tenor(row)} {row.get('securityType') or ''}".strip(),
        "reopening": (row.get("reopening") or "").lower() == "yes",
        "auction_date": (row.get("auctionDate") or "")[:10],
        "closing_time_et": row.get("closingTimeCompetitive") or "",
        "high_yield_pct": None if bill else _num(row.get("highYield")),
        "high_discount_rate_pct": _num(row.get("highDiscountRate")) if bill else None,
        "bid_to_cover": _num(row.get("bidToCoverRatio")),
        "offering_usd_bn": round(_num(row.get("offeringAmount")) / 1e9, 1)
        if _num(row.get("offeringAmount")) else None,
        "indirect_pct": share("indirectBidderAccepted"),
        "direct_pct": share("directBidderAccepted"),
        "dealer_pct": share("primaryDealerAccepted"),
        "cusip": row.get("cusip") or "",
    }


def _matches(row: dict, want: str) -> bool:
    if not want:
        return True
    w = re.sub(r"[^0-9a-z]", "", want.lower())
    t = re.sub(r"[^0-9a-z]", "", tenor(row).lower())
    # "10-Year" is the nominal note; TIPS and FRNs only when asked for
    for series in ("tips", "frn"):
        if t.endswith(series) and series not in w:
            return False
    return bool(w) and (w in t or t.startswith(w))


def lookup(term: str = "", days: int = 10) -> dict:
    """Recent results (each with the previous auction of the same tenor
    beside it) and the upcoming schedule, optionally for one tenor."""
    recent = [r for r in _get(f"auctioned?format=json&days={int(days)}") if _matches(r, term)]
    history = _history() if recent else []
    upcoming = [r for r in _get("upcoming?format=json") if _matches(r, term)]
    results = []
    for r in recent[:8]:
        s = summarize(r)
        prior = next((h for h in history
                      if tenor(h) == tenor(r) and (h.get("auctionDate") or "") < (r.get("auctionDate") or "")
                      and h.get("highYield" if s["high_yield_pct"] is not None else "highDiscountRate")),
                     None)
        if prior:
            p = summarize(prior)
            s["previous"] = {k: p[k] for k in ("auction_date", "high_yield_pct",
                                                "high_discount_rate_pct", "bid_to_cover", "indirect_pct")}
        results.append(s)
    return {
        "results": results,
        "upcoming": [{"security": f"{tenor(r)} {r.get('securityType') or ''}".strip(),
                      "auction_date": (r.get("auctionDate") or "")[:10],
                      "reopening": (r.get("reopening") or "").lower() == "yes",
                      "offering_usd_bn": round(_num(r.get("offeringAmount")) / 1e9, 1)
                      if _num(r.get("offeringAmount")) else None} for r in upcoming[:8]],
        "source": "TreasuryDirect",
        "note": ("high_yield_pct is where the auction cleared. Results post about one minute "
                 "after the competitive close (1:00 PM ET for notes and bonds). A tail or "
                 "stop-through needs the when-issued yield at 1:00 PM, which this feed does not "
                 "carry: do not state one."),
    }
