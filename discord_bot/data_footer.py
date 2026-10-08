"""The "Data:" footer for /ask answers built from the bot's own feeds.

A grounded answer ends with a numbered Sources list. An answer whose
figures came from a data tool (a price, an earnings date, a Sleeper
score) ended with nothing, so it read as uncited (owner, 2026-09-30:
"some don't have citations"). This names the feeds behind the tools
that returned data on the turn. Rendered from the tool trace in code,
so the model cannot omit or invent it.
"""
from __future__ import annotations

import re

# tool name -> what the reader is told the figures came from
FEEDS: dict[str, str] = {
    "lookup_market_price": "live prices (Finnhub, Binance.US, Yahoo)",
    "lookup_earnings_date": "earnings calendar (Finnhub, Nasdaq)",
    "lookup_earnings_slate": "earnings calendar (Finnhub, Nasdaq)",
    "lookup_options_chain": "options chain (Yahoo)",
    "lookup_price_history": "price history (Yahoo)",
    "lookup_economic_calendar": "economic calendar (Finnhub, ForexFactory, BLS, BEA)",
    "lookup_fantasy_league": "Sleeper league",
    "lookup_user_profile": "room trade ledger",
    "lookup_trade_log": "room trade ledger",
    "lookup_room_positions": "room trade ledger",
    "lookup_research": "bank research notes",
    "lookup_ticker_snapshot": "company and trading data (Yahoo)",
    "ticker_news": "news search (Google)",
    "ticker_primer": "company background (Google)",
    "query_data": "research database",
}
# Tools whose data is the room's own conversation. The reader is in the
# room, so "Data: room chat" told them nothing (owner, 2026-09-30).
NOT_CITED = frozenset({"search_chat_messages"})

# Statuses that mean the call produced nothing (mirrors bot._FAILED_TOOL_STATUSES).
_FAILED = frozenset({"no_data", "error", "empty", "not_found", "timeout", "not_a_stock", "building",
                     "metric_not_asked"})


def _label(entry: dict) -> str | None:
    """The feed label for one trace entry. The profile tool's racism
    ranking comes from the chat tags, not the trade ledger (2026-10-01:
    a racism board was footed 'room trade ledger'), and a lookup by
    username returns both ranks."""
    tool = str(entry.get("tool") or "")
    if tool == "lookup_user_profile":
        args = entry.get("args") or {}
        metric = str(args.get("metric") or "").lower() if isinstance(args, dict) else ""
        if metric == "racism":
            return "room chat tags"
        if metric != "trader":
            return "room trade ledger and chat tags"
    return FEEDS.get(tool)


# Feeds whose contribution is figures: cited only when one of the
# payload's numbers is in the answer (2026-10-08 audit: "live prices" on an
# answer whose figures all came from a member's quoted post, "Sleeper
# league" on an answer built from chat). Research, news and the primer
# carry words as much as numbers and stay cited when they ran.
NUMERIC_TOOLS = frozenset({
    "lookup_market_price", "lookup_earnings_date", "lookup_earnings_slate",
    "lookup_options_chain", "lookup_price_history", "lookup_economic_calendar",
    "lookup_fantasy_league", "lookup_ticker_snapshot", "lookup_trade_log",
    "lookup_room_positions"})


def _loose(cores) -> set[str]:
    """Numbers with their whole-number part as well: an answer rounds
    $405.42 to $405."""
    out = set(cores)
    out |= {c.split(".")[0] for c in cores if len(c.split(".")[0]) >= 2}
    return out


_RECORD_RE = re.compile(r"\b\d{1,2}-\d{1,2}(?:-\d{1,2})?\b")


def payload_figs(text: str) -> set[str]:
    """What a payload contributes: its figures and its win-loss records
    ("1-3" is the whole answer to "how is his team doing")."""
    return numeric_cores(text) | set(_RECORD_RE.findall(text or ""))


def _used(entry: dict, answer: str | None) -> bool:
    if answer is None or str(entry.get("tool") or "") not in NUMERIC_TOOLS:
        return True
    figs = entry.get("figs")
    if figs is None:            # no payload numbers recorded for this entry
        return True
    return bool(_loose(figs) & _loose(payload_figs(answer)))


def footer(tool_trace: list[dict] | None, answer: str | None = None) -> str:
    """'\\n\\nData: a · b' for the feeds that returned data this turn, in
    first-use order and without repeats. Empty when no tool did."""
    seen: list[str] = []
    for entry in tool_trace or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("status") or "ok") in _FAILED:
            continue
        if not _used(entry, answer):
            continue
        args = entry.get("args") or {}
        if (entry.get("tool") == "query_data" and isinstance(args, dict)
                and "chat_messages" in str(args.get("sql") or "")):
            continue            # the room's own chat, like search_chat_messages
        label = _label(entry)
        if label and label not in seen:
            seen.append(label)
    if not seen:
        return ""
    return "\n\nData: " + " · ".join(seen)


_FIGURE_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|[BMKT]\b|bn\b)?")


def figures(text: str) -> set[str]:
    """Figures worth matching: a currency or percent, or a number with a
    decimal or at least three digits. Bare small integers ("3 notes",
    "Q4") match everything."""
    out = set()
    for m in _FIGURE_RE.finditer(text or ""):
        f = m.group(0).replace(",", "").replace(" ", "")
        digits = sum(c.isdigit() for c in f)
        if f.isdigit() and len(f) == 4 and f[:2] in ("19", "20"):
            continue                                    # a year, in every dated line
        if f.startswith("$") or f.endswith("%") or "." in f or digits >= 3:
            out.add(f.rstrip("."))
    return out


def numeric_cores(text: str) -> set[str]:
    """The numbers in `figures(text)` without their formatting: '$18.2B',
    '$18.2 billion' and '18.2' all give '18.2'. For checking that a
    rewrite kept every figure when it may spell out units."""
    out = set()
    for f in figures(text):
        m = re.search(r"\d[\d.]*", f)
        if m:
            out.add(m.group(0).rstrip("."))
    return out


def compose(grounding_footer: str, answer: str, news: dict | None,
            tool_trace: list[dict] | None) -> str:
    """The answer's citation block.

    Google grounding on the answer wins outright, as before. Otherwise:
    the news prefetch's links, but only when the answer carries a figure
    from the news digest (a link cited for a headline the answer never
    used says nothing about where its numbers came from), then the Data
    line naming the bot's own feeds, which includes the news search."""
    if grounding_footer:
        # Google sources plus the bot's own feeds: a grounding footer used
        # to replace the Data line, so a Yahoo price or calendar row went
        # uncited on a grounded answer (2026-10-08 audit, 9 answers).
        data = footer(tool_trace, answer)
        return grounding_footer + ("\n" + data.lstrip("\n") if data else "")
    out = ""
    news = news or {}
    links = news.get("sources") or []
    if links and figures(answer) & figures(news.get("digest") or ""):
        out = "\n\nSources:\n" + "\n".join(
            f"[{i + 1}] [{(s.get('title') or s['url'])[:80]}](<{s['url']}>)"
            for i, s in enumerate(links[:2]))
    data = footer(tool_trace, answer)
    if data:
        out = out + "\n" + data.lstrip("\n") if out else data
    return out
