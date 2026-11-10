"""Tickers an answer introduces must still trade (2026-10-08 audit).

On 2026-10-06 the bot answered "what are we shorting" with GMS (bought by
Home Depot in 2025) and BECN (bought by QXO in 2025), from memory. The
room caught GMS; BECN went unflagged. Every stock ticker the answer names
that the asker did not is priced once; a symbol with no quote anywhere is
not a live listing, and the lines that name it are dropped.

Only where the answer puts the name forward to trade (2026-11-10): the
asker asked for names ("give me tickers", "gun and ammo public stocks"),
or the symbol's own line makes a trade call ("Short Beacon Roofing
(BECN)"). Of the check's six fires to that date, the one right one (VSTO,
2026-11-04) was a request for names; the five wrong ones were terms inside
an explanation (ASI, WFE, TTM twice, CRAH) on questions that asked for none.
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


# The asker asked for names to trade or look at.
_NAMES_ASKED_RE = re.compile(
    r"\b(?:tickers?|stocks?|names|plays?|picks?|ideas?|shorts?|shorting|longs|buy|sell"
    r"|calls|puts|companies|equities|etfs?)\b", re.I)
# A line that puts a symbol forward as a trade. "sell-off", "sell-side",
# "long-term", "short-term" and "short interest" describe, they do not call.
_TRADE_LINE_RE = re.compile(
    r"\b(?:short(?:s|ing)?(?![-\s](?:term|interest|squeeze))|buy(?:ing)?"
    r"|sell(?!-?(?:off|side))|long(?![-\s]term)|calls|puts)\b", re.I)


def _line_of(text: str, m: re.Match) -> str:
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    return text[start:end if end != -1 else len(text)]


_MARKET_WORDS_RE = re.compile(
    r"\$|%|\b(?:short|long|buy|sell|calls?|puts?|stock|shares?|price|trade[sd]?|trading"
    r"|position|ticker|earnings|bullish|bearish|target|upside|downside)\b", re.I)


def _defines_acronym(answer: str, m: re.Match, acr: str) -> bool:
    """True for "Artificial Superintelligence (ASI)": the parenthesis
    follows its own spelled-out phrase (at least half the letters are the
    initials of the words before it, the first one included) on a line
    with no trading words. 2026-10-10 audit: the superintelligence half of
    an answer was dropped as a dead ticker. "Beacon Roofing (BECN)" is not
    a definition (one initial of four), and a trading line is always
    checked."""
    start = answer.rfind("\n", 0, m.start()) + 1
    line = _line_of(answer, m)
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", answer[start:m.start()])[-(len(acr) + 2):]
    # a name that carries the symbol itself is a company and its ticker
    # ("GMS Supply (GMS)"), never a definition
    if any(w.upper() == acr for w in words):
        return False
    # Every letter is the initial of the lowercase words just before it: a
    # definition even on a trading line ("wafer fabrication equipment (WFE)"
    # in a line about the stock selling off, 2026-10-18 audit A17). A
    # capitalised name ("First Republic Corp (FRC)") is a company.
    phrase = words[-len(acr):]
    if (len(words) >= len(acr) >= 2 and "".join(w[0].upper() for w in phrase) == acr
            and all(w[0].islower() for w in phrase)):
        return True
    if _MARKET_WORDS_RE.search(line):
        return False
    initials = [w[0].upper() for w in words]
    if len(words) < 2 or acr[0] not in initials:
        return False
    i = initials.index(acr[0])
    hits, j = 0, i
    for ch in acr:
        while j < len(initials) and initials[j] != ch:
            j += 1
        if j < len(initials):
            hits += 1
            j += 1
    return hits * 2 >= len(acr)


def introduced_tickers(answer: str, question: str) -> list[str]:
    """Stock tickers in the answer that the question did not name, where
    the answer puts them forward to trade (module docstring)."""
    from discord_bot import ask_router as R
    asked = {t.upper() for t in R.extract_tickers(question or "", all_tiers=True)}
    names_asked = bool(_NAMES_ASKED_RE.search(R.asker_text(question or "")))
    out = []
    for m in _MARKED_RE.finditer(answer or ""):
        t = (m.group(1) or m.group(2) or m.group(3)).upper()
        if t in asked or t in out or not R.is_stock(t) or R.not_a_ticker(t):
            continue
        if not names_asked and not _TRADE_LINE_RE.search(_line_of(answer, m)):
            continue
        if m.group(3) and _defines_acronym(answer, m, t):
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
