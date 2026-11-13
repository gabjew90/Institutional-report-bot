"""The answer body for "when does X report", written in code (2026-11-05).

Run 8 of the quality audit checked 30 random factual answers against
outside sources. Three of the nine errors were on this shape, all in
figures the turn's data already held correctly:

- AMD "revenue of $13.22B (+41.6% y/y)": $13.22B was the calendar feed's
  consensus, +41.6% the growth of Yahoo's $13.09B. Two sources in one
  figure; $13.22B would be +43.0%.
- Shopify "$3.82B (+31.7% y/y)": the same split.
- Main Street "Wednesday, November 5": the payload said Thu Nov 5.

The figure check could not catch the first two because it asks whether a
number appears anywhere in the turn's data, not whether it belongs to the
figure beside it. So the lines are rendered here and the model writes only
the opening (discord_bot/answer_block.py):

1. the date as `_relative_day_et` computed it, weekday included, and
   whether both feeds list it;
2. the consensus from ONE source, with growth computed here against that
   same source's year-ago quarter: Yahoo's row when Yahoo lists the same
   report date, else the calendar feed's figures with no growth (its
   year-ago quarter is unknown);
3. the last report against its estimate;
4. the price.

Since 2026-11-13 the lines are placed by discord_bot/stock_body.py, which posts
them on every date question: a result question gets the report's own lines
above these, and an options or second ask gets the model's arrows below them.
"""
from __future__ import annotations

import re

# A second ask after the date ("when does NVDA report and how much has it
# run") needs figures these lines do not carry.
_SECOND_ASK_RE = re.compile(r"\b(?:and|also|plus)\b.{0,60}?\b(?:how|what|why|should|is|are|will|would|can)\b", re.I)


def _money(v: float) -> str:
    a, sign = abs(v), "-" if v < 0 else ""
    for div, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if a >= div:
            x = a / div
            return f"{sign}${x:.2f}{unit}" if x < 100 else f"{sign}${x:.1f}{unit}"
    return f"{sign}${a:,.0f}"


def _eps(v: float) -> str:
    return f"-${abs(v):.2f}" if v < 0 else f"${v:.2f}"


def _growth(now, ago) -> float | None:
    """Percent change from a positive year-ago figure; a loss or zero has no rate."""
    try:
        now, ago = float(now), float(ago)
    except (TypeError, ValueError):
        return None
    if ago <= 0:
        return None
    return (now / ago - 1) * 100


def _with_growth(fig: str, pct: float | None) -> str:
    return f"**{fig}** ({pct:+.1f}% y/y)" if pct is not None else f"**{fig}**"


def consensus(nxt: dict, growth: dict) -> dict:
    """{eps, revenue, eps_growth_pct, revenue_growth_pct, source} from one
    source. Yahoo's row is used when its next report date is the
    calendar's, since only then is it the same quarter."""
    yahoo_date = str(((growth or {}).get("next_report") or {}).get("date") or "")[:10]
    row = (growth or {}).get("next_quarter") or {}
    if yahoo_date and yahoo_date == str(nxt.get("date") or "")[:10] and (
            row.get("eps") is not None or row.get("revenue") is not None):
        return {"eps": row.get("eps"), "revenue": row.get("revenue"),
                "eps_growth_pct": _growth(row.get("eps"), row.get("eps_year_ago")),
                "revenue_growth_pct": _growth(row.get("revenue"), row.get("revenue_year_ago")),
                "source": "yahoo"}
    return {"eps": nxt.get("eps_estimate"), "revenue": nxt.get("revenue_estimate"),
            "eps_growth_pct": None, "revenue_growth_pct": None, "source": "calendar"}


def price_line(price: dict | None, symbol: str) -> str:
    """The quote for `symbol` in the session's own terms, '' without one."""
    if not isinstance(price, dict) or price.get("status") != "ok":
        return ""
    q = next((q for q in price.get("quotes") or []
              if str(q.get("symbol") or "").upper() == symbol and q.get("price") is not None), None)
    if not q:
        return ""
    from discord_bot.price_block import quote_line
    return quote_line(q, str(price.get("session") or ""), with_symbol=False)


def block_lines(edate: dict, snapshot: dict | None = None, price: dict | None = None) -> list[str]:
    """The lines for one company's next report, [] when the feed has no
    coming report to give (none listed, or one already released)."""
    nxt = (edate or {}).get("next")
    when = str((nxt or {}).get("when") or "").replace(", timing TBD", ", time not announced")
    if not isinstance(nxt, dict) or not nxt.get("date") or not when:
        return []
    if "already released" in when or "ago" in when or when.startswith("yesterday"):
        return []
    sym = str(edate.get("symbol") or "").upper()
    if not sym:
        return []
    status = str(nxt.get("date_status") or "")
    if status.startswith("projected"):
        lines = [f"→ **{sym}** is expected to report {when}. The date is projected, not confirmed."]
    else:
        lines = [f"→ **{sym}** reports {when}."]
    growth = ((snapshot or {}).get("growth") or {}) if (snapshot or {}).get("status") == "ok" else {}
    c = consensus(nxt, growth)
    parts = []
    if c["eps"] is not None:
        parts.append("EPS " + _with_growth(_eps(float(c["eps"])), c["eps_growth_pct"]))
    if c["revenue"] is not None:
        parts.append("revenue " + _with_growth(_money(float(c["revenue"])), c["revenue_growth_pct"]))
    if parts:
        lines.append("→ Consensus: " + " on ".join(parts) + ".")
    last = growth.get("last_report") or {}
    if last.get("actual") is not None and last.get("estimate") is not None and last.get("date"):
        from datetime import date as _date
        try:
            d = _date.fromisoformat(str(last["date"])[:10])
            day = f"{d.strftime('%b')} {d.day}"
        except ValueError:
            day = str(last["date"])[:10]
        a, e = float(last["actual"]), float(last["estimate"])
        verb = "beat" if a > e else "missed" if a < e else "matched"
        lines.append(f"→ Last report ({day}): EPS **{_eps(a)}**, {verb} the **{_eps(e)}** estimate.")
    pl = price_line(price, sym)
    if pl:
        lines.append(pl)
    return lines
