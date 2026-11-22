"""A model's reading of a question the regex router could not place
(2026-11-22, owner: "we need to figure the parsing").

The router reads a question with patterns. Each new phrasing is a new
pattern: on 2026-11-21 alone 34 logged routes were wrong and were fixed one
pattern at a time ("has earnings" read as Hasbro, "amd,qcom,arm" read as ARM
alone, "when are MU earnings" missing the earnings shape). So the question
was measured: one structured Flash-Lite call (a closed list of kinds, the
tickers, twelve words on what is asked) over all 969 logged questions since
09-01, beside the router. They disagreed on 422. Each was judged blind by
two independent reviewers, the systems shuffled per item. Where the
reviewers agree:

- the kind: model right 218, router right 68 (about 91% against 73% overall);
- the tickers: model right 139, router right 21 (about 97% against 84%).

The model's reading is better, with one error of its own: it names a kind
for a statement or a reaction ("Hard name to play", "Followed. 3.4 fill"),
which would put a stock rundown under a passing remark. When the asker's
words are a question or request it was right far more often than not, and
when they are not it was a coin flip. Read a second time, one question in
ten got a different kind.

So the reading is used where the measurement supports it, and nowhere else:

1. The router could not place the question (the catch-all), the asker's
   words are a question or request, and the model names one of the kinds it
   was right on at least four times in five (`UNPLACED_KINDS`).
2. The router fell back to "a view on the stock" only because a ticker was
   named and no pattern matched. When the model named another kind there it
   was right 41 times to 1 (`VIEW_KINDS`).

The router keeps every question it recognises. The model only says what kind
of question it is and which tickers it names: code checks the tickers
against the listed symbols and the message itself, builds the same data
plan the router builds for that kind (`ask_router.apply_reading`), and
posts the lines as before. Any failure of the call leaves the router's
route as it was.

Cost and speed, measured on the worker over the 969: no error and no
filter block, 0.74 s median (0.94 s at the 99th percentile), $0.00035 a
question. For a catch-all question it runs beside the flag classifier that
already ran there, so it adds no wait. Spend is recorded as `ask_reader`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 2.5
_client = None

KINDS = ["earnings_slate", "earnings_date", "price", "options_chain", "econ_calendar", "price_history",
         "company_profile", "member_ledger", "chat_history", "fantasy", "historical_stat", "news_event",
         "ticker_opinion", "room_crowding", "banter", "other"]
# Kinds the model may give a question the router could not place: right at
# least four times in five on the labelled set, with five or more cases
# (news_event 37 to 3, member_ledger 12 to 1, chat_history 9 to 0,
# earnings_date 9 to 0, price 7 to 1, price_history 5 to 0, company_profile
# 4 to 1). Left with the catch-all: fantasy (12 to 7), a stock view (6 to
# 3), the economic calendar (3 to 3), a historical statistic (4 to 4).
UNPLACED_KINDS = frozenset({"news_event", "member_ledger", "chat_history", "earnings_date", "price",
                            "price_history", "company_profile"})
# Kinds the model may give in place of the router's fallback view on a
# stock (18 to 0 for news_event, 4 to 0 earnings_date, 3 to 1 price, 3 to 0
# company_profile, 3 to 0 member_ledger), and "not a question about the
# stock at all" (7 to 0), which gets the price and nothing else.
VIEW_KINDS = frozenset({"news_event", "earnings_date", "price", "company_profile", "member_ledger"})
NOT_A_QUESTION = frozenset({"banter", "other"})

INSTRUCTION = """You read one message sent to a bot in a Discord room of options and crypto traders, and say what it asks for. Context blocks in square brackets (a message being replied to, earlier messages) may come first: the asker's own words are the last lines. Reply with JSON only.

