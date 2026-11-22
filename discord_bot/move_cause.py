"""Why a stock moved, or what happened to it (2026-11-21, owner).

2026-11-20, 4:38 PM ET: "what happened with CRWD and ZS stocks right now
after hours". The bot posted both moves correctly from the quote feed and a
third arrow it made up ("cyber names seeing extreme bifurcated flow and
multiple compression as desks de-risk enterprise software"). That evening
CrowdStrike was reported to be in talks to buy Zscaler, and the room had
already said so. Three things lined up:

1. The news search ran in code only when one stock was named
   (ask_router). With two, the turn carried prices alone.
2. Google was offered and the model did not call it. Search is the model's
   choice in the API, and it skips it when a tool payload is in the turn.
3. The grounding net stood down: every figure of the draft was in the
   price payload. A cause stated with no figure has nothing to check.

The same question about ZS alone, run the next day, found the talks: same
model and prompt, the only difference that code ran the search. So here
the search runs in code for every stock the question names (up to
ask_router.MAX_CAUSE_SYMBOLS), and the answer has one shape:

- one price line per name, posted by code (price_block.quote_line);
- the cause, worded by the model from the news lines dated since the last
  session and from nothing else, one arrow per name that has such lines;
- for a name with no such line, a line built here saying the search found
  nothing. The model has no slot to write a cause in, and an arrow it
  writes anyway is dropped. A search that ran and found nothing is the
  answer. Only when the system's search did not come back may the model's
  own search stand in, and only when its reply carries Google sources.

Nothing else is posted: no report date, no business description, no desk
view. The 2026-11-21 harness answer on ZS alone had the cause right as the
third of five arrows, the other four unasked.

Scope: a question that asks only for the cause (ask_router.asks_cause), on
stocks, with no report in the last days. After a print the result lines
are the cause and stock_body keeps the turn. A fund, an index or a coin
has no code search yet; for those the grounding net no longer stands down
on price data alone (bot.py phase 7), so the model's own search is forced.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from discord_bot.news_tool import _DATED_RE, _PUBLISHER_RE

MAX_LINES = 3                 # news lines per stock handed to the model
MAX_GROUNDED_ARROWS = 2       # the model's own arrows when only its search found the cause

# The asker names a longer stretch than the last session: every line of
# the search's week counts.
_WIDE_RE = re.compile(
    r"\b(?:(?:this|last|past|the)\s+(?:week|few\s+days|couple\s+(?:of\s+)?days)|lately|recently|"
    r"all\s+week|since\s+\w+)\b", re.I)


def _today() -> date:
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()


def prev_trading_day(today: date) -> date:
    """The last trading day strictly before `today`."""
    from world_context import previous_trading_day
    return date.fromisoformat(previous_trading_day(today.isoformat()))


def cutoff(asked: str = "", today: date | None = None) -> date:
    """News dated on or after this day can explain the move asked about:
    the trading day before today, which covers today's session, last
    night's release, and Friday's close when asked over a weekend."""
    today = today or _today()
    if _WIDE_RE.search(asked or ""):
        return today - timedelta(days=7)
    return prev_trading_day(today)


# "..., causing Zscaler shares to jump over 16% in after-hours trading": the
# move as the consequence of the event, which stock_outline's pattern (the
# move as a clause of its own) does not read.
_CAUSED_MOVE_RE = re.compile(
    r",?\s+(?:causing|sending|pushing|driving|lifting|dragging|"
    r"(?:which|that)\s+(?:sent|pushed|lifted|drove|dragged|caused))\s+"
    r"(?=[^;|]*(?:\bshares?\b|\bstock\b|%))[^;|]*$", re.I)
# "..., with the Nasdaq falling 5.6%": an article's own index move, which is
# the figure that goes stale beside the live quote.
_INDEX_TAIL_RE = re.compile(
    r",?\s+(?:with|as|while|and)\s+(?:the\s+)?(?:s&p(?:\s*500)?|nasdaq(?:\s+(?:composite|100))?|dow(?:\s+jones)?"
    r"(?:\s+industrial\s+average)?|vix|russell(?:\s+2000)?|(?:major\s+|main\s+)?(?:indexes|indices|averages))\b"
    r"[^;|]*$", re.I)
# a second date the search wrote after the first: "(2026-11-21 reports):"
_SECOND_DATE_RE = re.compile(r"^(?:\([^()]*20\d\d-\d\d-\d\d[^()]*\)|20\d\d-\d\d-\d\d)[:,]?\s*")


def _clean(line: str) -> str:
    """One news line for the model: the news's own account of the stock's
    move is cut (the posted quote is the move, and the two disagree by the
    minute), the date and the publisher stay."""
    from discord_bot.stock_outline import _MOVE_TAIL_RE
    m = _DATED_RE.match(line)
    day = m.group(1) if m else ""
    body = (line[m.end():] if m else line).strip().lstrip(")*:–-• ")
    body = _SECOND_DATE_RE.sub("", body)
    pub = ""
    pm = _PUBLISHER_RE.search(body.rstrip(". "))
    if pm:
        pub, body = f" ({pm.group(1)})", body.rstrip(". ")[:pm.start()]
    cut = _INDEX_TAIL_RE.sub("", _CAUSED_MOVE_RE.sub("", _MOVE_TAIL_RE.sub("", body))).strip().rstrip(".,")
    if len(cut.split()) >= 4:
        body = cut
    body = body.strip().rstrip(".")
    return f"{day}: {body}{pub}" if day else f"{body}{pub}"


def fresh_lines(news: dict | None, asked: str = "", today: date | None = None) -> list[str]:
    """The search's lines dated on or after the cutoff, newest first as the
    search gave them, at most MAX_LINES."""
    if not isinstance(news, dict) or news.get("status") != "ok":
        return []
    cut = cutoff(asked, today).isoformat()
    out = []
    for ln in (news.get("digest") or "").splitlines():
        m = _DATED_RE.match(ln)
        if m and m.group(1) >= cut:
            out.append(_clean(ln))
    return out[:MAX_LINES]


MARKET = "the US market"       # the label of the market-wide search (ask_router.market_prefetch)
MARKET_ARROWS = 2              # a session usually has more than one driver


def _names(symbols: list[str]) -> str:
    bold = [s if s == MARKET else f"**{s}**" for s in symbols]
    return bold[0] if len(bold) == 1 else ", ".join(bold[:-1]) + " or " + bold[-1]


def missing_lines(found_nothing: list[str], failed: list[str], cut: date) -> list[str]:
    """What code posts for a stock whose search gave no line that can
    explain the move: one line for the searches that found nothing dated
    `cut` or later, one for those that did not come back."""
    out = []
    if found_nothing:
        out.append(f"→ The news search found nothing on {_names(found_nothing)} dated "
                   f"{cut:%a} {cut:%b} {cut.day} or later, so no reason for the move is confirmed.")
    if failed:
        out.append(f"→ The news search on {_names(failed)} did not come back, "
                   "so no reason for the move is confirmed.")
    return out


def any_fresh(plan: list, results: list, asked: str = "", today: date | None = None) -> bool:
    """A news search in this turn returned a line dated since the last session."""
    from discord_bot import ask_router as R
    return any(t == R.T_NEWS and fresh_lines(r, asked, today)
               for (t, _), r in zip(plan or [], results or []))


def _named_search(tool: str, args: dict | None) -> str:
    """The stock a news search of the plan is about, when the asker named
    it: '' for another tool, a fund or coin, or a sector basket's first name."""
    from discord_bot import ask_router as R
    if tool == R.T_NEWS and (args or {}).get("market"):
        return MARKET
    sym = str((args or {}).get("symbol") or "").upper()
    return sym if tool == R.T_NEWS and R.is_stock(sym) and not (args or {}).get("basket") else ""


