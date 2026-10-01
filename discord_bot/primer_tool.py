"""The /ask business primer (2026-09-30): what a company sells and which part
of it drives the numbers, built once per ticker and kept 30 days.

Owner: "if a ticker is asked about in a question, no matter in the context
or earnings, or options, or in general, shouldn't there be some level of
information about what part of their business is driving a specific
metric? like gpu or cloud or retail business driving revenue and margins".
The bank notes name segments without explaining them ("SCA mix", "HBM4"),
Yahoo's summary lists segments without their weight, and the model had
nothing to tie a desk's figure to the business line behind it.

One Google-grounded Flash-Lite call builds the primer the first time a name
is asked about; it is stored in `ticker_primers` and reused. A build that
outlasts the prefetch deadline keeps running and is stored for the next
question. Prefetch only, never declared to the model. Spend ledger caller:
ask_primer.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

log = logging.getLogger(__name__)

BUILD_TIMEOUT_S = 25          # the build itself; the prefetch waits less
WAIT_S = 7.0                  # what one question waits for a cold build
# A build that came back unsourced every attempt is not retried for this
# long: each attempt is a grounded call, and a busy name would otherwise
# start three of them on every question.
FAILED_TTL_S = 6 * 3600
_client = None
_inflight: dict[str, asyncio.Task] = {}
_failed: dict[str, float] = {}

PROMPT = (
    "Today is {today}. Search the web, then describe the US-listed company {sym}{name} for a "
    "retail trader in plain English. Exactly these five lines, each starting with its label:\n"
    "SELLS: what it sells and to whom, in one sentence.\n"
    "SEGMENTS: its reported segments, named exactly as in its most recent 10-K or 10-Q "
    "(companies rename and regroup segments, so never use older names), with their "
    "approximate share of revenue in the latest fiscal year, each segment explained in a "
    "few words (say what the product is, e.g. 'HBM: high-bandwidth memory stacked beside "
    "AI chips'). If the segments are regions, also give the split it reports by product, "
    "service line or type of work (e.g. consulting vs managed services), named as the "
    "company names it.\n"
    "DRIVERS: which segment, product or service line drives revenue growth now and which "
    "carries the highest margin, and why, named as in the SEGMENTS line.\n"
    "WATCHED: the two or three figures investors judge it on at earnings, each "
    "explained.\n"
    "OUTSIDE: what moves it besides its own results (prices of what it sells or buys, "
    "a customer's spending, rates, regulation, a cycle).\n"
    "Facts only, no opinion, no price targets, no stock recommendation. Spell out "
    "every acronym the first time."
)


# Primers built before this were asked for "reported segments" with no
# date and no filing to anchor them; MU's came back with its pre-2025
# names ("Compute and Networking"). Older rows are rebuilt on next use.
MIN_BUILT_AT = "2026-10-01T15:40:00"    # UTC, after this change deploys


def _today_et() -> str:
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    return datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York")).strftime("%B %d, %Y")


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_primer", api_key=key)
    return _client


# The colon is required: "Outside the US, ..." is prose, not the label.
_LABEL_RE = re.compile(r"^[^A-Za-z]*(SELLS|SEGMENTS|DRIVERS|WATCHED|OUTSIDE)[\s*_]*:[\s*_]*(.+)$", re.I)


def _parse(text: str) -> str | None:
    """Keep the five labelled lines, whatever numbering or bold the model
    put around the label; None when fewer than three are there."""
    kept = []
    for ln in (text or "").splitlines():
        m = _LABEL_RE.match(ln.strip())
        if m and m.group(2).strip():
            kept.append(f"{m.group(1).upper()}: {m.group(2).strip().strip('*').strip()}")
    return "\n".join(kept) if len(kept) >= 3 else None


# The same prompt came back without a search 2 of 3 times for MU on
# 2026-09-30. Unsourced segment shares are not stored; the build runs in
# the background past the question's wait, so retries cost no latency.
BUILD_ATTEMPTS = 3


async def _build(sym: str, name: str) -> dict:
    import db
    from google.genai import types
    from config import settings
    from discord_bot.news_tool import _sources
    client = _get_client()
    if client is None:
        return {"status": "error", "symbol": sym, "error": "No Gemini key configured."}
    primer, sources = None, []
    for attempt in range(BUILD_ATTEMPTS):
        try:
            resp = await asyncio.wait_for(client.aio.models.generate_content(
                model=settings.ask_gemini_model or settings.gemini_model,
                contents=PROMPT.format(sym=sym, name=f" ({name})" if name else "",
                                       today=_today_et()),
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=0.1, max_output_tokens=1200,
                    thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                ),
            ), BUILD_TIMEOUT_S)
        except Exception as e:
            log.warning(f"primer {sym}: build attempt {attempt + 1} failed ({type(e).__name__}: {e})")
            continue
        cand = (getattr(resp, "candidates", None) or [None])[0]
        sources = _sources(getattr(cand, "grounding_metadata", None))
        primer = _parse(getattr(resp, "text", None) or "")
        if primer and sources:
            break
        log.info(f"primer {sym}: attempt {attempt + 1} unusable "
                 f"(labelled={bool(primer)}, sources={len(sources)})")
    if not primer or not sources:
        _failed[sym] = time.monotonic()
        return {"status": "no_data", "symbol": sym, "error": "No sourced business description."}
    try:
        await asyncio.to_thread(db.upsert_ticker_primer, sym, primer, sources)
    except Exception as e:
        log.warning(f"primer {sym}: store failed ({e})")
    return {"status": "ok", "symbol": sym, "primer": primer, "sources": sources}


async def _execute_ticker_primer(args: dict) -> dict:
    """Stored primer, or a fresh build. A view question waits up to WAIT_S
    for it; with `wait` false (a price or date lookup) the build starts in
    the background and the question does not wait. Either way the build
    continues and stores itself."""
    import db
    sym = (args.get("symbol") or "").strip().upper().lstrip("$")
    if not sym:
        return {"status": "error", "error": "No symbol provided."}
    try:
        stored = await asyncio.to_thread(db.get_ticker_primer, sym)
    except Exception as e:
        log.warning(f"primer {sym}: read failed ({e})")
        stored = None
    if stored and str(stored.get("built_at") or "").replace(" ", "T") >= MIN_BUILT_AT:
        return {"status": "ok", **stored}
    failed_at = _failed.get(sym)
    if failed_at is not None and time.monotonic() - failed_at < FAILED_TTL_S:
        return {"status": "no_data", "symbol": sym, "error": "No sourced business description."}
    task = _inflight.get(sym)
    if task is None or task.done():
        task = asyncio.ensure_future(_build(sym, (args.get("name") or "").strip()))
        _inflight[sym] = task

        def _forget(t, s=sym):
            # Only this task: a newer build for the symbol may already be
            # registered by the time this callback runs.
            if _inflight.get(s) is t:
                _inflight.pop(s, None)
        task.add_done_callback(_forget)
    if args.get("wait") is False:
        return {"status": "building", "symbol": sym}
    try:
        return await asyncio.wait_for(asyncio.shield(task), WAIT_S)
    except asyncio.TimeoutError:
        return {"status": "timeout", "symbol": sym,
                "error": "Business description is still being built; answer without it."}