shape, exactly one of:
- earnings_slate: which companies report earnings on a day or in a week (a list is wanted).
- earnings_date: about the earnings report of one or several NAMED companies: when it is, what time, what is expected, or how it went (beat, miss, results).
- price: the current price or quote of something named, or one figure of a named company (market cap, float, shares outstanding, short interest, cash, debt, a valuation multiple).
- options_chain: options on a named ticker: implied move, implied volatility, a chain, a strike, open interest.
- econ_calendar: economic releases and events (CPI, jobs report, FOMC, PCE, PPI, ISM, GDP, Treasury auctions): when, what is expected, or what printed.
- price_history: the past price behaviour of something named: its change over a period, a moving average, a high or low, a return.
- company_profile: what a named company is or does.
- member_ledger: a room member's or caller's trades, record, win rate, profit and loss or positions.
- chat_history: what was said in the room: counting messages or words, who said what, a recap of the chat, ranking members by what they post.
- fantasy: the room's fantasy football league, or NFL players and games.
- historical_stat: a historical or statistical market fact that is not one named security's own price series (seasonality, how often something happens, what markets did after past events).
- news_event: why something moved, what happened to a company or the market, news and current events, the odds of an event, or a current fact that needs a fresh source.
- ticker_opinion: a view or general question on one named stock (thoughts, buy or sell, the setup) that none of the shapes above covers.
- room_crowding: what the room as a whole is positioned in.
- banter: a joke, an insult, a roast request, a greeting, talk about people, or chat with no factual request.
- other: a factual or task request that fits none of the above (general knowledge, arithmetic, writing, how-to).

tickers: the US-listed stock or fund symbols, index symbols (SPX, NDX, VIX) or coins (BTC, ETH, SOL) the asker's own words are about, in capitals, in the order named. A company named in words becomes its symbol (Nvidia is NVDA). Correct a misspelt ticker only when the sentence leaves no doubt. An ordinary word is never a ticker. Empty when none.

asked: at most 12 words saying what the asker wants."""
SCHEMA = {"type": "OBJECT", "properties": {
    "shape": {"type": "STRING", "enum": KINDS},
    "tickers": {"type": "ARRAY", "items": {"type": "STRING"}},
    "asked": {"type": "STRING"}}, "required": ["shape", "tickers", "asked"]}

# The asker's words are a question or a request. A statement or a reaction
# is not, whatever it mentions.
_ASKS_RE = re.compile(
    r"\?|^\s*(?:what|whats|when|whens|where|who|whos|why|how|hows|which|is|are|was|were|did|does|do|can|could"
    r"|should|will|would|give|show|tell|list|name|explain|summari[sz]e|recap|rate|compare|find|check"
    r"|calculate|tldr)\b", re.I)


def asks(asked: str) -> bool:
    return bool(_ASKS_RE.search(asked or ""))


def _get_client():
    global _client
    if _client is None:
        from config import settings
        from ai_analysis.usage_ledger import make_client
        key = settings.google_ask_api_key or settings.google_api_key
        if not key:
            return None
        _client = make_client("ask_reader", api_key=key)
    return _client


def parse(raw: str) -> dict | None:
    """The call's JSON as {kind, tickers, asked}, or None when it is not
    the form asked for."""
    try:
        out = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(out, dict) or out.get("shape") not in KINDS:
        return None
    # letters only: one reading came back as "CB RS"
    tickers = ["".join(c for c in str(t).upper() if c.isalnum() or c == ".") for t in out.get("tickers") or []]
    tickers = [t for t in tickers if t]
    return {"kind": out["shape"], "tickers": tickers[:6], "asked": str(out.get("asked") or "")[:120]}


async def read(question: str, channel_name: str = "", safety_settings=None) -> dict | None:
    """One reading of the question, or None on any failure or a slow
    reply: the router's own route then stands."""
    from google.genai import types
    from config import settings
    client = _get_client()
    if client is None or not (question or "").strip():
        return None
    text = f"[channel: #{channel_name}]\n" + question.strip()[-1800:]
    config = types.GenerateContentConfig(
        system_instruction=INSTRUCTION, safety_settings=safety_settings, temperature=0.0,
        max_output_tokens=200, response_mime_type="application/json", response_schema=SCHEMA,
        thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"))
    try:
        resp = await asyncio.wait_for(client.aio.models.generate_content(
            model=settings.ask_gemini_model or settings.gemini_model, contents=text, config=config),
            READ_TIMEOUT_S)
    except Exception as e:
        log.info(f"/ask reader: no reading ({type(e).__name__}); the router's route stands")
        return None
    return parse((getattr(resp, "text", None) or "").strip())
