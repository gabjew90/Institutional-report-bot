"""One code-built body for a turn about one stock (2026-11-13, owner: option 1).

Until now a single-stock answer was built one of four ways, chosen by the
route: a model opening above code lines (the date lookup), code lines above
model arrows (a print, on four shapes), a model-worded outline (the same
four shapes with no print), or nothing at all ("did AMAT beat", a reply to
an earlier answer). Each gap was a place where the model reworded figures a
feed carries, and each bad rewording got a check: the print guard, the
implied-move guard, the outline's PRICE, RESULT, UPCOMING, OPTIONS and
POSITIONING slots.

Here the lines the feeds carry are rendered for every such turn, chosen by
the stock's state and by what was asked, and posted as written. The model
writes arrows only for what no feed carries: the business line, the news,
an analyst's action, a desk's view (stock_outline.build_outline).

State, from the snapshot's report dates:

- print:   a report in the last three days with its EPS (ask_router.fresh_print).
           Lines: EPS against its estimate and a year earlier, revenue when a
           feed has it, the guidance clauses of the news lines.
- pending: the report's time has passed and Yahoo has not filled the
           figures. No result line: the model writes the result from the
           news lines.
- last:    an older report, when a date or reply question asks how it went.
- none:    the next report with its consensus.

What was asked decides the order and the extras: a date or result question
leads with the report and ends with the price, any other leads with the
price. An options question gets the straddle's move. Company data (market
cap, shares and short interest, balance sheet, valuation) is posted only
when the asker's words ask for it (owner, 2026-11-13: positioning only
when asked). A reply to an earlier answer is a narrow question, so it gets
the result lines after a print and otherwise only what its words ask for.

Not here: funds, indices and coins, several stocks in one question, the
price shape (price_block renders it whole) and price history.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

SHAPES = frozenset({"ticker_opinion", "options_chain", "news_event", "company_profile",
                    "earnings_date", "follow_up"})

# A reply asking when the company reports, and one asking how a report
# went. Stricter than the router's own patterns: a reply is routed here by
# what it points back at, so "when will it recover" and "what did you
# mean" must not be read as questions about the report.
_REPLY_DATE_RE = re.compile(
    r"\b(?:when|what\s+(?:time|day|date))\b.{0,40}\b(?:report\w*|earnings|print\w*)\b"
    r"|\bearnings\s+date\b|\bnext\s+(?:earnings|report|print)\b", re.I)
_REPLY_RESULT_RE = re.compile(
    r"\b(?:beat|beats|miss|missed|results?|earnings|eps|revenue|guid(?:e|ed|ance)|numbers|print(?:ed)?"
    r"|last\s+quarter)\b", re.I)


def _ok(result) -> dict | None:
    return result if isinstance(result, dict) and result.get("status") == "ok" else None


def _news_lines(news: dict | None) -> list[str]:
    return [ln for ln in ((_ok(news) or {}).get("digest") or "").splitlines() if ln.strip()]


def price_line(price: dict | None, snapshot: dict | None, symbol: str,
               named: bool = True) -> tuple[str, float | None]:
    """(the quote as a line, the quoted price). The live quote when the
    route fetched one, else the snapshot's price with no move beside it
    (the options shape does not prefetch prices). `named` False is the
    line after others that already name the stock."""
    from discord_bot.price_block import quote_line
    p = _ok(price) or {}
    quotes = [q for q in p.get("quotes") or [] if q.get("price") is not None]
    q = next((q for q in quotes if str(q.get("symbol") or "").upper() == symbol), None)
    if q is None and len(quotes) == 1:
        q = {**quotes[0], "symbol": symbol}     # the feed's own spelling of the symbol
    if q is not None:
        return quote_line(q, str(p.get("session") or ""), with_symbol=named), float(q["price"])
    snap_price = (snapshot or {}).get("price")
    if snap_price is not None:
        who = f"**{symbol}** at" if named else "Trading at"
        return f"→ {who} **${float(snap_price):,.2f}**.", float(snap_price)
    return "", None


def options_line(chain: dict | None) -> str:
    """The straddle's move as one line, '' when the chain priced none and
    said nothing about its quotes."""
    c = _ok(chain)
    if not c:
        return ""
    s = c.get("summary") or {}
    note = c.get("quotes_note") or ""
    if s.get("implied_move_pct") is None:
        return "→ Options are not quoting right now, so there is no implied move to read." if note else ""
    dollars = f" (**${s['implied_move_dollars']:,.2f}**)" if s.get("implied_move_dollars") else ""
    try:
        d = date.fromisoformat(str(s.get("expiration_iso"))[:10])
        by = f" by the {d:%b} {d.day} expiration"
    except ValueError:
        by = ""
    stale = ""
    if not s.get("live_quotes", True) or "last live" in note:
        stale = f", on the last live quotes ({c.get('as_of')})"
    return (f"→ Options are pricing a move of about **{s['implied_move_pct']:.1f}%**{dollars} "
            f"either way{by}, from the at-the-money straddle{stale}.")


def upcoming_lines(symbol: str, edate: dict | None, snapshot: dict | None,
                   today: date | None = None) -> list[str]:
    """The next report, its consensus and the last report, each a line.
    From the earnings feed when the route fetched it (earnings_block: the
    date as the feed words it, the consensus from one source), else from
    the snapshot's own next report."""
    from discord_bot import earnings_block as eb
    if _ok(edate):
        try:
            return eb.block_lines(edate, snapshot)
        except Exception:
            return []
    growth = (snapshot or {}).get("growth") or {}
    nr = growth.get("next_report") or {}
    try:
        d = date.fromisoformat(str(nr.get("date"))[:10])
    except ValueError:
        return []
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    if d < today:
        return []
    session = f", {nr['session']}" if nr.get("session") else ""
    lines = [f"→ **{symbol}** reports {d:%a} {d:%b} {d.day}{session}."]
    c = eb.consensus({"date": nr.get("date")}, growth)
    parts = []
    if c["eps"] is not None:
        parts.append("EPS " + eb._with_growth(eb._eps(float(c["eps"])), c["eps_growth_pct"]))
    if c["revenue"] is not None:
        parts.append("revenue " + eb._with_growth(eb._money(float(c["revenue"])), c["revenue_growth_pct"]))
    if parts:
        lines.append("→ Consensus: " + " on ".join(parts) + ".")
    return lines