def needs_search(plan: list, results: list, asked: str = "", today: date | None = None) -> bool:
    """The turn has no news line that can explain the move, and not because
    a search on a named stock ran and found nothing: none ran (a fund, an
    index, a coin, a sector) or one did not come back. Only then is the
    model's own search forced."""
    if any_fresh(plan, results, asked, today):
        return False
    ran = [r for (t, a), r in zip(plan or [], results or []) if _named_search(t, a)]
    return not ran or any(not isinstance(r, dict) or r.get("status") not in ("ok", "no_data") for r in ran)


# A first word that does not tell one listed company from another.
_GENERIC_FIRST = frozenset(
    "american first general united national international global the new advanced applied digital".split())


def _mentions(lines: list[str], sym: str) -> bool:
    """A news line names the stock: its symbol as a word, or the first
    word of its listed name ("Zscaler" for ZS)."""
    from discord_bot import listed_symbols as L
    text = " ".join(lines)
    if len(sym) >= 2 and re.search(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])", text):
        return True
    first = re.split(r"[\s,.]+", (L.name(sym) or "").strip())[0]
    return (len(first) >= 4 and first.lower() not in _GENERIC_FIRST
            and re.search(rf"\b{re.escape(first)}\b", text, re.I) is not None)


def _groups(symbols: list[str], lines: dict[str, list[str]]) -> list[list[str]]:
    """The stocks that have news lines, grouped where one's lines name
    another: CrowdStrike's line about buying Zscaler is one event for both
    (two of four harness runs on 2026-11-21 wrote it once per stock)."""
    group = {s: [s] for s in symbols}
    for a in symbols:
        for b in symbols:
            if a != b and lines.get(a) and group[a] is not group[b] and _mentions(lines[a], b):
                joined = group[a] + [s for s in group[b] if s not in group[a]]
                for s in joined:
                    group[s] = joined
    out, seen = [], set()
    for s in symbols:
        g = group[s]
        if id(g) not in seen and any(lines.get(x) for x in g):
            seen.add(id(g))
            out.append(sorted(g, key=symbols.index))
    return out


