"""The /ask ticker-news prefetch (2026-09-30): one Google-grounded search,
run in code, for what happened to a stock in the last few days.

Why it exists: on 2026-09-30 "what your thoughts on MU" was answered two
hours after Micron reported, entirely from the banks' pre-print previews
("into the print"). Google was allowed on the shape, but the model did not
call it, and the grounding retry counts a tool payload as a source, so a
research-backed answer never reaches the web. The model cannot be forced to
search; code can. This prefetch makes the search itself and hands the
model a dated, sourced digest beside the bank notes and the snapshot.

Prefetch only: the model is not offered it as a tool (it already has
Google). Its cost is one grounded Flash-Lite call per single-stock question,
recorded in the spend ledger as `ask_news`.

A "what is X" question asks for the last LONG_DAYS (2026-10-30 audit A7:
"what is ELMT stock" searched the last week and never found the Defense
Department's $450M stake from six weeks earlier, the event that defines
the stock). That search names the stock's largest one-day moves, computed
in code from Yahoo's closes, and asks what caused each: a search for "the
biggest events" alone returned three minor September items and missed the
stake (probe 2026-10-30).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger(__name__)

NEWS_TIMEOUT_S = 7.5          # under the router's 8 s prefetch deadline
MAX_SOURCES = 4
NEWS_ATTEMPTS = 2
DIGEST_CHARS = 2500
# A search took 6.4 s for MU on 2026-09-30 and every stock answer waits on
# the slowest prefetch; the room asks about the same name in bursts.
CACHE_TTL_S = 10 * 60
MISS_TTL_S = 2 * 60
_CACHE: dict[str, tuple[float, dict]] = {}
_client = None

PROMPT = (
    "Search the web for news about the US-listed stock {sym}{name} from the last 5 trading "
    "days, today included. Report only facts, newest first, one line each, each line "
    "starting with its date (YYYY-MM-DD) and ending with the publisher in parentheses:\n"
    "- earnings: if the company reported, the actual revenue and EPS against the estimates, "
    "each with its growth from the same quarter a year earlier, guidance against the "
    "estimates, and the stock's move after the release\n"
    "- other company news: deals, contracts, product launches, offerings, executive changes, "
    "regulatory or legal events\n"
    "- analyst rating or price-target changes, with the firm\n"
    "- today's move and its stated reason, if reported\n"
    "No opinions, no forecasts of your own, no background. If you find nothing dated in "
    "the window, reply exactly: NO RECENT NEWS."
)
LONG_DAYS = 90
MAX_MOVES = 3
HISTORY_TIMEOUT_S = 2.5
# The moves come first: asked as one item among "the largest events", the
# search answered NO RECENT NEWS in four tokens (harness, 2026-10-30); asked
# why the stock moved on each day, it found the stake with 5 to 13 links.
LONG_PROMPT = (
    "{head} major company events of {who} since {since}: earnings "
    "results against the estimates, government or strategic investments and stakes, deals, "
    "contracts, share offerings. One line per event, newest first, at most 8 lines, each "
    "starting with its date (YYYY-MM-DD) and ending with the publisher in parentheses. Facts "
    "only, plain text, no markdown. If a search finds nothing for a day, leave that day out. If "
    "you find nothing at all, reply exactly: NO RECENT NEWS."
)
MOVES_CLAUSE = ("Search the web for why shares of {who} moved on each of these days: {moves}. "
                "Then search for the other")


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_news", api_key=key)
    return _client


def _sources(gm) -> list[dict]:
    out, seen = [], set()
    for ch in (getattr(gm, "grounding_chunks", None) or []):
        web = getattr(ch, "web", None)
        url = getattr(web, "uri", None) if web else None
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": (getattr(web, "title", None) or "")[:80], "url": url})
        if len(out) >= MAX_SOURCES:
            break
    return out


async def _execute_ticker_news(args: dict) -> dict:
    """Run the grounded news search for one symbol."""
    from google.genai import types
    from config import settings
    sym = (args.get("symbol") or "").strip().upper().lstrip("$")
    if not sym:
        return {"status": "error", "error": "No symbol provided."}
    days = LONG_DAYS if int(args.get("days") or WINDOW_DAYS) > WINDOW_DAYS else WINDOW_DAYS
    key = f"{sym}:{days}"
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    client = _get_client()
    if client is None:
        return {"status": "error", "symbol": sym, "error": "No Gemini key configured."}
    t0 = time.monotonic()
    # The company's name makes the search find it: "ELMT" alone returned
    # nothing, "The Elmet Group Co. (ELMT)" its September news.
    from discord_bot import listed_symbols as _listed
    name = (args.get("name") or "").strip() or _listed.name(sym)
    name_part = f" ({name})" if name else ""
    moves: list[dict] = []
    if days == WINDOW_DAYS:
        prompt = PROMPT.format(sym=sym, name=name_part)
    else:
        from datetime import date, timedelta
        moves = await _largest_moves(sym, days)
        who = f"{name} ({sym}, US-listed)" if name else f"the US-listed stock {sym}"
        head = (MOVES_CLAUSE.format(who=who, moves="; ".join(
            f"{m['date']} {m['pct']:+.1f}%" for m in moves)) if moves else "Search the web for the")
        prompt = LONG_PROMPT.format(head=head, who=who,
                                    since=(date.today() - timedelta(days=days)).isoformat())
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        temperature=0.1, max_output_tokens=900,
        # A list of dated facts needs little reasoning. Budget 0 is
        # rejected (400) on gemini-3.5-flash-lite; MINIMAL is the floor
        # and was the fastest setting measured on 2026-09-30.
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
    )
    out: dict = {"status": "error", "symbol": sym, "error": "News search failed."}
    # A second try when the first fails or comes back with nothing dated,
    # as long as it can still finish inside the prefetch deadline. On
    # 2026-09-30 one search in two came back with no usable lines.
    for _attempt in range(NEWS_ATTEMPTS):
        left = NEWS_TIMEOUT_S - (time.monotonic() - t0)
        if left < 1.5:
            break
        try:
            resp = await asyncio.wait_for(client.aio.models.generate_content(
                model=settings.ask_gemini_model or settings.gemini_model,
                contents=prompt, config=config), left)
        except asyncio.TimeoutError:
            out = {"status": "timeout", "symbol": sym, "error": "News search did not finish in time."}
            break
        except Exception as e:
            log.warning(f"ticker news {sym}: {e}")
            out = {"status": "error", "symbol": sym, "error": "News search failed."}
            continue
        text = (getattr(resp, "text", None) or "").strip()
        cand = (getattr(resp, "candidates", None) or [None])[0]
        sources = _sources(getattr(cand, "grounding_metadata", None))
        # Unlinked lines count in the long window too: four live ELMT runs
        # on 2026-10-30 returned the stake, the contract and the Q2 results,
        # all correct and all without grounding metadata (see dated_lines).
        dated = dated_lines(text, days=days)
        if dated:
            # Links when the API returned them; the in-window dates are what
            # make the lines news rather than memory (see dated_lines).
            out = {"status": "ok", "symbol": sym, "digest": _clip("\n".join(dated)),
                   "sources": sources, "window_days": days}
            break
        out = {"status": "no_data", "symbol": sym,
               "error": f"No dated news on {sym} in the last {days} days."}
    if moves:
        out["moves"] = moves
    if out["status"] in ("ok", "no_data"):
        now = time.monotonic()
        for k, (ts, _) in list(_CACHE.items()):
            if now - ts >= CACHE_TTL_S:
                _CACHE.pop(k, None)
        # A miss is kept briefly: a release can cross minutes after it.
        # Stored with a back-dated stamp so the shared TTL check expires it.
        stamp = now if out["status"] == "ok" else now - (CACHE_TTL_S - MISS_TTL_S)
        _CACHE[key] = (stamp, out)
    return out


def largest_moves(hist: list[dict], n: int = MAX_MOVES) -> list[dict]:
    """The `n` largest close-to-close moves in `hist` (oldest first), by
    size, as [{date, pct}] newest first."""
    rows = [h for h in hist or [] if h.get("close") and h.get("date")]
    moves = [{"date": str(b["date"])[:10], "pct": round((b["close"] / a["close"] - 1) * 100, 1)}
             for a, b in zip(rows, rows[1:])]
    top = sorted(moves, key=lambda m: abs(m["pct"]), reverse=True)[:n]
    return sorted(top, key=lambda m: m["date"], reverse=True)


async def _largest_moves(sym: str, days: int) -> list[dict]:
    from datetime import date, timedelta
    from report import market_data as _md
    start = (date.today() - timedelta(days=days + 1)).isoformat()
    try:
        hist = await asyncio.wait_for(
            asyncio.to_thread(_md.fetch_price_history, sym, start, None, "1d"), HISTORY_TIMEOUT_S)
    except Exception as e:
        log.info(f"ticker news {sym}: no price history for the moves ({e})")
        return []
    return largest_moves(hist or [])


# "**2026-10-21 (+11.5%)**:" is how a move line came back bolded (2026-10-30)
_DATED_RE = re.compile(r"^\s*[-*•]?\s*(?:\*\*)?\(?(20\d\d-\d\d-\d\d)")


WINDOW_DAYS = 7


def dated_lines(text: str, today=None, days: int = WINDOW_DAYS) -> list[str]:
    """The lines that open with a date inside the last `days` (New
    York calendar). The model writes "NO RECENT NEWS" under one category
    while listing real items under another (NVDA, 2026-09-30, beside the
    $150B buyback), so the phrase anywhere cannot mean the reply is empty.
    The window is what makes a line trustworthy without a link: about half
    of the grounded replies on 2026-09-30 came back with no grounding
    metadata at all while carrying that evening's print, which only a
    search could supply; a line dated in the last week cannot come from
    the model's training data. The 90-day window rests on the same
    ground: its oldest date is still past what the model was trained on."""
    from datetime import date, datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).date()
    out = []
    for ln in (text or "").splitlines():
        m = _DATED_RE.match(ln)
        if not m or "NO RECENT NEWS" in ln.upper():
            continue
        try:
            d = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if today - timedelta(days=days) <= d <= today + timedelta(days=1):
            out.append(ln.strip())
    return out


def _clip(text: str, limit: int = DIGEST_CHARS) -> str:
    """Whole lines up to `limit`: a line cut mid-figure reads as a fact."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut if cut.strip() else text[:limit]