def company_lines(snapshot: dict | None, asked: str) -> list[str]:
    """The snapshot lines the asker's words ask for, as price_block reads
    them: market cap, shares and short interest, balance sheet, valuation."""
    from discord_bot import price_block as pb
    from discord_bot.ask_router import _VALUATION
    snap = _ok(snapshot)
    if not snap or not asked:
        return []
    want = [label for rx, label in pb._FIELDS if rx.search(asked)]
    if re.search(_VALUATION, asked, re.I):
        want.append("valuation")
    if pb._FUNDAMENTALS_RE.search(asked):
        want = ["market cap", "shares", "balance sheet", "valuation"]
    if not want:
        return []
    have = pb._snapshot_lines(snap)
    return [pb._as_line(a, have[a]) for a in want if a in have]


def _unnamed(line: str, symbol: str) -> str:
    """The line without the symbol it opens with, for a line that follows
    one already naming the stock."""
    for old, new in ((f"→ **{symbol}** reports ", "→ Reports "),
                     (f"→ **{symbol}** is expected to report ", "→ Expected to report "),
                     (f"→ **{symbol}** reported ", "→ Reported ")):
        if line.startswith(old):
            return new + line[len(old):]
    return line


def build(shape: str, symbol: str, results: dict, fresh: dict | None = None,
          asked: str = "", today: date | None = None) -> dict | None:
    """The posted lines for one stock turn, or None when the turn is not
    one (another shape, a fund or a coin) or there is nothing to post.
    `results` maps a prefetched tool name to its result. `asked` is the
    asker's own words (ask_router.asker_text). Plain data: the ask log
    serializes _ask_meta."""
    from discord_bot import ask_router as R
    from discord_bot import print_block as pb
    symbol = (symbol or "").upper()
    if shape not in SHAPES or not symbol or not R.is_stock(symbol):
        return None
    snap = _ok(results.get(R.T_SNAPSHOT))
    news_lines = _news_lines(results.get(R.T_NEWS))
    asked = asked or ""
    date_q = shape == "earnings_date"
    narrow = shape == "follow_up"
    result_q = bool((_REPLY_RESULT_RE if narrow else R._RESULT_Q_RE).search(asked))

    last = ((snap or {}).get("growth") or {}).get("last_report") or {}
    printed, state = None, "none"
    if fresh and fresh.get("actual") is not None and fresh.get("estimate") is not None:
        printed, state = fresh, "print"
    elif fresh:
        state = "pending"
    elif ((date_q or narrow) and result_q
          and last.get("actual") is not None and last.get("estimate") is not None):
        printed, state = {**last, "symbol": symbol}, "last"

    quote, quoted = price_line(results.get(R.T_PRICE), snap, symbol)
    sections: list[tuple[str, str]] = []      # (kind, line)

    def add(kind: str, lines) -> None:
        sections.extend((kind, ln) for ln in lines if ln)

    lead_with_price = bool(quote) and not date_q
    if lead_with_price and not (narrow and not printed):
        add("price", [quote])
    if printed:
        add("eps", [pb.eps_line(printed, "" if sections else symbol)])
        if printed.get("revenue"):
            add("revenue", [pb.revenue_line(printed, news_lines)])
        if state == "print":
            add("guidance", pb.guidance_lines(news_lines, since=str(printed.get("date") or "")))
    # The next report: always on a date question, on a view of a stock with
    # no print out, and on a reply only when its words ask when.
    want_upcoming = (date_q or (not narrow and state == "none")
                     or (narrow and state == "none" and bool(_REPLY_DATE_RE.search(asked))))
    if want_upcoming and state != "pending":
        up = upcoming_lines(symbol, results.get(R.T_EDATE), snap, today)
        if printed:
            up = [ln for ln in up if not ln.startswith("→ Last report")]
        if not date_q:
            # one line beside a view: the date with its consensus
            up = [ln for ln in up if not ln.startswith("→ Last report")]
            if len(up) >= 2:
                up = [up[0] + " " + up[1][2:]]
        if sections:
            up = [_unnamed(ln, symbol) for ln in up]
        add("upcoming", up)
    if shape == "options_chain":
        add("options", [options_line(results.get(R.T_CHAIN))])
    add("company", company_lines(snap, asked))
    if date_q and quote:
        # last on a date question. Alone when the feeds have no line for the
        # report (out but not filled, or no date listed): the outline then
        # asks the model for the rest from the news and the earnings block.
        add("price", [price_line(results.get(R.T_PRICE), snap, symbol, named=not sections)[0]])
    if not sections:
        return None
    return {"lines": [ln for _, ln in sections], "kinds": sorted({k for k, _ in sections}),
            "state": state, "symbol": symbol, "shape": shape,
            "has_price": any(k == "price" for k, _ in sections),
            "price": quoted if any(k == "price" for k, _ in sections) else None,
            "eps": float(printed["actual"]) if printed else None,
            "revenue": float(printed["revenue"]) if printed and printed.get("revenue") else None,
            "revenue_source": (printed or {}).get("revenue_source") or ""}


