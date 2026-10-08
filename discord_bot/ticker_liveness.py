"""Tickers an answer introduces must still trade (2026-10-08 audit).

On 2026-10-06 the bot answered "what are we shorting" with GMS (bought by
Home Depot in 2025) and BECN (bought by QXO in 2025), from memory. The
room caught GMS; BECN went unflagged. Every stock ticker the answer names
that the asker did not is priced once; a symbol with no quote anywhere is
not a live listing, and the lines that name it are dropped.
"""
from __future__ import annotations

import re

MAX_CHECKED = 5


# Only tokens written AS tickers: a cashtag, `TICK` or (TICK). A bare
# capitalised word is as likely an acronym (DRAM, GPU, TAM) with no quote.
_MARKED_RE = re.compile(r"\$([A-Z]{1,5})\b|`([A-Z]{1,5})`|\(([A-Z]{1,5})\)")
# A line that explains why a name no longer trades stays.
_GONE_WORDS_RE = re.compile(
    r"\b(?:acquired|bought|merged|delisted|taken\s+private|went\s+private)\b", re.I)


def introduced_tickers(answer: str, question: str) -> list[str]:
    """Stock tickers in the answer that the question did not name."""
    from discord_bot import ask_router as R
    asked = {t.upper() for t in R.extract_tickers(question or "", all_tiers=True)}
    out = []
    for m in _MARKED_RE.finditer(answer or ""):
        t = (m.group(1) or m.group(2) or m.group(3)).upper()
        if t in asked or t in out or not R.is_stock(t) or t.lower() in R._COMMON_WORDS:
            continue
        out.append(t)
    return out[:MAX_CHECKED]


def dead(quotes: list[dict]) -> list[str]:
    """Symbols the price tool could not find on any feed."""
    return [q.get("symbol") for q in quotes or []
            if isinstance(q, dict) and q.get("error") and "no live feed" in str(q["error"])]


def drop_lines(answer: str, symbols: list[str]) -> str:
    """The answer without the lines that name a dead symbol. Unchanged if
    that would leave nothing."""
    if not symbols:
        return answer
    rx = re.compile(r"(?<![A-Za-z])\$?(?:" + "|".join(map(re.escape, symbols)) + r")(?![A-Za-z])")
    kept = [ln for ln in (answer or "").split("\n")
            if not rx.search(ln) or _GONE_WORDS_RE.search(ln)]
    body = "\n".join(kept).strip()
    return body if body else answer


async def guard(answer: str, question: str, price_fn) -> tuple[str, list[str]]:
    """(answer, dropped symbols). `price_fn` is the lookup_market_price
    executor. Any failure leaves the answer as it was."""
    syms = introduced_tickers(answer, question)
    if not syms:
        return answer, []
    try:
        res = await price_fn({"symbols": syms})
    except Exception:
        return answer, []
    gone = dead((res or {}).get("quotes") or [])
    # every symbol failing is the feed being down, not every name delisted
    if not gone or (len(syms) > 1 and len(gone) == len(syms)):
        return answer, []
    new = drop_lines(answer, gone)
    return (new, gone) if new != answer else (answer, [])
