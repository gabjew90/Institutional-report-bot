"""Room corrections of /ask answers, read from the ask logs (2026-10-28).

The scoreboard's other numbers count repair work (checks that fired, words a
check changed). This module measures the outcome instead: how often the room
told the bot an answer was wrong, and which answer.

Two signals, both from the ask logs alone:

- corrected: a reply to the bot's answer that says it was wrong, incomplete
  or answered something else ("you forgot spcx", "GMS is owned by home
  depot", "I said gay not racist", "it's already live", "be more specific").
  The reply quotes the answer as its parent, so the correction is attached to
  the answer it replied to, whoever asked it.
- re-asked: the same asker puts the same question again in the same channel
  within 10 minutes without replying, which means the first answer did not
  land. Keyed by the logged username (the logs carry no author_id), which is
  stable over a 10-minute window.

`is_correction` is a deterministic pattern on the asker's own words. Insults
and banter under an answer are not corrections. It was written from 316
hand-labelled replies to the bot (2026-09-01 to 10-26), and on those it
catches 44 of 50 corrections with 3 false alarms. That is the set it was
tuned on, so treat it as an upper bound and re-label a fresh month before
trusting a small change in the rate.

The ends of the window are soft: an answer just before it cannot be
credited with a correction inside it (counted as unplaced), and an answer
near its end may be corrected after it. Read the most recent days with that
in mind.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from discord_bot import ask_router

BOT_NAMES = ("omniwiz",)
REASK_WINDOW = timedelta(minutes=10)
TARGET_WINDOW = timedelta(hours=6)

_CORRECTION_RE = re.compile(
    # says it is wrong
    r"\b(?:wrong|inaccurate|incorrect|not\s+true|that'?s\s+a\s+lie|fake\s+news|nice\s+try)\b"
    r"|^\s*(?:no|nah|nope)\b"
    # left something out
    r"|\b(?:you|your|u)\s+(?:forgot|missed|left\s+out|didn'?t\s+(?:show|include|list|answer|mention))\b"
    r"|\b(?:forgot|missed)\s+\$?[a-z]{2,6}\b"
    # answered something else
    r"|\bi\s+(?:said|meant|mean|asked)\b|\bi'?m\s+asking\b|\bi\s+am\s+asking\b|\bwhy\s+would\s+i\s+care\b"
    # a fact about the asker
    r"|\bi\s+(?:don'?t|do\s+not|never)\s+(?:even\s+)?(?:trade|own|hold|play|use)\b|\bnot\s+mine\b"
    r"|\bi'?m\s+not\s+a\b|\bonly\s+in\s+one\b|\bi\s+don'?t\s+think\s+i'?ve\s+ever\b"
    # contradicts itself or the tape
    r"|\byou\s+(?:just\s+)?said\b|\balready\s+(?:live|out|over|happened|reported|printed)\b"
    r"|\bcheck\s+the\s+(?:stock|chart|price|tape)\b|\bget\s+your\s+\w+\s+right\b"
    r"|\bseems?\s+(?:low|high|off)\b|\bhow\s+is\s+(?:it|\w+\s+a)\b|\bis\s+owned\s+by\b"
    r"|\bis\s+playing\s+(?:tonight|today|tomorrow)\b"
    # did not do the job
    r"|\btoo\s+long\b|\bbe\s+more\s+specific\b|\banswer\s+the\s+question\b|\btask\s+incomplete\b"
    r"|\b(?:do|try)\s+(?:it\s+|that\s+|this\s+|analysis\s+)?again\b|\bdo\s+better\b|\bresults\s+only\b"
    r"|\bcan'?t\s+even\b|\bthis\s+list\s+(?:blows|sucks)\b|\buseless\b",
    re.I)
_STOP = {"the", "a", "an", "is", "are", "was", "of", "to", "in", "on", "for", "and", "or",
         "what", "whats", "how", "me", "my", "i", "you", "it", "this", "that", "do", "does"}


def is_correction(text: str) -> bool:
    return bool(_CORRECTION_RE.search(text or ""))


def _words(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def _content(s: str) -> set[str]:
    return {w for w in _words(s).split() if len(w) > 2 and w not in _STOP}


def bot_parent(question: str) -> str:
    """The bot's answer this question replies to, or ''."""
    m = ask_router._REPLY_BLOCK_RE.search(question or "")
    if not m or not m.group("who").strip().lower().startswith(BOT_NAMES):
        return ""
    return m.group("parent")


def _ts(e: dict) -> datetime:
    return datetime.fromisoformat(e["ts"])


def _target(entries: list[dict], i: int, parent: str) -> dict | None:
    """The earlier answer in the same channel whose text holds the quoted
    parent: the latest one in the last 6 hours. An answer posted in several
    messages is matched on any of them."""
    key = _words(parent)[:60]
    if len(key) < 12:
        return None
    t, chan = _ts(entries[i]), entries[i].get("channel")
    for j in range(i - 1, -1, -1):
        if t - _ts(entries[j]) > TARGET_WINDOW:
            break
        if entries[j].get("channel") == chan and key in _words(entries[j].get("answer", "")):
            return entries[j]
    return None


def mark(entries: list[dict]) -> dict:
    """Set `corrected`, `corrections` and `reasked` on each entry (oldest
    first) and return the counts the attribution could not place."""
    for e in entries:
        e["corrected"], e["corrections"], e["reasked"] = False, [], False
    unplaced = 0
    for i, e in enumerate(entries):
        own = ask_router.asker_text(e["question"])
        parent = bot_parent(e["question"])
        if parent:
            if is_correction(own):
                target = _target(entries, i, parent)
                if target is None:
                    unplaced += 1
                else:
                    target["corrected"] = True
                    target["corrections"].append(own[:160])
            continue
        words = _content(own)
        if len(words) < 3:
            continue
        for j in range(i - 1, -1, -1):
            p = entries[j]
            if _ts(e) - _ts(p) > REASK_WINDOW:
                break
            if p["asker"] != e["asker"] or p["channel"] != e["channel"] or not e["asker"]:
                continue
            prev = _content(ask_router.asker_text(p["question"]))
            if prev and len(words & prev) / len(words | prev) >= 0.6:
                p["reasked"] = True
            break
    return {"unplaced_corrections": unplaced}
