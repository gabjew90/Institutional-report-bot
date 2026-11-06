"""The answer body for a price or company-data question, written in code (2026-11-05).

Audit run 8 (2026-11-05) found the price shape stating figures the turn's
data held, under the wrong label: "Total cash and equivalents $8.3B" for
Qualcomm, where $8.3B is Yahoo's totalCash (cash, equivalents and
marketable securities; cash alone was $4.53B). The model wrote the label,
and no check can tell a right number under a wrong name from a right one.

So, as for the earnings date (discord_bot/earnings_block.py), the lines
are rendered here and the model writes only the opening:

1. one line per quote, in the session's own terms (live, after hours,
   pre-market, last close, or 24 hours for a coin);
2. for one stock, the company-data lines the question asks for (market
   cap, shares and short interest, cash and debt with cash flow,
   valuation), taken from the snapshot's own labelled lines, or a market
   cap and 52-week line when it asks for none.

A quote that failed, a volume question, or a second ask in the question
gets no block: the model's answer covers what these lines cannot.
"""
from __future__ import annotations

import re

_CRYPTO_SOURCES = ("binance", "coingecko")
# Yahoo's caret symbols under the names the room uses
_INDEX_NAMES = {"^GSPC": "SPX", "^NDX": "NDX", "^VIX": "VIX", "^DJI": "Dow", "^RUT": "Russell 2000",
                "^IXIC": "Nasdaq Composite", "^TNX": "10-year Treasury yield",
                "^FVX": "5-year Treasury yield", "^TYX": "30-year Treasury yield",
                "^IRX": "3-month Treasury yield"}

# question words -> the snapshot render() line that answers them
_FIELDS = (
    (re.compile(r"\bmarket\s+cap", re.I), "market cap"),
    (re.compile(r"\bshares\s+outstanding|\bshare\s+count|\bfloat\b|\bshort\s+interest|\bhow\s+many\s+shares",
                re.I), "shares"),
    (re.compile(r"\bcash\b|\bdebts?\b|\bfcf\b|\bcapex\b|\bcapital\s+(?:spending|expenditures?)|\bbalance\s+sheet",
                re.I), "balance sheet"),
)
_FUNDAMENTALS_RE = re.compile(r"\bfundamentals?\b", re.I)


def quote_line(q: dict, session: str = "", with_symbol: bool = True) -> str:
    """One quote as a line, '' when it carries no price."""
    if q.get("price") is None:
        return ""
    p, chg = float(q["price"]), q.get("change_pct")
    sym = str(q.get("symbol") or "")
    who = f"**{_INDEX_NAMES.get(sym, sym)}** " if with_symbol else ""
    fresh = q.get("data_freshness") or ""
    if q.get("yield_pct") is not None:
        bps = q.get("change_bps")
        return f"→ {who}at **{float(q['yield_pct']):.2f}%**" + (f", {bps:+.1f} bps" if bps is not None else "") + "."
    if sym.startswith("^"):
        # an index level is points, not dollars
        return f"→ {who}at **{p:,.2f}**" + (f", {chg:+.1f}% today" if chg is not None else "") + "."
    if q.get("source") in _CRYPTO_SOURCES:
        return f"→ {who}**${p:,.2f}**" + (f", {chg:+.1f}% over 24 hours" if chg is not None else "") + "."
    if fresh == "live_extended_hours":
        if session == "PRE-MARKET":
            return (f"→ {who}**${p:,.2f}** pre-market"
                    + (f", {chg:+.1f}% from the last close" if chg is not None else "") + ".")
        close, ext = q.get("regular_session_close"), q.get("extended_hours_change_pct")
        if close is not None:
            day = f" ({chg:+.1f}% on the day)" if chg is not None else ""
            ah = f", {ext:+.1f}%" if ext is not None else ""
            lead = f"{who}closed" if who else "Closed"
            return f"→ {lead} at **${float(close):,.2f}**{day}, **${p:,.2f}** after hours{ah}."
    if fresh in ("regular_session_close", "last_print"):
        lead = f"{who}last close" if who else "Last close"
        return f"→ {lead} **${p:,.2f}**" + (f" ({chg:+.1f}% that session)" if chg is not None else "") + "."
    lead = f"{who}at" if who else "Trading at"
    return f"→ {lead} **${p:,.2f}**" + (f", {chg:+.1f}% today" if chg is not None else "") + "."


def _snapshot_lines(snapshot: dict) -> dict:
    """The snapshot's labelled render() lines, keyed by label."""
    from report.ticker_snapshot import render
    out = {}
    for ln in render(snapshot).split("\n"):
        label, sep, rest = ln.partition(": ")
        if sep:
            out[re.sub(r"\s*\(.*\)$", "", label).strip()] = rest.strip()
    return out


def _as_line(label: str, rest: str) -> str:
    return f"→ **{label[:1].upper() + label[1:]}:** {rest}" + ("" if rest.endswith(".") else ".")


def block_lines(price: dict, snapshot: dict | None = None, question: str = "") -> list[str]:
    """The lines for a price or company-data question, [] when the model
    should answer it instead."""
    from discord_bot.earnings_block import _SECOND_ASK_RE
    q = question or ""
    if _SECOND_ASK_RE.search(q):
        return []
    quotes = (price or {}).get("quotes") or []
    if not quotes or any(x.get("price") is None for x in quotes) or any("volume" in x for x in quotes):
        return []
    session = str(price.get("session") or "")
    lines = [quote_line(x, session) for x in quotes]
    if any(not ln for ln in lines):
        return []
    from discord_bot.ask_router import _VALUATION
    asked = [label for rx, label in _FIELDS if rx.search(q)]
    if re.search(_VALUATION, q, re.I):
        asked.append("valuation")
    if _FUNDAMENTALS_RE.search(q):
        asked = ["market cap", "shares", "balance sheet", "valuation"]
    snap = snapshot if (snapshot or {}).get("status") == "ok" else None
    if len(quotes) != 1 or not snap:
        # company data is fetched for one stock only: "SpaceX vs Tesla
        # market cap" is the model's
        return [] if asked else lines
    have = _snapshot_lines(snap)
    if asked:
        missing = [a for a in asked if a not in have]
        if missing:
            return []       # the snapshot lacks it: the model says so, or searches
        return lines + [_as_line(a, have[a]) for a in asked]
    # a plain price question: where it sits, beside what it is worth
    ctx = []
    if snap.get("market_cap"):
        ctx.append(f"market cap {have.get('market cap', '')}".strip())
    hi, live = snap.get("week52_high"), quotes[0].get("price")
    if hi and live is not None and float(live) <= hi:
        # from the live quote above, not the snapshot's cached price
        ctx.append(f"{(hi - float(live)) / hi * 100:.1f}% below its 52-week high of ${hi:,.2f}")
    if ctx:
        lines.append("→ " + (", ".join(ctx))[:1].upper() + (", ".join(ctx))[1:] + ".")
    return lines
