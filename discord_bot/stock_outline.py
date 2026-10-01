"""The answer outline for a single-stock /ask question (2026-10-01).

Owner-approved redesign. Until now a stock question handed the model six
data blocks (bank notes, news, snapshot, primer, chain, price) and asked it
to compose four correct, attributed arrows; code then checked the result
and asked for rewrites. Every live sample found a new failure: the print
ignored for pre-print previews, the business line left out or bolted on,
a print attributed to a bank that never wrote it ("Goldman Sachs notes Q4
revenue printed $54.23B": it came from the news search), answers at 14 s
and seven model calls.

This module decides, in code, what the answer says and in what order, and
attaches each fact's true source. The model gets the outline as the last
block before it writes and words one arrow per slot. Attribution and
ordering are no longer the model's to get right; the rewrite checks stay
as backstops and should rarely fire.

Pure: build_outline reads the prefetch results the router already
fetched and returns text. No network, no model.
"""
from __future__ import annotations

MAX_DESKS = 2
MAX_NEWS_LINES = 2
_STOCK_SHAPES = {"ticker_opinion", "options_chain", "news_event", "company_profile"}


def _price_slot(price: dict | None, symbol: str, snapshot: dict | None = None) -> str | None:
    for q in (price or {}).get("quotes") or []:
        if str(q.get("symbol") or "").upper() != symbol or q.get("price") is None:
            continue
        line = f"{symbol} ${q['price']:,.2f}"
        if q.get("change_pct") is not None:
            line += f", {q['change_pct']:+.1f}% today"
        if q.get("extended_hours_change_pct") is not None:
            line += f" ({q['extended_hours_change_pct']:+.1f}% after the regular close)"
        return f"PRICE: {line} [live prices]"
    # the options shape does not prefetch prices; the snapshot has one
    if (snapshot or {}).get("price") is not None:
        return f"PRICE: {symbol} ${snapshot['price']:,.2f} [company and trading data]"
    return None


def _eps(v) -> str:
    return f"${v:,.2f}"


def _result_slot(fresh: dict | None, snapshot: dict | None, news: dict | None) -> str | None:
    """The print if one came out in the last few days, else the next
    report with what the consensus expects."""
    g = (snapshot or {}).get("growth") or {}
    news_lines = []
    if (news or {}).get("status") == "ok":
        news_lines = [ln for ln in (news.get("digest") or "").splitlines() if ln.strip()][:MAX_NEWS_LINES]
    if fresh:
        when = f"{fresh['date']} {fresh.get('session') or ''}".strip()
        if fresh.get("actual") is not None and fresh.get("estimate") is not None:
            sp = fresh.get("surprise_pct")
            head = (f"RESULT: reported {when}: EPS {_eps(fresh['actual'])} vs {_eps(fresh['estimate'])} "
                    f"expected" + (f" ({sp:+.1f}%)" if sp is not None else "") + " [company report]")
        else:
            head = f"RESULT: reported {when}; the figures are in the news lines [news search]"
        if news_lines:
            head += ". From the news, name the publisher each line ends with: " + " | ".join(news_lines)
        return head
    nq, nr = g.get("next_quarter") or {}, g.get("next_report") or {}
    bits = []
    if nr.get("date"):
        bits.append(f"next report {nr['date']} {nr.get('session') or ''}".strip())
    if nq.get("revenue_growth_pct") is not None:
        bits.append(f"consensus revenue {nq['revenue_growth_pct']:+.1f}% y/y")
    if nq.get("eps_growth_pct") is not None:
        bits.append(f"consensus EPS {nq['eps_growth_pct']:+.1f}% y/y ({_eps(nq['eps'])})")
    reports = g.get("eps_reports") or []
    if reports and reports[0].get("surprise_pct") is not None:
        r = reports[0]
        bits.append(f"last report {r['date']} beat by {r['surprise_pct']:+.1f}%"
                    if r["surprise_pct"] >= 0 else f"last report {r['date']} missed by {r['surprise_pct']:.1f}%")
    out = ("UPCOMING: " + ", ".join(bits) + " [consensus data]") if bits else None
    if news_lines:
        news_part = "NEWS: " + " | ".join(news_lines) + " [the publisher each line ends with]"
        out = f"{out}\n{news_part}" if out else news_part
    return out


def _driver_slot(primer: dict | None) -> str | None:
    if (primer or {}).get("status") != "ok":
        return None
    text = primer.get("primer") or ""
    drivers = next((ln.split(":", 1)[1].strip() for ln in text.splitlines()
                    if ln.upper().startswith("DRIVERS:")), "")
    watched = next((ln.split(":", 1)[1].strip() for ln in text.splitlines()
                    if ln.upper().startswith("WATCHED:")), "")
    if not drivers:
        return None
    slot = f"DRIVER: {drivers}"
    if watched:
        slot += f" What it is judged on: {watched}"
    return slot + " [company background; say it in one short clause tied to the result or the setup, no list of segments]"