def _merged(per_stock: list[list[str]]) -> list[str]:
    """Several stocks' lines as one list: each stock's first before any
    stock's second, a repeated line once, at most MAX_LINES."""
    out, seen = [], set()
    for i in range(max((len(x) for x in per_stock), default=0)):
        for lines in per_stock:
            key = re.sub(r"[^a-z0-9]", "", lines[i].lower()) if i < len(lines) else ""
            if key and key not in seen:
                seen.add(key)
                out.append(lines[i])
    return out[:MAX_LINES]


def build(plan: list, results: list, asked: str = "", today: date | None = None) -> dict | None:
    """The body of a cause turn from the route's prefetch plan and its
    results (None for a prefetch that timed out), or None when no stock was
    searched or no quote came back. Shaped like stock_body.build's result,
    which the phase-9 wiring reads. Plain data: the ask log serializes it."""
    from discord_bot import ask_router as R
    from discord_bot.price_block import quote_line
    price, searched = None, []
    for (tool, args), res in zip(plan or [], results or []):
        if tool == R.T_PRICE and isinstance(res, dict) and res.get("status") == "ok" and price is None:
            price = res
        else:
            sym = _named_search(tool, args)
            if sym and sym not in [s for s, _ in searched]:
                searched.append((sym, res if isinstance(res, dict) else None))
    if not searched or not price:
        return None
    session = str(price.get("session") or "")
    lines = [ln for ln in (quote_line(q, session) for q in price.get("quotes") or []) if ln]
    if not lines:
        return None
    cut = cutoff(asked, today)
    per_stock = {sym: fresh_lines(res, asked, today) for sym, res in searched}
    # One event that moved several of the stocks is one block and one
    # sentence. A stock another's line names is explained by that line,
    # whatever its own search returned.
    news, explained = {}, set()
    for group in _groups([s for s, _ in searched], per_stock):
        news[" and ".join(group)] = _merged([per_stock[s] for s in group])
        explained.update(group)
    found_nothing, failed = [], []
    for sym, res in searched:
        if sym in explained:
            continue
        if res is not None and res.get("status") in ("ok", "no_data"):
            found_nothing.append(sym)
        else:
            failed.append(sym)
    return {"lines": lines, "kinds": ["price"], "desks": [], "state": "cause",
            "symbol": searched[0][0], "shape": "news_event", "has_price": True,
            # no headline figure: stock_body drops an arrow that repeats one,
            # and a cause that names the price is still the cause
            "price": None, "eps": None, "revenue": None, "revenue_source": "",
            "cause": {"symbols": [s for s, _ in searched], "news": news, "slots": len(news),
                      "found_nothing": found_nothing, "failed": failed, "cutoff": cut.isoformat(),
                      "missing": missing_lines(found_nothing, failed, cut)}}


_HEAD = ("[ANSWER OUTLINE, built by the system. The question asks why a stock moved or what happened "
         "to it. The system posts the lines below first, exactly as written, as the start of your reply:\n")
_NOT_YOURS = ("\nDo not write them, and give no figure for the size of a move: it is in those lines. "
              "Nothing else belongs in this reply: no report date, no business description, no desk "
              "view, no positioning, no outlook. ")


