"""How hot the reply may run, decided in code from what the asker
actually said this exchange.

WHY (2026-09-04). The dial is spelled out in the prompt: it rests at
zero, only what they brought THIS exchange raises it, praise is not
provocation, size is part of the match, and never open profile material
the exchange did not open. On 09-04 all three banter replies broke it:

  "Gg Abe"                     -> a paragraph about their straddle
  "Hey buddy straddle hit 11x" -> their MSTR and Micron history
  "Puts pls."                  -> "peak casino brain", Palantir bags,
                                  shoplifting plans with Kyle

None of those inputs is provocation. The rule was there and the model
did not apply it, which is the CLAUDE.md pattern: a rule that can be
decided deterministically should be decided in code and handed over,
not left to judgement. So the LEVEL is computed here and injected as an
instruction; the prompt keeps the prose about what each level means.

This tunes intensity ONLY. It never decides whether to reply — the
reply-to-bot trigger is untouched (owner reversed a stand-down once;
tune the answer, never re-narrow the trigger).
"""
from __future__ import annotations

import re

# Level 3: sustained abuse aimed at the bot. Slurs are handled by the
# separate slur machinery; this is the "you piece of shit" register.
_ABUSE = re.compile(
    r"\b(?:fuck\s+(?:you|off)|shut\s+the\s+fuck\s+up|kill\s+yourself|kys)\b", re.I)

# Level 2: a direct insult AT the bot or the asker's counterpart —
# second person plus a contempt word.
_INSULT_WORD = (r"(?:stupid|dumb|idiot|moron|retard\w*|trash|garbage|useless|worthless"
                r"|clown|joke|pathetic|braindead|brain\s*dead|dogshit|shit|suck\w*|awful"
                r"|terrible|lame|cringe|mid)")
_INSULT = re.compile(
    # The second person must GOVERN the insult, not merely appear near
    # it: at 40 characters "you know abe is an idiot" scored 2, which
    # aimed the clapback at someone insulting a third party. 14 keeps
    # "ur takes are trash" (11) and drops that one (16). Errors here
    # must fall DOWNWARD — the prompt's own rule is "unsure whether
    # something was a jab? It wasn't."
    r"\b(?:you|u|ur|your|yours|yr)\b[^.!?]{0,14}\b" + _INSULT_WORD + r"\b"
    # Either order: "this bot is dogshit" put the subject FIRST and
    # scored 0, missing a direct insult outright.
    r"|\b" + _INSULT_WORD + r"\b[^.!?]{0,20}\b(?:bot|ai|you)\b"
    r"|\b(?:this\s+)?(?:bot|ai)\b[^.!?]{0,20}\b" + _INSULT_WORD + r"\b", re.I)

# Level 1: a tease or poke. Backhanded praise counts as a POKE, not an
# insult: the prompt says take it, one dry line, done.
_TEASE = re.compile(
    r"\b(?:lol|lmao|lmfao|kek|ratio|cope|copium|mid|nice\s+try|sure\s+buddy"
    r"|good\s+(?:boy|bot)|wow\s+it\s+can|calm\s+down|relax|chill|yeah\s+right"
    r"|hey\s+buddy|bro\s+what|wtf\s+are\s+you)\b|\?{2,}|!{2,}", re.I)

# Ordinary requests and reactions that must NOT raise the dial. "Gg",
# "puts pls", "thanks" are not provocation however the model reads them.
_NEUTRAL_ONLY = re.compile(r"^[\s\W]*(?:gg|gj|ty|thx|thanks|nice|w|dub|based|real|facts)[\s\W]*$", re.I)

LEVELS = {
    0: "DIAL 0 — straight answer. No jab, no profile material, no "
       "'you of all people' framing. Not one seasoning clause.",
    1: "DIAL 1 — at most ONE dry line of seasoning riding on a real "
       "answer. No paragraph, no history, no P&L jab.",
    2: "DIAL 2 — a clapback is earned. Use only what THIS exchange "
       "opened. Personal color beats P&L.",
    3: "DIAL 3 — escalate with them, round for round, same limits on "
       "material.",
}


def asker_message(question: str) -> str:
    """The asker's own words this turn, without the quoted context the
    reply-to machinery prepends. A quoted trade alert is not something
    the asker said."""
    q = (question or "").strip()
    m = re.search(r"\[[^\]]*message to you\]\s*\n(.*)$", q, re.S)
    if m:
        return m.group(1).strip()
    if q.startswith("["):
        q = re.sub(r"^\[.*?\]\s*\n?", "", q, flags=re.S).strip()
        if "\n\n" in q:
            q = q.split("\n\n")[-1].strip()
    return q


def provocation_level(question: str) -> int:
    """0-3 from the asker's own message. Unsure is 0: the prompt's own
    rule is "unsure whether something was a jab? It wasn't."."""
    msg = asker_message(question)
    if not msg:
        return 0
    if _ABUSE.search(msg):
        return 3
    if _INSULT.search(msg):
        return 2
    if _NEUTRAL_ONLY.match(msg):
        return 0
    if _TEASE.search(msg):
        return 1
    return 0


def max_jab_sentences(level: int) -> int:
    """Size is part of the match: a two-word tease earns at most one
    sentence, a real insult earns the paragraph."""
    return {0: 0, 1: 1, 2: 4}.get(level, 8)


def directive(question: str) -> tuple[int, str]:
    """(level, the line to inject). Computed here so the model is told
    the level rather than asked to infer it."""
    lvl = provocation_level(question)
    msg = asker_message(question)
    return lvl, (
        f"\n\nTONE DIAL FOR THIS TURN: {LEVELS[lvl]}\n"
        f"The asker's own words this exchange were: {msg[:200]!r}. "
        f"That is what sets the ceiling; their profile and their history do not.\n"
    )
