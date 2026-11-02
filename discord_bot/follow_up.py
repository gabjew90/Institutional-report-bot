"""A reply to the bot's own answer, sorted in code (2026-11-02, owner).

Replies are 43% of /ask (132 of 308 questions, 30 days to 11-02), and an answer
to one was corrected 12.1% of the time against 4.5% for a first question. The
router saw a reply as a new question with the answer's text beside it. Of 24
replies under a one-stock answer, 13 used no stock data, because the ticker was
looked for in the answer's text and is often not there ("**Apple AAPL**", or a
line that only says "trailing 12-month free cash flow"). The NBIS conversation
of 10-21 went three rounds: "give me a guess on forward pe", then "you can't
even come up with a number" got a roast about the asker's short.

So each answer's row in ask_bot_answers now records what it was about (`turn`),
and a reply is one of three things:

- a reaction ("gracias", "lol"): routed as before, with the stock's price.
- a question: routed as if it named the stock, through the normal shapes.
- a complaint about a factual answer: the original question is asked again,
  in the factual register, with the data. Owner: data only, no jab. A pure
  insult is not a complaint and keeps the room's register.

Pure: regex and small helpers, no db and no network.
"""
from __future__ import annotations

import re

REACTION = "reaction"
QUESTION = "question"
COMPLAINT = "complaint"

_ASKED_MAX = 300

# Written from 316 hand-labelled replies to the bot (2026-09-01 to 10-26). On
# those it catches 44 of 50 corrections with 3 false alarms. The whole pattern
# is the outcome measure in scripts/ask_outcomes.py. Routing leaves out what the
# asker says about themselves: asking the question again does not answer "I
# never trade that", and "I'm not a [insult]" is an insult (the first live one,
# 11-02, got a full FSLR answer).
_ABOUT_ASKER = (
    r"\bi\s+(?:don'?t|do\s+not|never)\s+(?:even\s+)?(?:trade|own|hold|play|use)\b|\bnot\s+mine\b"
    r"|\bi'?m\s+not\s+a\b|\bonly\s+in\s+one\b|\bi\s+don'?t\s+think\s+i'?ve\s+ever\b")
_ABOUT_ANSWER = (
    # says it is wrong
    r"\b(?:wrong|inaccurate|incorrect|not\s+true|that'?s\s+a\s+lie|fake\s+news|nice\s+try)\b"
    r"|^\s*(?:no|nah|nope)\b"
    # left something out
    r"|\b(?:you|your|u)\s+(?:forgot|missed|left\s+out|didn'?t\s+(?:show|include|list|answer|mention))\b"
    r"|\b(?:forgot|missed)\s+\$?[a-z]{2,6}\b"
    # answered something else
    r"|\bi\s+(?:said|meant|mean|asked)\b|\bi'?m\s+asking\b|\bi\s+am\s+asking\b|\bwhy\s+would\s+i\s+care\b"
    # contradicts itself or the tape
    r"|\byou\s+(?:just\s+)?said\b|\balready\s+(?:live|out|over|happened|reported|printed)\b"
    r"|\bcheck\s+the\s+(?:stock|chart|price|tape)\b|\bget\s+your\s+\w+\s+right\b"
    r"|\bseems?\s+(?:low|high|off)\b|\bhow\s+is\s+(?:it|\w+\s+a)\b|\bis\s+owned\s+by\b"
    r"|\bis\s+playing\s+(?:tonight|today|tomorrow)\b"
    # did not do the job
    r"|\btoo\s+long\b|\bbe\s+more\s+specific\b|\banswer\s+the\s+question\b|\btask\s+incomplete\b"
    r"|\b(?:do|try)\s+(?:it\s+|that\s+|this\s+|analysis\s+)?again\b|\bdo\s+better\b|\bresults\s+only\b"
    r"|\bcan'?t\s+even\b|\bthis\s+list\s+(?:blows|sucks)\b|\buseless\b")
_CORRECTION_RE = re.compile(_ABOUT_ANSWER + "|" + _ABOUT_ASKER, re.I)
_COMPLAINT_RE = re.compile(_ABOUT_ANSWER, re.I)
_MENTION_RE = re.compile(r"<@!?\d+>|@[\w.]+(?:\s+\([\w.]+\))?")
_REACTION_RE = re.compile(
    r"^(?:thanks?(?:\s+you)?|thx|ty|gracias|lol|lmao|lmfao|nice|cool|ok(?:ay)?|bet|damn|wow|wtf|fr|facts"
    r"|true|yep|yes|yeah|sick|cheap|good\s+bot|ah\s+fuck|fuck|shit|no\s+shit)\W*$", re.I)
_ASKS_RE = re.compile(
    r"^\s*(?:what|what'?s|whats|when|when'?s|where|which|who|why|how|is|are|does|did|do|can|could|will"
    r"|would|should|has|have|was|were|give\s+me|show\s+me|tell\s+me|pull|compare|guess)\b", re.I)
# An insult that opens like a question ("why are you so dumb") asks nothing.
_INSULT_RE = re.compile(
    r"\b(?:idiot|dumb\w*|stupid|moron|retard\w*|nigg\w*|clank\w*|cunt|bitch|pussy|fuck\s+you)\b", re.I)
