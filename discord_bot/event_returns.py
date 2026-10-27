"""Returns after dated events, both steps run in code (2026-10-27).

"1 week and 1 month returns after the last few SpaceX unlocks" needs two
steps: the dates (only a web search has them) and the returns (only the
closes have them). lookup_price_history computes the returns when it is
given `event_dates`, but the model has to find the dates first and then
pass them, and on the 2026-10-27 harness run it pulled the whole series
with start/end and did the arithmetic itself. This prefetch does both:
one grounded Flash-Lite search for the dates, then the returns from
Yahoo's closes through ask_tools.returns_after. The answer body is
rendered from the result in code (discord_bot/answer_block.py).

Prefetch only. Its cost is one grounded call per question, recorded in
the spend ledger as `ask_events`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, timedelta

log = logging.getLogger(__name__)

SEARCH_TIMEOUT_S = 9.0         # under the 14 s prefetch deadline bot.py gives this tool
MAX_EVENTS = 8
CACHE_TTL_S = 30 * 60
_CACHE: dict[tuple[str, str, int], tuple[float, dict]] = {}
_client = None

PROMPT = (
    "Search the web for the dates of the last {n} {event} for the US-listed stock {sym}{name}, "
    "newest first, only ones that have already happened. One line per event and nothing else: "
    "the date as YYYY-MM-DD, then a colon, then what happened in a few words with its size "
    "(shares, dollars) if reported, then the timing (before the open, during the session, after "
    "the close) if reported, then the publisher in parentheses. If you find none, reply exactly: "
    "NONE FOUND."
)
_LINE_RE = re.compile(r"^\s*[-*•]?\s*\(?(20\d\d-\d\d-\d\d)\)?\s*[:,\-–—]?\s*(.*)$")
_AFTER_CLOSE_RE = re.compile(r"\bafter\s+(?:the\s+)?(?:close|bell)\b|\bafter[- ]hours\b|\bpost-?market\b",
                             re.I)
_PUBLISHER_RE = re.compile(r"\s*\([^()]*\)\s*\.?\s*$")
# Fed decisions land at 2 PM: the day's own close is the base.
_AFTER_CLOSE_EVENT_RE = re.compile(r"\b(?:fomc|fed)\b", re.I)


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_events", api_key=key)
    return _client


def parse_events(text: str, today: date, event: str = "") -> list[dict]:
    """[{date, label, after_close}] from the search reply, newest first,
    dated no later than today and no earlier than five years back."""
    out, seen = [], set()
    for ln in (text or "").splitlines():
        m = _LINE_RE.match(ln)
        if not m:
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d > today or d < today - timedelta(days=5 * 366) or d in seen:
            continue
        seen.add(d)
        rest = m.group(2).strip()
        # the room's voice has no dashes or semicolons
        label = re.sub(r"\s*[—–;]\s*", ", ", _PUBLISHER_RE.sub("", rest)).strip(" .,")
        out.append({"date": d.isoformat(), "label": label[:80],
                    "after_close": bool(_AFTER_CLOSE_RE.search(rest)
                                        or _AFTER_CLOSE_EVENT_RE.search(event))})
    out.sort(key=lambda e: e["date"], reverse=True)
    return out[:MAX_EVENTS]


async def _search(sym: str, event: str, n: int, name: str) -> tuple[str, list[dict]]:
    """The search reply text and its links."""
    from google.genai import types
    from config import settings
    from discord_bot.news_tool import _sources
    client = _get_client()
    if client is None:
        return "", []
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.1, max_output_tokens=700,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )
    resp = await asyncio.wait_for(client.aio.models.generate_content(
        model=settings.ask_gemini_model or settings.gemini_model,
        contents=PROMPT.format(n=n, event=event, sym=sym, name=f" ({name})" if name else ""),
        config=config), SEARCH_TIMEOUT_S)
    cand = (getattr(resp, "candidates", None) or [None])[0]
    return (getattr(resp, "text", None) or "").strip(), _sources(getattr(cand, "grounding_metadata", None))


async def _execute_event_returns(args: dict) -> dict:
    """The dates of a ticker's recent events and the returns after each."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from report import market_data as _md
    from discord_bot.ask_tools import returns_after
    sym = str(args.get("symbol") or "").strip().upper().lstrip("$")
    event = str(args.get("event") or "").strip().lower()
    if not sym or not event:
        return {"status": "error", "error": "symbol and event are required"}
    n = max(2, min(int(args.get("count") or 6), MAX_EVENTS))
    hit = _CACHE.get((sym, event, n))
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    today = datetime.now(ZoneInfo("America/New_York")).date()
    try:
        text, sources = await _search(sym, event, n, str(args.get("name") or "").strip())
    except asyncio.TimeoutError:
        return {"status": "timeout", "symbol": sym, "event": event,
                "error": "The search for the event dates did not finish in time."}
    except Exception as e:
        log.warning(f"event returns {sym} {event}: search failed: {e}")
        return {"status": "error", "symbol": sym, "event": event,
                "error": "The search for the event dates failed."}
    events = parse_events(text, today, event)
    if not events:
        return {"status": "no_data", "symbol": sym, "event": event,
                "error": f"No dated {event} found for {sym}: say so, never guess dates."}
    start = (date.fromisoformat(events[-1]["date"]) - timedelta(days=10)).isoformat()
    try:
        hist = await asyncio.to_thread(_md.fetch_price_history, sym, start, None, "1d")
    except Exception as e:
        log.warning(f"event returns {sym}: history failed: {e}")
        hist = None
    if not hist:
        return {"status": "no_data", "symbol": sym, "event": event, "events": events,
                "error": f"No price history for {sym}: give the dates, never invent returns."}
    rows = []
    for e in events:
        r = returns_after(hist, e["date"], e["after_close"])
        rows.append({**r, "label": e["label"], "after_close": e["after_close"]})
    out = {"status": "ok", "symbol": sym, "event": event, "events": rows,
           # A reply with no links may be the model's memory, not the search
           # (the SPCX unlock dates were invented three times, 2026-06-17).
           "dates_confirmed": bool(sources),
           "digest": "\n".join(ln.strip() for ln in text.splitlines() if _LINE_RE.match(ln)),
           "sources": sources,
           "how_to_read": ("dates from a Google search; base is the last close before the date "
                           "(that day's close when the event came after the open); each horizon "
                           "is 1, 5 and 21 trading sessions later")}
    now = time.monotonic()
    for k, (ts, _) in list(_CACHE.items()):
        if now - ts >= CACHE_TTL_S:
            _CACHE.pop(k, None)
    _CACHE[(sym, event, n)] = (now, out)
    return out


