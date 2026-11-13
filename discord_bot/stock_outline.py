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

Figures out of the outline (2026-11-13, owner: option 1). The PRICE, RESULT,
UPCOMING, OPTIONS and POSITIONING slots handed the model figures a feed
carries and asked it to word them. Those lines are now rendered and posted
by code for every turn about one stock (discord_bot/stock_body.py), and the
outline lists only what no feed carries: the news, the business line, an
analyst's action, a desk's view. One header for every state.
"""
from __future__ import annotations

import re

from discord_bot.news_tool import _DATED_RE, _PUBLISHER_RE

MAX_DESKS = 2
MAX_NEWS_LINES = 2
_STOCK_SHAPES = {"ticker_opinion", "options_chain", "news_event", "company_profile"}


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
    r"(?:guided|guides|guiding|issued guidance|forecast)\b)"
    # ", and next-quarter revenue was guided to ...", ", and issued fiscal
    # first-quarter revenue guidance of ..." (2026-11-13 harness: a line that
    # ran the year's results into the guidance was posted whole as guidance)
    r"|,\s+(?:and\s+|while\s+)?(?=[^,;]{0,70}?\bguid(?:ed|ance)\b)", re.I)
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
        # the search sometimes writes the date twice ("2026-11-13 (2026-11-13) ...")
        body = re.sub(r"^\(?20\d\d-\d\d-\d\d\)?[:,]?\s*", "", body)
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


def _news_slots(state: str, fresh: dict | None, news: dict | None, have_price: bool = False,
                revenue_posted: bool = False) -> tuple[list[str], str | None]:
    """What the news lines add to the lines code posts (stock_body). Second
    value: an analyst's action after a print, which the outline places
    with the desks.

    - print: the result and the guidance are posted. When no feed carried
      the revenue, the result clauses of the news get a slot for it.
    - pending (the report is out, Yahoo has not filled the figures): the
      result and the guidance are only in the news, each its own slot.
    - otherwise the dated lines themselves."""
    lines = []
    if (news or {}).get("status") == "ok":
        lines = [ln for ln in (news.get("digest") or "").splitlines() if ln.strip()]
    if state not in ("print", "pending") or not fresh:
        if not lines:
            return [], None
        return ["NEWS: " + " | ".join(lines[:MAX_NEWS_LINES]) + " [the publisher each line ends with]"], None
    since = str(fresh.get("date") or "")
    parts = _split_news(lines[:MAX_NEWS_LINES], cut_moves=have_price, since=since)
    analyst = ("ANALYST ACTION: " + " | ".join(parts["analyst"]) + " [news search]"
               if parts["analyst"] else None)
    if state == "print":
        if revenue_posted:
            return [], analyst
        # every line: the results line is not always among the first two
        result = _split_news(lines, cut_moves=have_price, since=since)["result"][:MAX_NEWS_LINES]
        if not result:
            return [], analyst
        named = any(_PUBLISHER_RE.search(p) for p in result)
        return [("RESULT DETAIL: the EPS line is posted. From the news, the revenue and what else "
                 "the posted lines do not carry" + (", naming the publisher each line ends with" if named else "")
                 + ": " + " | ".join(result) + " [news search]")], analyst
    when = f"{fresh['date']} {fresh.get('session') or ''}".strip()
    head = f"RESULT: reported {when}; the figures are in the news lines [news search]"
    if parts["result"]:
        named = any(_PUBLISHER_RE.search(p) for p in parts["result"])
        head += (". From the news" + (", name the publisher each line ends with" if named else "")
                 + ": " + " | ".join(parts["result"]))
    slots = [head]
    if parts["guidance"]:
        slots.append("GUIDANCE: " + " | ".join(parts["guidance"]) + " [news search]")
    return slots, analyst


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


def _chain_error_slot(chain: dict | None) -> str | None:
    """The straddle's move is a posted line (stock_body.options_line). A
    chain that could not be read has no line, so the model says so."""
    if chain and chain.get("status") != "ok":
        return "OPTIONS: the options chain could not be read [options chain]"
    return None


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


_HEAD = "[ANSWER OUTLINE, built by the system from the blocks above. "
_SLOT_RULES = ("Write one arrow per numbered slot, in this order. Use the facts in that slot, and name "
               "only the source in its brackets, in words, never the bracket itself: a figure from the "
               "news or the company report is never a bank's. Say each in plain words. ")
_NO_NEW_FACTS = "Add no facts that are not in the blocks above.]"


def build_outline(results: dict, shape: str, symbol: str, fresh: dict | None = None,
                  asked: str = "", posted: dict | None = None) -> str:
    """The outline block for a stock answer, or '' when the turn is not
    about one stock or there is nothing to outline. `results` maps a
    prefetched tool name to its result. `asked` is the asker's own words
    (ask_router.asker_text), read only to tell a question about the desks.
    `posted` is the body code renders for the turn (discord_bot/stock_body.py):
    its lines open the reply as written, and the slots here are only what
    those lines do not carry."""
    from discord_bot import ask_router as R
    from discord_bot import stock_body as B
    if not symbol or (shape not in B.SHAPES) or (not posted and shape not in _STOCK_SHAPES):
        return ""
    lines = (posted or {}).get("lines") or []
    kinds = set((posted or {}).get("kinds") or [])
    state = (posted or {}).get("state") or (
        "none" if not fresh else "print" if fresh.get("actual") is not None else "pending")
    research = results.get(R.T_RESEARCH)
    # A reply to an earlier answer is a narrow question, and a plain date
    # lookup is answered by its lines: neither gets the rundown.
    lookup = shape == "earnings_date" and state == "none" and not research
    slots: list = []
    if shape != "follow_up" and not lookup:
        news_slots, analyst = _news_slots(state, fresh, results.get(R.T_NEWS),
                                          have_price="price" in kinds,
                                          revenue_posted="revenue" in kinds)
        slots = [
            *news_slots,
            _today_news_slot(results.get(R.T_NEWS)) if shape == "news_event" and not fresh else None,
            _driver_slot(results.get(R.T_PRIMER), fresh),
            analyst,
            *_desk_slots(research, fresh, asked),
            _chain_error_slot(results.get(R.T_CHAIN)) if shape == "options_chain" else None,
        ]
    slots = [s for s in slots if s]
    body = "\n".join(f"{i}. {s}" for i, s in enumerate(slots, 1))
    if not lines:
        if len(slots) < 2:
            return ""
        return _HEAD + _SLOT_RULES + _NO_NEW_FACTS + "\n" + body
    head = (_HEAD + "The system posts the lines below first, exactly as written, as the start of "
            "your reply:\n" + "\n".join(lines)
            + "\nDo not write them, and do not repeat, recompute or restate a figure they carry. ")
    if not slots:
        return (head + "If the question asks for something the lines do not answer, answer it in one "
                "or two arrows from the blocks above. Otherwise write one short arrow on what stands "
                "out in them. " + _NO_NEW_FACTS)
    return (head + _SLOT_RULES + "The stock's move comes from the posted lines only. If the question "
            "asks for something neither the posted lines nor the slots answer, answer it in one more "
            "arrow from the blocks above. " + _NO_NEW_FACTS + "\n" + body)


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