def outline(body: dict) -> str:
    """The last block the model reads on a cause turn."""
    c = body["cause"]
    head = _HEAD + "\n".join(body["lines"]) + _NOT_YOURS
    if not c["slots"] and c["failed"]:
        return (head + "The news search the system ran did not come back, and the system says so under "
                "your reply unless you find the cause: search the web now, and if a report dated in the "
                "last two days explains the move, write it in one sentence and name the publisher. If "
                "you find none, reply with the one line: No reported cause. Never offer a reason from "
                "memory, from the price action or from the sector.]")
    if not c["slots"]:
        return (head + "The news search the system ran found nothing dated recently enough to explain "
                "the move, and the system says so under your reply. Reply with the one line: No "
                "reported cause. Never offer a reason from memory, from the price action or from the "
                "sector.]")
    if MARKET in c["news"]:
        return (head + f"Write at most {MARKET_ARROWS} arrows: what moved the market, one sentence each, "
                "taken from the news lines below and from nothing else. Give the reasons the lines "
                "state and no figure they do not carry. Name one publisher per arrow, the one its line "
                "ends with, in plain text. If no line explains the move, say the news lines give no "
                "reason for it.]" + chr(10) + "NEWS ON THE US MARKET: " + " | ".join(c["news"][MARKET]))
    n = c["slots"]
    arrows = "one arrow" if n == 1 else f"one arrow per NEWS block below, {n} arrows"
    if any(" and " in label for label in c["news"]):
        arrows += (", and where a block is on several stocks, one sentence on the event that moved "
                   "them, naming each")
    rest = ""
    if c["missing"]:
        gone = [*c["found_nothing"], *c["failed"]]
        rest = (f" The search found nothing recent on {', '.join(gone)}: the system says so under your "
                "reply, so write no cause for it unless a line below states one.")
    slots = "\n".join(f"NEWS ON {sym}: " + " | ".join(lines) for sym, lines in c["news"].items())
    return (head + f"Write {arrows}: what moved the stock, in one sentence, taken from the news lines "
            "below and from nothing else. Give the event that explains the move and leave the other "
            "lines out: no list of contributing factors. Name one publisher for it, the one its line "
            "ends with, in plain text. Bold only a stock's symbol. If no line explains a move, say "
            "the news lines give no reason for it." + rest + "]\n" + slots)


def as_posted(line: str) -> str:
    """A news line as an arrow, when the model wrote no cause from the
    lines it was given: '2026-11-20: text (Publisher)' -> '→ Nov 20: text
    (Publisher).'"""
    m = re.match(r"(20\d\d-\d\d-\d\d):\s*(.*)$", line)
    if m:
        try:
            d = date.fromisoformat(m.group(1))
            line = f"{d:%b} {d.day}: {m.group(2)}"
        except ValueError:
            pass
    return "→ " + line.rstrip(".") + "."


def compose(answer: str, body: dict, grounded: bool = False) -> tuple[str, dict]:
    """(the posted answer, what happened to the model's arrows).

    The price lines, then the model's arrows up to one per stock that had
    news lines, then the line for each stock with none. A stock the model
    wrote nothing for gets its newest news line as the search wrote it.

    With no news lines the model's arrows are dropped, with one exception:
    the system's search did not come back and the model's reply carries
    Google sources (`grounded`), the one other way a cause can have a
    source. A search that ran and found nothing is the answer: the forced
    fallback search then returned filler with links ("valuation resets and
    profit-taking" on a stock that was up, 2026-11-21 harness)."""
    from discord_bot import stock_body as B
    c = body["cause"]
    kept, dropped = B.arrows(answer, body)
    own_search = grounded and bool(c["failed"]) and not c["slots"]
    if not own_search:
        # A figure in the cause must be one the price lines or the news
        # lines carry: an index move read out of an article is the figure
        # that went stale ("S&P 500 down 2.1%" beside the live quote).
        from discord_bot.figure_provenance import unsourced_figures
        evidence = chr(10).join([*body["lines"], *(ln for lines in c["news"].values() for ln in lines)])
        sourced = [p for p in kept if not unsourced_figures(p, evidence)[1]]
        dropped += len(kept) - len(sourced)
        kept = sourced
    cap = (MARKET_ARROWS if MARKET in c["news"] else c["slots"]) or (MAX_GROUNDED_ARROWS if own_search else 0)
    dropped += max(0, len(kept) - cap)
    kept = kept[:cap]
    fallback = False
    if c["slots"] and not kept:
        kept = [as_posted(lines[0]) for lines in c["news"].values()]
        fallback = True
    missing = c["missing"]
    if own_search and kept:
        # the model's sourced arrows stand in for the search that failed
        missing = missing_lines(c["found_nothing"], [], date.fromisoformat(c["cutoff"]))
    return ("\n\n".join([*body["lines"], *kept, *missing]),
            {"kept": len(kept), "dropped": dropped, "missing": bool(missing), "fallback": fallback})


def merge_news(prev: dict | None, res: dict) -> dict:
    """The turn's news for the Sources footer: every search's links, one
    stock's first link before another's second."""
    a, b = list((prev or {}).get("sources") or []), list(res.get("sources") or [])
    links, seen = [], set()
    for i in range(max(len(a), len(b))):
        for src in (a, b):
            # one link per site: two pages of one site read as the same
            # source twice ("stocktitan.net" on both lines, 2026-11-21 harness)
            site = (src[i].get("title") or src[i].get("url") or "").lower() if i < len(src) else ""
            if i < len(src) and src[i].get("url") not in seen and site not in seen:
                seen.update({src[i].get("url"), site})
                links.append(src[i])
    digest = "\n".join(d for d in ((prev or {}).get("digest") or "", res.get("digest") or "") if d)
    return {"sources": links, "digest": digest}