_HORIZON_WORDS = (("1_day", "1 day"), ("1_week", "1 week"), ("1_month", "1 month"))


def block_lines(r: dict) -> list[str]:
    """One line per event, oldest first (discord_bot/answer_block.py)."""
    lines = []
    for e in sorted(r.get("events") or [], key=lambda x: x.get("date") or ""):
        try:
            d = date.fromisoformat(e["date"])
        except (KeyError, ValueError):
            continue
        head = f"→ **{d:%b} {d.day}, {d.year}**" + (f", {e['label']}" if e.get("label") else "")
        if e.get("error") or "base_close" not in e:
            lines.append(f"{head}: no close before this date in the price history")
            continue
        b = date.fromisoformat(e["base_date"])
        parts = []
        for key, word in _HORIZON_WORDS:
            h = e.get(key)
            if isinstance(h, dict):
                parts.append(f"{h['return_pct']:+.1f}% after {word}")
            else:
                parts.append(f"{word}: not yet")
        lines.append(f"{head}: from ${e['base_close']:,.2f} ({b:%b} {b.day} close), "
                     + " · ".join(parts))
    if lines and not r.get("dates_confirmed", True):
        lines.append("→ The search behind these dates returned no links, so treat the dates as "
                     "unconfirmed.")
    return lines
