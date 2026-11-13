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

Shorter after a print (2026-11-12, owner: "did amat earnings droo" was
answered in 150 words and six arrows, 19 minutes after the report). The
result arrow was one 73-word sentence holding the results, the guidance and
an unrelated rating. Two desk arrows came from notes that said only that the
company reports on Thursday ("JPMorgan flagged the print on its afternoon
briefing calendar"). A positioning arrow answered nothing asked, and the
news line's "shares moving lower" sat beside a live quote of +0.6% after
hours. Now: a desk gets a slot only when its note carries a view
(`_has_view`). A note that only lists the company among the week's reports
never does, and a note written before a print that is now out gets none
(owner, same day: what a desk expected is not the answer once the result
is in; a note written after the report still gets its slot, and so does a
preview when the asker asks about the desks, `_asks_about_desks`). With a
print in the last days
the news lines are split into RESULT, GUIDANCE and ANALYST ACTION slots, the
news's own account of the stock's move is cut when a live quote is in the
outline, and there is no POSITIONING slot.
"""
from __future__ import annotations

import re

from discord_bot.news_tool import _DATED_RE, _PUBLISHER_RE

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


# What a clause of a news line is about. An analyst's action is checked
# first: "raised its target and expects stronger demand" is the analyst's.
_ANALYST_RE = re.compile(
    r"\b(?:rating|price target|upgrad\w+|downgrad\w+|initiat\w+ coverage|"
    r"(?:rais|cut|lower|lift|set|hik|trim|boost|slash)\w*\s+(?:(?:its|the|their)\s+)?(?:price\s+)?target)\b", re.I)
_GUIDE_RE = re.compile(r"\b(?:guid(?:ed|es|ing|ance)|outlook|forecast(?:s|ed|ing)?)\b", re.I)
_REPORTED_RE = re.compile(r"\b(?:reported|posted|delivered)\b", re.I)
# Where a line turns from the results to the guidance.
_CLAUSE_SPLIT_RE = re.compile(
    r";|,\s+(?:and\s+|while\s+)?(?=(?:the company\s+|it\s+|management\s+)?"
    r"(?:guided|guides|guiding|issued guidance|forecast)\b)", re.I)
# The news's own account of the stock's move, at the end of a clause
# ("..., with shares moving lower following the release"). The live quote
# is the move: the two disagreed in the AMAT answer.
_MOVE_TAIL_RE = re.compile(
    r"(?:^\s*|,\s*(?:(?:with|and|as|while|though|although|but)\s+)?|"
    r"\s+(?:with|and|as|while|though|although|but)\s+)(?:the\s+|its\s+)?(?:shares?|stock)\s+"
    r"(?:(?:is|are|was|were|has|have|had)\s+)?"
    r"(?:mov|fell|fall|ris|rose|dropp|drop|slid|slip|jump|gain|climb|declin|sank|sink|tumbl|surg|"
    r"trad|dipp|dip|down|up\b|lower|higher|plung|soar|rall|pop|tick|edg)[^;|]*$", re.I)


def _split_news(lines: list[str], cut_moves: bool, since: str = "") -> dict[str, list[str]]:
    """News lines as clauses sorted by subject: the results, the guidance,
    an analyst's action. A line is split at its semicolons, and each clause
    keeps the publisher the line ended with. An analyst line dated before
    `since` (the report date) is left out: it is not a reaction to it."""
    out: dict[str, list[str]] = {"result": [], "guidance": [], "analyst": []}
    for raw in lines:
        m = _DATED_RE.match(raw)
        date = m.group(1) if m else ""
        body = (raw[m.end():] if m else raw).strip().lstrip(")*:\u2013-\u2022 ")
        pub = ""
        pm = _PUBLISHER_RE.search(body)
        if pm:
            pub, body = f" ({pm.group(1)})", body[:pm.start()]
        if _ANALYST_RE.search(body) and not _GUIDE_RE.search(body) and not _REPORTED_RE.search(body):
            if since and date and date < since[:10]:
                continue
            # a line about a rating stays whole, with its date
            out["analyst"].append(f"{body.strip().rstrip('.')}, {date}{pub}" if date
                                  else f"{body.strip().rstrip('.')}{pub}")
            continue
        # written as the sign, so the model copies it and does not convert it
        # ("plus or minus $0.20" came back as "±$20¢", 2026-11-12)
        body = re.sub(r"plus or minus\s+(?=\$?\d)", "±", body, flags=re.I)
        for clause in _CLAUSE_SPLIT_RE.split(body):
            if cut_moves:
                clause = _MOVE_TAIL_RE.sub("", clause)
            clause = clause.strip().rstrip(".,")
            if len(clause.split()) < 3:
                continue
            kind = ("analyst" if _ANALYST_RE.search(clause) and not _REPORTED_RE.search(clause) else
                    "guidance" if _GUIDE_RE.search(clause) else "result")
            out[kind].append(f"{clause}{pub}")
    return out


def _result_slots(fresh: dict | None, snapshot: dict | None, news: dict | None,
                  have_price: bool = False) -> tuple[list[str], str | None]:
    """The print if one came out in the last few days (the results, then
    the guidance, each its own slot), else the next report with what the
    consensus expects. Second value: an analyst's action from the news
    after a print, which the outline places with the desks."""
    g = (snapshot or {}).get("growth") or {}
    news_lines = []
    if (news or {}).get("status") == "ok":
        news_lines = [ln for ln in (news.get("digest") or "").splitlines() if ln.strip()][:MAX_NEWS_LINES]
    if fresh:
        when = f"{fresh['date']} {fresh.get('session') or ''}".strip()
        parts = _split_news(news_lines, cut_moves=have_price, since=str(fresh.get("date") or ""))
        if fresh.get("actual") is not None and fresh.get("estimate") is not None:
            sp = fresh.get("surprise_pct")
            head = (f"RESULT: reported {when}: EPS {_eps(fresh['actual'])} vs {_eps(fresh['estimate'])} "
                    f"expected" + (f" ({sp:+.1f}%)" if sp is not None else "") + " [company report]")
        else:
            head = f"RESULT: reported {when}; the figures are in the news lines [news search]"
        if parts["result"]:
            named = any(_PUBLISHER_RE.search(p) for p in parts["result"])
            head += (". From the news" + (", name the publisher each line ends with" if named else "")
                     + ": " + " | ".join(parts["result"]))
        slots = [head]
        if parts["guidance"]:
            slots.append("GUIDANCE: " + " | ".join(parts["guidance"]) + " [news search]")
        analyst = ("ANALYST ACTION: " + " | ".join(parts["analyst"]) + " [news search]"
                   if parts["analyst"] else None)
        return slots, analyst
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
    return ([out] if out else []), None


def _driver_slot(primer: dict | None, fresh: dict | None = None) -> str | None:
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
    # After a print the result slots carry the figures. The list of what
    # the company is judged on then has nothing to attach to, and the
    # model padded the arrow with it ("with gross margin and core tool
    # orders setting the pace for operational profitability", 2026-11-12).
    if watched and not fresh:
        slot += f" What it is judged on: {watched}"
    return slot + " [company background; say it in one short clause tied to the result or the setup, no list of segments]"


def _desk_pieces(note: dict) -> list[str]:
    """The note's own sentences that `_desk_text` shows."""
    first_earnings = (note.get("earnings") or [""])[0]
    calls = note.get("calls") or []
    if calls:
        return [x for x in (calls[0].get("rationale") or "", first_earnings) if x]
    for key in ("earnings", "insights"):
        if note.get(key):
            return [note[key][0]]
    return []


def _desk_text(note: dict) -> str:
    """The one line that best states this desk's view on the name: a call
    with its rationale, else an earnings line, else an insight."""
    first_earnings = (note.get("earnings") or [""])[0]
    for c in note.get("calls") or []:
        bits = [(c.get("action") or "").replace("_", " "), c.get("rating"),
                f"price target {c['price_target']}" if c.get("price_target") and str(c["price_target"]).upper() != "N/A" else None]
        head = ", ".join(b for b in bits if b and b != "N/A")
        text = f"{head}: {c.get('rationale') or ''}".strip(": ")
        if c.get("call_by"):
            # the note relays another broker's action (2026-10-26 audit A21)
            text = f"reports {c['call_by']}'s call, not its own: {text}"
        # the desk's own numbers live in its earnings lines, not the call
        if first_earnings and first_earnings not in text:
            text += f". Its figures: {first_earnings}"
        return text
    for key in ("earnings", "insights"):
        lines = note.get(key) or []
        if lines:
            return lines[0]
    return ""


_RATING_ACTIONS = {"upgrade", "downgrade", "initiate", "price_target_change"}
# A figure of the desk's own: money, a percentage, a multiple, basis points.
# A date ("11/12"), a year or a quarter ("4Q") is not one.
_FIGURE_RE = re.compile(
    r"\$\s?\d|\d\s?(?:%|percent\b)|\b\d+(?:\.\d+)?\s?x\b|"
    r"\b\d+(?:\.\d+)?\s?(?:bps?|basis points?|billion|million|bn|mn)\b|\b\d+(?:\.\d+)?[BM]\b", re.I)


# A sentence that only puts the company on the calendar.
_SCHEDULE_RE = re.compile(
    r"\breport(?:s|ing)?\s+(?:earnings|results|on\b|after\b|before\b|this\b|next\b|tonight|today|"
    r"tomorrow|mon|tue|wed|thu|fri|\d|(?:fiscal\s+)?(?:Q\d|\dQ|first|second|third|fourth))|"
    r"\b(?:due|set|scheduled|slated) to report\b|"
    r"\bearnings\s+(?:on|due|after|before|this|next|tonight|today|tomorrow)\b|"
    r"\bon (?:the|its) (?:earnings )?calendar\b", re.I)
_VIEW_WORD_RE = re.compile(
    r"\b(?:expect|sees?\b|prefer|upside|downside|above|below|beat|miss|ahead of|strong|weak|risk|"
    r"bull|bear|favou?r|likes?\b|cautious|constructive|rais|cut|guid|margin|demand|growth|"
    r"share gain|pricing|estimate|consensus|street)", re.I)


def _is_schedule_only(pieces: list[str]) -> bool:
    """Every sentence only says when the company reports."""
    sentences = [x for p in pieces for x in re.split(r"(?<=[.!?])\s+", p or "") if len(x.split()) >= 2]
    return bool(sentences) and all(
        _SCHEDULE_RE.search(x) and not _VIEW_WORD_RE.search(x) and not _FIGURE_RE.search(x)
        for x in sentences)


def _has_view(note: dict, pieces: list[str]) -> bool:
    """Whether a note says something about the name worth its own arrow.
    Always: a price target, a rating action, a call the desk holds with
    high conviction, or a figure of its own. Otherwise a note that only
    lists the company among the week's reports ("AMAT and CSCO report
    earnings 11/12", "Neutral: reporting after the close on Thursday") has
    none."""
    for c in (note.get("calls") or [])[:1]:          # the call `_desk_text` shows
        pt = str(c.get("price_target") or "")
        if pt and pt.upper() != "N/A":
            return True
        if (c.get("action") or "").lower() in _RATING_ACTIONS:
            return True
        if (c.get("conviction") or "").lower() == "high" and (c.get("rationale") or "").strip():
            return True
    if any(_FIGURE_RE.search(p or "") for p in pieces):
        return True
    return not _is_schedule_only(pieces)


_DESK_WORDS_RE = re.compile(
    r"\b(?:desks?|analysts?|banks?|brokers?|street|sell[- ]?side|expect\w*|previews?|"
    r"price targets?|targets?|pts?|ratings?|rated|upgrad\w*|downgrad\w*|gs|jpm|bofa)\b", re.I)


def _asks_about_desks(asked: str, research: dict | None) -> bool:
    """The asker's own words ask what the desks said or expected, or name
    one of the banks in the research ("what did goldman expect")."""
    text = (asked or "").lower()
    if not text:
        return False
    if _DESK_WORDS_RE.search(text):
        return True
    for n in (research or {}).get("notes") or []:
        first = str(n.get("source") or "").lower().split(" ")[0]
        if len(first) >= 3 and first != "the" and re.search(rf"\b{re.escape(first)}\b", text):
            return True
    return False


def _desk_slots(research: dict | None, fresh: dict | None, asked: str = "") -> list[str]:
    if (research or {}).get("status") != "ok":
        return []
    from discord_bot.ask_router import _is_preview
    keep_previews = bool(fresh) and _asks_about_desks(asked, research)
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
        preview = bool(fresh) and _is_preview(n, fresh)
        if preview and not keep_previews:
            continue                      # the result is out: what it expected is not the answer
        if not text or not _has_view(n, _desk_pieces(n)):
            continue                      # the bank's next note may have one
        tag = ", written before the report: what it expected" if preview else ""
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


def _today_news_slot(news: dict | None, today=None) -> str | None:
    """For "what happened to X": say when the news search found nothing
    dated today, so the answer says that instead of building a cause from
    week-old items (2026-10-18 audit A16: "what happened to cbrs" was
    answered with an Oct 7 director filing, read as a share purchase)."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    digest = (news or {}).get("digest") or ""
    iso = today.isoformat()
    from discord_bot.news_tool import _DATED_RE
    if any((m := _DATED_RE.match(ln)) and m.group(1) == iso for ln in digest.splitlines()):
        return None
    return ("NEWS TODAY: the news search found no company news dated today; say so plainly, "
            "then give the move against the market and the dated items as background only "
            "[news search]")


def build_outline(results: dict, shape: str, symbol: str, fresh: dict | None = None,
                  asked: str = "") -> str:
    """The outline block for a stock answer, or '' when the shape is not a
    single-stock view or there is nothing to outline. `results` maps a
    prefetched tool name to its result. `asked` is the asker's own words
    (ask_router.asker_text), read only to tell a question about the desks."""
    from discord_bot import ask_router as R
    if shape not in _STOCK_SHAPES or not symbol:
        return ""
    symbol = symbol.upper()
    price = _price_slot(results.get(R.T_PRICE), symbol, results.get(R.T_SNAPSHOT))
    lead, analyst = _result_slots(fresh, results.get(R.T_SNAPSHOT), results.get(R.T_NEWS),
                                  have_price=bool(price and "today" in price))
    slots = [
        price,
        *lead,
        _today_news_slot(results.get(R.T_NEWS)) if shape == "news_event" and not fresh else None,
        _driver_slot(results.get(R.T_PRIMER), fresh),
        analyst,
        *_desk_slots(results.get(R.T_RESEARCH), fresh, asked),
        _options_slot(results.get(R.T_CHAIN)) if shape == "options_chain" else None,
        # for the days after a print the answer is about the result
        None if fresh else _risk_slot(results.get(R.T_SNAPSHOT)),
    ]
    slots = [s for s in slots if s]
    if len(slots) < 2:
        return ""
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(slots, 1))
    last = ("into it. The stock's move comes from the PRICE slot only. " if fresh else
            "into it, and drop the POSITIONING slot if it does not bear on the question. ")
    return ("[ANSWER OUTLINE, built by the system from the blocks above. Write one arrow per "
            "numbered slot, in this order. Use the facts in that slot, and name only the source "
            "in its brackets, in words, never the bracket itself: a figure from the news or the "
            "company report is never a bank's. "
            "Say each in plain words, growth first where there is growth. The RESULT or "
            "UPCOMING slot keeps its own arrow and its own source; you may fold the PRICE slot "
            + last + "Add no facts that are not in the blocks above.]\n" + body)


# The source tags the outline puts in brackets. The model copied them into
# the first live answers ("+1.0% session-to-date [live prices]",
# "... [JPMorgan]"); they are for the model, not the reader. A feed named
# in lowercase ("$23.19 ... [finnhub]", 2026-10-31 harness) goes too: the
# Data footer names the feeds.
_FEED_TAGS = ("finnhub", "yahoo", "coingecko", "binance.us", "nasdaq", "google")
_SOURCE_TAG_RE = re.compile(
    r"\s*\[(?:live prices|company report|consensus data|company background[^\]\n]*|"
    r"exchange and filing data|options chain|news search|company and trading data|"
    + "|".join(re.escape(t) for t in _FEED_TAGS) + "|"
    r"the publisher[^\]\n]*|[A-Z][A-Za-z0-9.&' -]{1,40}; this view only|"
    r"[A-Z][A-Za-z0-9.&' -]{1,40})\](?!\()")


def strip_source_tags(answer: str, banks: set[str] | None = None) -> str:
    """Remove the outline's bracketed source tags from an answer. A bare
    '[Name]' is removed only when Name is one of the outline's banks, so
    other bracketed text survives; markdown links are never touched."""
    text = answer or ""

    def _drop(m):
        tag = m.group(0).strip()[1:-1]
        bare = ";" not in tag and tag not in _GENERIC_TAGS and not tag.startswith(
            ("company background", "the publisher"))
        if bare and tag not in (banks or set()):
            # a publisher the model named in the outline's bracket style
            # ("[Stock Titan]"): a real attribution, written as one, keeping
            # the whitespace it had; inside an open parenthesis just unbracket
            lead = m.group(0)[:len(m.group(0)) - len(m.group(0).lstrip())]
            before = text[:m.start()]
            inside = before.count("(") > before.count(")")
            return f"{lead}{tag}" if inside else f"{lead}({tag})"
        return ""
    text = _SOURCE_TAG_RE.sub(_drop, text)
    text = _PAREN_LABEL_RE.sub("", text)
    # Several tags in one bracket at the end of a line ("[live prices, ticker
    # snapshot]", "[Goldman Sachs, exchange data]"), 2026-10-18 live harness.
    text = _TAG_LIST_RE.sub("", text)
    # The outline's own labels joined in one bracket anywhere in a line
    # ("[company report and news search]", 2026-11-12 live harness).
    return _LABEL_JOIN_RE.sub("", text)


_TAG_LIST_RE = re.compile(
    r"[ \t]*\[(?:[A-Za-z][A-Za-z.&' -]{1,30})(?:,\s*[A-Za-z][A-Za-z.&' -]{1,30})+\](?=[ \t]*(?:\n|$))")


_LABELS = ("live prices", "company report", "consensus data", "exchange and filing data",
           "options chain", "news search", "company and trading data")
_GENERIC_TAGS = {*_LABELS, *_FEED_TAGS}
_ANY_LABEL = "(?:" + "|".join(re.escape(t) for t in (*_LABELS, "company background", *_FEED_TAGS)) + ")"
_LABEL_JOIN_RE = re.compile(
    r"[ \t]*\[" + _ANY_LABEL + r"(?:\s*(?:,|and|&|/|\+)\s*" + _ANY_LABEL + r")+\](?!\()", re.I)
# The outline's labels copied in parentheses ("... semiconductor industries
# (Company Background)", 2026-10-31 harness). Only the labels: a publisher
# in parentheses, "(GlobeNewswire)" or "(Yahoo)", is a real attribution.
_PAREN_LABEL_RE = re.compile(
    r"[ \t]*\((?:" + "|".join(re.escape(t) for t in (*_LABELS, "company background")) + r")\)",
    re.I)


def outline_banks(research: dict | None) -> set[str]:
    return {n.get("source") for n in (research or {}).get("notes") or [] if n.get("source")}


def outline_symbol(prefetch: list) -> str:
    """The symbol the route's stock prefetches are about."""
    from discord_bot import ask_router as R
    for tool, args in prefetch or []:
        if tool in (R.T_SNAPSHOT, R.T_RESEARCH) and (args or {}).get("symbol"):
            return str(args["symbol"]).upper()
    return ""