# A bare "no" or "nah, thanks" is not a complaint about the answer. The outcome
# measure counts them, but they do not earn the question being asked again.
_SOFT_NO_RE = re.compile(r"^\s*(?:no|nah|nope)\b[\s,.!]*(?P<rest>.*)$", re.I | re.S)
_SATISFIED_RE = re.compile(r"\b(?:thanks?|thx|fine|good|ok(?:ay)?|cool|worries)\b", re.I)
# The reply carries on from the answer: a word pointing back at it, or a next
# step in the position.
_POINTS_BACK_RE = re.compile(
    r"\b(?:it|its|it'?s|they|they'?re|them|their|that|this|these|those|the\s+stock|the\s+company"
    r"|how\s+about|what\s+about|close|sell|hold|trim|add)\b", re.I)
_NOT_POINTING_RE = re.compile(r"\bwhat\s+time\s+is\s+it\b", re.I)
# Another market, which no ticker names: "whats bitcoin at", "is the market open"
_OTHER_SUBJECT_RE = re.compile(
    r"\b(?:bitcoin|btc|ethereum|eth|solana|crypto|oil|crude|brent|gold|silver|dollar|dxy|yields?"
    r"|treasur\w+|bonds?|the\s+fed|fomc|powell|warsh|cpi|pce|ppi|jobs\s+report|nfp|s&p|spx|nasdaq"
    r"|dow|vix|futures|(?:the\s+|stock\s+)market(?!\s+cap))\b", re.I)
_TICKER_TOOLS = {"lookup_market_price", "lookup_ticker_snapshot", "lookup_earnings_date",
                 "lookup_research", "lookup_options_chain", "lookup_price_history",
                 "event_returns"}
# not "^GSPC": an index is no stock to carry into the next question
_SYMBOL_RE = re.compile(r"(?<![\^\w])[A-Z][A-Z.\-]{0,9}\b")


def is_correction(text: str) -> bool:
    return bool(_CORRECTION_RE.search(text or ""))


def kind(own: str, data_words: bool = False) -> str:
    """What the asker's own words are. `data_words`: they name something a
    stock's data can answer (the router's stock_intent)."""
    text = _MENTION_RE.sub(" ", own or "").strip()
    if not text or _REACTION_RE.match(text):
        return REACTION
    soft = _SOFT_NO_RE.match(text)
    if _COMPLAINT_RE.search(text) and not (soft and (
            len(soft.group("rest").split()) < 3 or _SATISFIED_RE.search(soft.group("rest")))
            and not _COMPLAINT_RE.search(soft.group("rest"))):
        return COMPLAINT
    if data_words:
        return QUESTION
    if _INSULT_RE.search(text):
        return REACTION
    return QUESTION if "?" in text or _ASKS_RE.match(text) else REACTION


def carries_on(own: str, data_words: bool = False) -> bool:
    """The reply is about what the answer was about, not some other subject.
    "who won the game last night?" under an Apple answer is not about Apple."""
    text = own or ""
    if _OTHER_SUBJECT_RE.search(text) or _NOT_POINTING_RE.search(text):
        return False
    return data_words or bool(_POINTS_BACK_RE.search(text))


def subject(turn: dict | None) -> str:
    """The one stock a factual answer was about, or ''. Two names are not one
    subject, and a banter answer's capitals are no subject at all."""
    if not turn or not turn.get("factual"):
        return ""
    tickers = turn.get("tickers") or []
    return tickers[0] if len(tickers) == 1 else ""


def record(own: str, meta: dict, trace: list | None) -> dict:
    """What an answer was about, stored with it. The stocks are the ones a data
    tool returned for this turn: the router's tickers are guesses ("TOO", "ACT"
    and "ORDER" in the 11-02 replay of a month of replies) until a tool has
    answered for them. `asked` is the root question: a complaint's turn keeps
    the question being complained about, so a third round still knows it."""
    tickers: list[str] = []
    for t in trace or []:
        if not isinstance(t, dict) or t.get("tool") not in _TICKER_TOOLS or t.get("status") != "ok":
            continue
        args = t.get("args") or {}
        for sym in _SYMBOL_RE.findall(str(args.get("symbol") or args.get("symbols") or "").upper()):
            if sym not in tickers:
                tickers.append(sym)
    shape = meta.get("route_shape") or ""
    return {"shape": shape,
            "tickers": tickers[:6],
            "asked": (meta.get("route_reask") or own or "").strip()[:_ASKED_MAX],
            # a stock view runs in the banter register and is still a data answer
            "factual": meta.get("kind") == "FACT" or shape not in ("", "unknown", "banter")}


def question_note(symbol: str, asked: str) -> str:
    return (f"FOLLOW-UP: the asker is replying to your answer about ${symbol}"
            + (f' (they had asked: "{asked}")' if asked else "")
            + ". Answer what the reply asks, and lead with that figure or fact from the data "
            "blocks. Do not repeat the rest of your earlier answer. If the reply is about "
            "something else, answer that and leave the stock out.")


def complaint_note(asked: str) -> str:
    return ("COMPLAINT ABOUT YOUR LAST ANSWER: the asker's reply says it was wrong, incomplete or "
            "did not answer"
            + (f'. Their question was: "{asked}"' if asked else "")
            + (". Answer that question now from the data blocks and deal with what the reply says. "
               if asked else ". Answer what the reply says from the data blocks. ")
            +
            "Give the figure. If the figure does not exist, say why in one line and give the "
            "closest figure that does. If the reply states a fact, check it against the data "
            "before agreeing with it. Facts only: nothing about the asker, no apology, no "
            "'fair point'.")
