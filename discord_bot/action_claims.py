"""The bot cannot schedule anything or message anyone later (2026-10-08 audit).

On 2026-10-07 BK asked "remind me to ping Wock in 2h13m". The bot said the
"stopwatch is running", and when BK came back with "you never sent me a
reminder" it answered "consider wock officially pinged". Nothing was ever
sent. The bot answers when asked and has no reminder, timer or messaging
tool, so a request for one gets the honest answer in code, and an answer
that claims such an action is replaced with it.
"""
from __future__ import annotations

import re

HONEST_LINE = ("→ I can't set reminders, run timers or ping anyone later: I only answer "
               "when someone asks me. Set a reminder on your phone and tag them yourself.")

# The asker is talking about reminders, pings or timers at all.
_TOPIC_RE = re.compile(
    r"\b(?:remind(?:er|ers|ed|ing)?|ping(?:ed|ing)?|timer|alarm|stopwatch|countdown"
    r"|notify|alert\s+(?:me|him|her|them))\b", re.I)
# ...and is asking the bot to do it.
# Imperative only, at the start of the message: "why did BK ping me in
# general" asks about a past ping, and "remind me when NVDA reports" asks
# for a date.
_REQUEST_RE = re.compile(
    r"^\s*(?:<@!?\d+>\s*)?(?:(?:hey\s+)?(?:bot|omniwiz)[,:]?\s+)?"
    r"(?:(?:pls|please|yo)\s+)?(?:(?:can|could|will|would)\s+(?:you|u)\s+)?(?:pls\s+|please\s+)?"
    r"(?:remind\s+(?:me|us|him|her|them|<@!?\d+>|@?\w+)\s+(?:to|in|at|about|tomorrow|tonight)"
    r"|set\s+(?:a|an|the|up\s+a)?\s*(?:reminder|timer|alarm)"
    r"|(?:ping|tag|message|dm|notify|alert)\s+(?:me|us|him|her|them|<@!?\d+>|@?\w+)\s+"
    r"(?:in|at|when|tomorrow|tonight|later|after))\b", re.I)
# First-person or bot-voice claims of having done, or being about to do, it.
_CLAIM_RE = re.compile(
    r"\b(?:i(?:'ve|\s+have)?\s+(?:pinged|sent|set|scheduled|messaged|dm'?d|tagged|notified|reminded)"
    r"|i(?:'ll|\s+will)\s+(?:remind|ping|message|dm|tag|notify|hit\s+you)"
    r"|(?:officially|already)\s+pinged|consider\s+\S+\s+(?:officially\s+)?pinged"
    r"|reminder(?:'s|\s+is)?\s+(?:set|locked|scheduled|on)|(?:timer|stopwatch|clock|countdown)"
    r"(?:'s|\s+is)\s+(?:running|set|ticking|started)|locked\s+in\s+for)\b", re.I)


def is_reminder_request(asker_words: str) -> bool:
    return bool(_REQUEST_RE.search(asker_words or ""))


def guard(answer: str, asker_words: str) -> tuple[str, bool]:
    """(answer, replaced). Replaced when the asker asked for a reminder or
    ping, or the conversation is about one and the answer claims to have
    done it."""
    words = asker_words or ""
    if not _TOPIC_RE.search(words):
        return answer, False
    if is_reminder_request(words) or _CLAIM_RE.search(answer or ""):
        return HONEST_LINE, True
    return answer, False
