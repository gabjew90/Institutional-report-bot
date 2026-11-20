"""Answers that did not land, read from the ask logs (2026-10-28, widened 2026-11-20).

The scoreboard's other numbers count repair work (checks that fired, words a
check changed). This module measures the outcome instead: how often the asker
told the bot an answer was wrong, asked again, or got no answer at all.

Three signals, all from the ask logs alone:

- corrected: a reply to the bot's answer that says it was wrong, incomplete
  or answered something else ("you forgot spcx", "GMS is owned by home
  depot", "I said gay not racist", "it's already live", "be more specific").
  The reply quotes the answer as its parent, so the correction is attached to
  the answer it replied to, whoever asked it.
- re-asked: the same question put again. Either the same asker repeats it in
  the same channel within 10 minutes without replying (keyed by the logged
  username, stable over that window), or a reply to the answer restates the
  question that produced it ("what time is the Apple earnings call", then
  "what time is the call" under the answer).
- gave up: the answer is one of the bot's own canned no-answer lines.

`did_not_land` is any of the three, and is the number a change should move.

`is_correction` starts from the pattern in discord_bot/follow_up.py, which
also routes live replies. The additions and exclusions below are for the
measure only, so widening the measure does not change what the bot does.

How well it reads (labels are by hand, kept outside this repository because
they are member messages):

- 2026-09-01 to 10-26, 316 replies: the follow_up pattern caught 44 of 50
  corrections with 3 false alarms. It was written from that set.
- 2026-10-27 to 11-19, 245 answers, a fresh month: 16 answers did not land
  by the asker's own next message. The 10-28 version caught 3 with 3 false
  alarms. Two audits in a row (11-16, 11-19) had read "0 corrected, 0
  re-asked" while both happened: a re-ask posted as a reply was never
  tested, and a canned give-up counted as an answer. This version catches
  10 with no false alarm, on the set it was widened on, so treat that as an
  upper bound too. Run back over 2026-09-01 to 10-26 (718 answers) it flags
  11 more answers than before, each one read: 10 canned lines and one
  re-ask posted as a reply, none false, and no correction gained or lost.
  API failures, which the scoreboard already counted apart, are on top of
  both figures.

What it cannot see: a correction posted in the room without replying to the
bot ("that can't be right") is in chat, not in the ask logs. The quality
audit reads the room's next 15 minutes for those. A re-ask in other words
("earnings time", then "when do they report") and a contrary fact stated
plainly ("he sold that this morning") are also missed.

The ends of the window are soft: an answer just before it cannot be
credited with a correction inside it (counted as unplaced), and an answer
near its end may be corrected after it. Read the most recent days with that
in mind.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from discord_bot import ask_router, data_footer, follow_up

BOT_NAMES = ("omniwiz",)
REASK_WINDOW = timedelta(minutes=10)
TARGET_WINDOW = timedelta(hours=6)

_STOP = {"the", "a", "an", "is", "are", "was", "of", "to", "in", "on", "for", "and", "or",
         "what", "whats", "how", "me", "my", "i", "you", "it", "this", "that", "do", "does"}

# Measure only (see the module docstring). Each line stands for one labelled
# reply from 2026-10-27 to 11-19.
_MORE_RE = re.compile(
    # "you have to work each one out separately"
    r"^\s*(?:no[\s,]+)?you\s+(?:have|need)\s+to\b"
    # "give me a number": the answer left out the one thing asked for
    r"|^\s*(?:just\s+)?give\s+me\s+(?:a|an|the)\s+\w+\W*$"
    # "so again, who makes the playoffs"
    r"|^\s*(?:so\s+)?again\b|\bone\s+more\s+time\b", re.I)
# The follow_up pattern's false alarms in the fresh month: banter that only
# looks like a complaint.
_NOT_A_CORRECTION_RE = re.compile(
    r"\basking\s+for\s+a\s+friend\b"
    r"|^\s*no\s+(?:shit|way|cap|doubt|kidding|lie)\b", re.I)
# "I'm not a [insult]" answers a jab. "I'm not a seller here" corrects a fact.
_NOT_A_RE = re.compile(r"\bi'?m\s+not\s+a\b", re.I)


def is_correction(text: str) -> bool:
    """The asker's own words say the answer was wrong, incomplete or about
    something else."""
    t = text or ""
    if _NOT_A_CORRECTION_RE.search(t) or (_NOT_A_RE.search(t) and follow_up._INSULT_RE.search(t)):
        return False
    return bool(follow_up.is_correction(t) or _MORE_RE.search(t))


def _words(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").lower()).strip()


def _content(s: str) -> set[str]:
    return {w for w in _words(s).split() if len(w) > 2 and w not in _STOP}


def _canned_lines() -> tuple[str, ...]:
    lines = list(data_footer._FALLBACK_PREFIXES)
    try:
        from ask_response_validate import SAFE_REFUSAL  # same folder
        lines.append(SAFE_REFUSAL)
    except Exception:
        lines.append("→ don't have that one — pull it from your broker")
    return tuple(_words(x) for x in lines)


_CANNED = _canned_lines()


def gave_up(answer: str) -> bool:
    """The answer is one of the bot's own no-answer lines. Compared word by
    word, because the cleaner rewrites the dash in one of them."""
    return _words(answer).startswith(_CANNED)


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


def restates(reply: str, asked: str) -> bool:
    """A reply under an answer puts the question that produced it again:
    most of the shorter one's words are in the other ("what time is the
    call" under "what time is the Apple earnings call"), or the question
    named several listed symbols and the reply is one of them ("and MSFT"
    under "aapl, msft and nvda earnings expectations")."""
    a, b = _content(reply), _content(asked)
    both = a & b
    if not both:
        return False
    if len(both) >= 2:
        return len(both) / len(a | b) >= 0.6 or len(both) / min(len(a), len(b)) >= 0.8
    named = sum(ask_router.listed(w.upper()) for w in b)
    return len(a) == 1 and named >= 2 and ask_router.listed(next(iter(a)).upper())


def mark(entries: list[dict]) -> dict:
    """Set `corrected`, `corrections`, `reasked`, `gave_up` and
    `did_not_land` on each entry (oldest first) and return the counts the
    attribution could not place."""
    for e in entries:
        e["corrected"], e["corrections"], e["reasked"] = False, [], False
        # an API failure posts no answer either
        e["gave_up"] = e.get("type") == "failed" or gave_up(e.get("answer", ""))
    unplaced = 0
    for i, e in enumerate(entries):
        own = ask_router.asker_text(e["question"])
        parent = bot_parent(e["question"])
        if parent:
            target = _target(entries, i, parent)
            if is_correction(own):
                if target is None:
                    unplaced += 1
                else:
                    target["corrected"] = True
                    target["corrections"].append(own[:160])
            if target is not None and restates(own, ask_router.asker_text(target["question"])):
                target["reasked"] = True
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
    for e in entries:
        e["did_not_land"] = e["corrected"] or e["reasked"] or e["gave_up"]
    return {"unplaced_corrections": unplaced}
