"""Room rankings: which ones have data, and what to do with the rest (2026-10-02).

The bot measures two things about members: trading results and racism
(race-edged messages and slurs). `lookup_user_profile` ranks by either.
On 2026-10-01 Ligma asked "who's the most gay in chat?", got a dodge,
replied "Gimmie top 5, who has the crown", and the model answered with
the racism leaderboard, BK first, with counts and an Evidence block. It
substituted the only edgy metric it had for one nobody asked about.

Two rules, enforced here in code:

1. A metric ranking runs only when the conversation asked for that
   metric. `gate` refuses a racism or trader call when the asker's
   message and the message they replied to never mention it, and the
   refusal tells the model to answer as a roast instead.
2. A "who's the most <trait>" question about a trait the bot does not
   measure is answered by picking real people from their profiles and
   recent chat (owner, 2026-10-02: "just come up with something clever
   you have plenty of context on people"). `superlative_note` is the
   block that says so, appended to the prompt in phase 2.
"""
from __future__ import annotations

import re

METRIC_NOT_ASKED = "metric_not_asked"

_RACISM_RE = re.compile(
    r"racis|\bslurs?\b|n[\s-]?word|\bbigot|race[\s-]?edged|hard[\s-]?r\b",
    re.I)
_TRADER_RE = re.compile(
    r"trad(?:e|er|ers|es|ing)\b|p\s?&\s?l|\bpnl\b|profit|\bgains?\b|\blos(?:s|ses|ing|t)\b"
    r"|\bwin(?:s|ning)?\b|win\s?rate|\brecord\b|portfolio|\breturns?\b|printing"
    r"|\bplays?\b|\bcalls?\b|\bputs?\b|options?\b|\bbags?\b|\bmoney\b|\bw/l\b"
    r"|up the most|down the most|blown? up|\bacct\b|account|\blosers?\b",
    re.I)
_METRIC_RES = {"racism": _RACISM_RE, "trader": _TRADER_RE}

# "who's the most gay", "who is the biggest simp in chat", "who's the
# least funny here". The trait is the words after the superlative, up to
# a place word or the end of the clause.
_SUPERLATIVE_RE = re.compile(
    r"\bwho(?:'s|’s|s| is| are)\s+(?:the\s+)?(?:most|biggest|least|best|worst)\s+"
    r"(?P<trait>[a-z][a-z' -]{0,30}?)\s*(?=\bin\b|\bhere\b|\bof\b|\bon\b|[?!.,]|$)",
    re.I)


def asker_message(question: str) -> str:
    """The asker's own words: for a reply, the part after the
    '[X's message to you]' line; otherwise the whole question."""
    q = question or ""
    m = re.search(r"message to you\]\s*\n", q)
    return q[m.end():] if m else q


def asked_metrics(question: str) -> set[str]:
    """Metrics the conversation asked about, read from the asker's message
    and the message they replied to (a follow-up on a racism board such as
    'who's #6' quotes that board)."""
    # Quoted member messages ('[VERBATIM RECENT MESSAGES ...]') are left
    # out: a slur someone else typed is not the asker asking for a board.
    q = re.sub(r"\[VERBATIM RECENT MESSAGES[^\]]*\].*?(?=\n\[|\Z)", " ",
               question or "", flags=re.S)
    return {name for name, rx in _METRIC_RES.items() if rx.search(q)}


def gate(args: dict, question: str) -> dict | None:
    """None when the call may run. Otherwise the tool result to return
    in its place. Only metric calls are gated: a lookup by username
    returns that member's own ranks and is never a substitution."""
    metric = str((args or {}).get("metric") or "").strip().lower()
    if not metric or (args or {}).get("username"):
        return None
    if metric in asked_metrics(question):
        return None
    return {
        "status": METRIC_NOT_ASKED,
        "users": [],
        "error": (
            f"Nobody asked for a {metric} ranking, so this data does not "
            "answer the question. The bot only measures trading results and "
            "racism. Whatever the asker wants ranked, answer it as a roast: "
            "name real people from the room (WHO'S TALKING profiles, recent "
            "chat) and give each a specific reason from their own profile or "
            "messages. Commit to picks. No counts, percentages, ranks or "
            "evidence lines, and do not call lookup_user_profile with a "
            "metric again for this question."
        ),
    }


def superlative_trait(question: str) -> str | None:
    """The trait in a 'who's the most <trait>' question when it is not one
    the bot measures. None for anything else, including 'who's the best
    trader' and 'who's the most racist', which have real rankings."""
    m = _SUPERLATIVE_RE.search(asker_message(question))
    if not m:
        return None
    trait = m.group("trait").strip(" '")
    if not trait or any(rx.search(trait) for rx in _METRIC_RES.values()):
        return None
    return trait


def superlative_note(question: str) -> str:
    """The prompt block for an unmeasured superlative, or '' when the
    question is not one."""
    trait = superlative_trait(question)
    if not trait:
        return ""
    return (
        f'ROOM SUPERLATIVE: the asker wants the room\'s most "{trait}". The '
        "racism and trader rankings do not measure that, so do not use "
        "them. Pick real people from WHO'S TALKING and the recent chat, by "
        "name, with a specific reason each from their own profile or "
        "messages, and commit to a winner. A count is allowed only if you "
        "computed it this turn from a chat search."
    )