def _restates(part: str, body: dict, body_text: str) -> bool:
    """The arrow carries a headline figure the lines already give (the
    printed EPS, the revenue, the quoted price) and no figure of its own."""
    from discord_bot import print_block as pb
    from discord_bot.figure_provenance import unsourced_figures
    headline = any(body.get(k) is not None and pb._has_figure(part, body[k]) for k in ("eps", "price"))
    if not headline and body.get("revenue"):
        headline = pb._money(body["revenue"]) in part
    if not headline:
        return False
    _, missing = unsourced_figures(part, body_text)
    return not missing


def compose(answer: str, body: dict) -> tuple[str, int]:
    """(the posted lines followed by the model's arrows, how many of its
    arrows were dropped). An arrow is dropped when it only restates the
    lines: it repeats a headline figure with none of its own, or it says
    nothing the lines do not."""
    from discord_bot.answer_block import _NOT_A_LEAD, adds_nothing
    from discord_bot.data_footer import strip_model_sources
    from discord_bot.figure_provenance import _lines
    text = "\n\n".join(body["lines"])
    kept, dropped = [], 0
    for part in _lines(strip_model_sources(answer or "").strip()):
        part = part.strip()
        if not part or part.startswith(_NOT_A_LEAD):
            continue
        if _restates(part, body, text) or adds_nothing(part, text, ""):
            dropped += 1
            continue
        kept.append(part)
    return "\n\n".join([text, *kept]), dropped