def _desk_text(note: dict) -> str:
    """The one line that best states this desk's view on the name: a call
    with its rationale, else an earnings line, else an insight."""
    first_earnings = (note.get("earnings") or [""])[0]
    for c in note.get("calls") or []:
        bits = [(c.get("action") or "").replace("_", " "), c.get("rating"),
                f"price target {c['price_target']}" if c.get("price_target") and str(c["price_target"]).upper() != "N/A" else None]
        head = ", ".join(b for b in bits if b and b != "N/A")
        text = f"{head}: {c.get('rationale') or ''}".strip(": ")
        # the desk's own numbers live in its earnings lines, not the call
        if first_earnings and first_earnings not in text:
            text += f". Its figures: {first_earnings}"
        return text
    for key in ("earnings", "insights"):
        lines = note.get(key) or []
        if lines:
            return lines[0]
    return ""


def _desk_slots(research: dict | None, fresh: dict | None) -> list[str]:
    if (research or {}).get("status") != "ok":
        return []
    from discord_bot.ask_router import _is_preview
    # Calls first (a rating or target is a view), high conviction first,
    # newest first; one note per bank.
    notes = sorted(research.get("notes") or [], key=lambda n: str(n.get("published") or ""),
                   reverse=True)                     # newest first ...
    notes.sort(key=lambda n: (                       # ... then calls, high conviction first (stable)
        not n.get("calls"),
        not any((c.get("conviction") or "") == "high" for c in n.get("calls") or [])))
    out, banks = [], set()
    for n in notes:
        bank = n.get("source") or ""
        if not bank or bank in banks:
            continue
        text = _desk_text(n)
        if not text:
            continue
        tag = ", written before the report: what it expected" if fresh and _is_preview(n, fresh) else ""
        out.append(f"DESK: {bank} ({n.get('published')}{tag}): {text} [{bank}; this view only]")
        banks.add(bank)
        if len(out) >= MAX_DESKS:
            break
    return out


def _options_slot(chain: dict | None) -> str | None:
    if not chain:
        return None
    if chain.get("status") != "ok":
        return "OPTIONS: the options chain could not be read [options chain]"
    s = chain.get("summary") or {}
    note = chain.get("quotes_note") or ""
    if s.get("implied_move_pct") is not None:
        line = (f"OPTIONS: the at-the-money straddle prices about {s['implied_move_pct']:.1f}% "
                f"(${s.get('implied_move_dollars') or 0:,.2f}) either way by {s.get('expiration_iso')}")
        if not s.get("live_quotes", True) or "last live" in note:
            line += f" (last live quotes, {chain.get('as_of')})"
        return line + " [options chain]"
    if note:
        return "OPTIONS: options are not quoting right now, so no implied move [options chain]"
    return None


def _risk_slot(snapshot: dict | None) -> str | None:
    s = snapshot or {}
    if s.get("status") != "ok":
        return None
    bits = []
    if s.get("short_pct_float") is not None:
        bit = f"short interest {s['short_pct_float']}% of float"
        if s.get("short_interest_as_of"):
            bit += f" as of {s['short_interest_as_of']}"
        bits.append(bit)
    if s.get("pct_below_52w_high") is not None:
        bits.append(f"{s['pct_below_52w_high']}% below its 52-week high")
    offs = s.get("offering_filings_12m") or []
    if offs:
        bits.append(f"filed {offs[0]['form']} (share registration) on {offs[0]['date']}")
    return ("POSITIONING: " + ", ".join(bits) + " [exchange and filing data]") if bits else None


def build_outline(results: dict, shape: str, symbol: str, fresh: dict | None = None) -> str:
    """The outline block for a stock answer, or '' when the shape is not a
    single-stock view or there is nothing to outline. `results` maps a
    prefetched tool name to its result."""
    from discord_bot import ask_router as R
    if shape not in _STOCK_SHAPES or not symbol:
        return ""
    symbol = symbol.upper()
    slots = [
        _price_slot(results.get(R.T_PRICE), symbol, results.get(R.T_SNAPSHOT)),
        _result_slot(fresh, results.get(R.T_SNAPSHOT), results.get(R.T_NEWS)),
        _driver_slot(results.get(R.T_PRIMER)),
        *_desk_slots(results.get(R.T_RESEARCH), fresh),
        _options_slot(results.get(R.T_CHAIN)) if shape == "options_chain" else None,
        _risk_slot(results.get(R.T_SNAPSHOT)),
    ]
    slots = [s for s in slots if s]
    if len(slots) < 2:
        return ""
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(slots, 1))
    return ("[ANSWER OUTLINE, built by the system from the blocks above. Write one arrow per "
            "numbered slot, in this order. Use the facts in that slot, and name only the source "
            "in its brackets: a figure from the news or the company report is never a bank's. "
            "Say each in plain words, growth first where there is growth. The RESULT or "
            "UPCOMING slot keeps its own arrow and its own source; you may fold the PRICE slot "
            "into it, and drop the POSITIONING slot if it does not bear on the question. Add no "
            "facts that are not in the blocks above.]\n" + body)


def outline_symbol(prefetch: list) -> str:
    """The symbol the route's stock prefetches are about."""
    from discord_bot import ask_router as R
    for tool, args in prefetch or []:
        if tool in (R.T_SNAPSHOT, R.T_RESEARCH) and (args or {}).get("symbol"):
            return str(args["symbol"]).upper()
    return ""
