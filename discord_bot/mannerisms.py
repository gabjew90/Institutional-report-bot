"""AI mannerisms removed from an /ask answer in code (2026-11-12).

The owner's voice contract for /ask bans generic transitions and the
stock AI words by name ("delve", "tapestry", "crucial", "pivotal",
"furthermore", "in conclusion"). `ai_analysis/voice_rules.py` already
lists them for the pulse linter, and /ask's cleaner detected them and
changed nothing. A banned word is a rule a regex can enforce, so it is
enforced here and not in the prompt (CLAUDE.md /ask policy, rule 1).

Two edits, both safe to make without reading the sentence:

- a transition that opens a sentence and is followed by a comma is
  dropped and the next word capitalised ("Furthermore, margins rose" ->
  "Margins rose"). The comma is required, so "Overall revenue rose" and
  "Notably absent" stay as written.
- the named filler words are replaced by the plain word ("crucial" ->
  "key", "delve into" -> "look at").

Text inside double quotes is left alone: a quoted member or headline keeps
its own words. Pure: text in, text out.
"""
from __future__ import annotations

import re

from ai_analysis.voice_rules import BANNED_FILLER_PHRASES, BANNED_WRAPUP_SENTENCES

# "meanwhile", "that said" and "of course" carry a contrast or a
# concession: dropping them can change what the sentence says.
_KEEP = {"meanwhile", "that said", "of course", "it's worth noting"}
_OPENERS = sorted(
    {p.strip(" ,").lower() for p in (*BANNED_FILLER_PHRASES, *BANNED_WRAPUP_SENTENCES)} - _KEEP,
    key=len, reverse=True)
_START = r"(?P<lead>^|\n[ \t]*|(?<=[.!?:])[ \t]+|→[ \t]*)"
_OPENER_RE = re.compile(
    _START + r"(?P<open>" + "|".join(re.escape(o) for o in _OPENERS) + r")[ \t]*,[ \t]+(?P<next>\S)", re.I)
_WORTH_RE = re.compile(
    _START + r"(?P<open>it(?:'s|’s| is) (?:also )?worth noting(?: that)?)[ \t]*,?[ \t]+(?P<next>\S)", re.I)

_WORDS = (
    (re.compile(r"\bdelv(e|es|ed|ing)\s+(?:deep(?:er)?\s+)?into\b", re.I),
     {"e": "look at", "es": "looks at", "ed": "looked at", "ing": "looking at"}),
    (re.compile(r"\bdelv(e|es|ed|ing)\b", re.I),
     {"e": "dig", "es": "digs", "ed": "dug", "ing": "digging"}),
)
_ADJ_RE = re.compile(r"\b(crucial|pivotal)\b", re.I)
_QUOTED_RE = re.compile(r'("[^"\n]{0,600}"|“[^”\n]{0,600}”)')


def _match_case(src: str, out: str) -> str:
    return out[:1].upper() + out[1:] if src[:1].isupper() else out


def _clean(part: str) -> str:
    for rx in (_WORTH_RE, _OPENER_RE):
        # the next word takes the case the dropped opener had, so an answer
        # written in lowercase stays in lowercase
        part = rx.sub(lambda m: m.group("lead") + (m.group("next").upper() if m.group("open")[:1].isupper()
                                                   else m.group("next")), part)
    for rx, forms in _WORDS:
        part = rx.sub(lambda m: _match_case(m.group(0), forms[m.group(1).lower()]), part)
    def _adj(m):
        # capitalised inside a sentence it is a name ("Pivotal Software")
        before = part[:m.start()].rstrip(" \t*")
        if m.group(0)[:1].isupper() and before and before[-1] not in ".!?:\n→":
            return m.group(0)
        return _match_case(m.group(0), "key")
    return _ADJ_RE.sub(_adj, part)


def strip(text: str) -> str:
    """The text with sentence-opening transitions dropped and the named
    filler words replaced, outside double quotes."""
    if not text:
        return text
    parts = _QUOTED_RE.split(text)
    return "".join(p if i % 2 else _clean(p) for i, p in enumerate(parts))
