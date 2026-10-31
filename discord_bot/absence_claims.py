"""/ask: a sentence that says something does not exist must be backed by
what the turn saw, or it goes (2026-10-30 audit A5).

"what does elmt do? How much stake does DOD have in it?" was answered
"The Department of Defense holds no direct reported equity stake in the
company" (10-28 15:48, and again in the 10-30 harness run). The Defense
Department had invested $450M in preferred equity with warrants for up to
19.9% of the common on 9-14. No tool payload said there was no stake: the
news search had come back empty and the model read the empty result as a
negative. Figure provenance cannot see this, because the sentence has no
number in it.

The check: a sentence that denies an ownership, deal or announcement
("holds no stake", "rather than a direct equity stake", "has not
announced a deal") stays only if the evidence denies the same thing, that
is, the same noun appears in a tool payload, an injected block or a
Google-backed passage with a negation a few words before it. Otherwise the
sentence is removed. Chat, the question and the bot's earlier answers are
not evidence here: a wrong denial the bot made yesterday must not back
today's.

Strip only, never a rewrite (2026-10-30, design note 2), and never past
the shared floor (`guard_floor.gutted`). Pure functions, no I/O.
"""
from __future__ import annotations

import re

# What a denial is about. Words a stock answer denies wrongly when a search
# comes back empty: ownership, deals, filings, announcements.
_NOUN = (r"(?:stakes?|ownership|equity|investments?|investors?|shareholders?|owners?|"
         r"contracts?|deals?|partnerships?|agreements?|acquisitions?|mergers?|buyouts?|"
         r"offerings?|filings?|announcements?|news)")
_W = r"(?:[\w'$.-]+\s+)"           # one word and its space
_ANSWER_RE = re.compile(
    r"\b(?:no|zero)\s+" + _W + r"{0,3}" + _NOUN + r"\b"
    r"|\bnot\s+(?:a|an|any)\s+" + _W + r"{0,2}" + _NOUN + r"\b"
    r"|\b(?:has|have|had|did|does|do|is|are|was|were)(?:n't|\s+not|\s+never)\s+" + _W + r"{0,3}"
    + _NOUN + r"\b"
    r"|\brather\s+than\s+(?:a\s+|an\s+)?" + _W + r"{0,3}" + _NOUN + r"\b"
    r"|\bnever\s+" + _W + r"{0,3}" + _NOUN + r"\b"
    # "the stake was never announced"
    r"|\b" + _NOUN + r"\s+(?:was|were|has|have|had)(?:n't|\s+not|\s+never)\s+(?:been\s+)?"
    r"(?:announced|disclosed|reported|filed|signed|made|completed|confirmed)\b",
    re.I)
_NOUN_RE = re.compile(r"\b" + _NOUN + r"\b", re.I)
_NEG_RE = re.compile(r"\b(?:no|not|none|never|without|zero|neither|nor)\b|n't\b", re.I)
_SUPPORT_WINDOW = 8                # words between the negation and the noun in the evidence
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
# what opens a line and is not part of the claim: an arrow or bullet and a
# bold label ("→ **DOD stake:** ")
_LEAD_RE = re.compile(r"^\s*(?:→|[-*•])?\s*(?:\*\*[^*\n]{1,40}?(?::\*\*|\*\*:))?\s*")


def _stem(noun: str) -> str:
    n = noun.lower()
    return n[:-1] if n.endswith("s") and not n.endswith("ss") and n not in ("news",) else n


def denials(text: str) -> list[tuple[str, str]]:
    """(sentence, noun) for each sentence of `text` that denies something."""
    out = []
    for line in (text or "").splitlines():
        for sent in _SENT_RE.split(line):
            m = _ANSWER_RE.search(sent)
            if m:
                noun = _NOUN_RE.findall(m.group(0))[-1]
                out.append((sent[_LEAD_RE.match(sent).end():].strip(), _stem(noun)))
    return out


def supported(noun: str, evidence: str) -> bool:
    """The evidence itself denies `noun`: a negation within a few words
    before it ("no equity stake", "has not announced any deal")."""
    words = re.findall(r"[\w']+", evidence or "")       # "stake." is the word "stake"
    for i, w in enumerate(words):
        if _NOUN_RE.fullmatch(w) and _stem(w) == noun:
            window = " ".join(words[max(0, i - _SUPPORT_WINDOW):i])
            if _NEG_RE.search(window):
                return True
    return False


def check(answer: str, evidence: str) -> tuple[str, list[str]]:
    """(answer without its unbacked denials, the sentences removed)."""
    removed = [s for s, noun in denials(answer) if not supported(noun, evidence)]
    if not removed:
        return answer, []
    lines = []
    for line in answer.splitlines():
        for s in removed:
            line = line.replace(s, "")
        line = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        # an arrow, bullet or label left with nothing after it goes too
        if line.strip() and not line[_LEAD_RE.match(line).end():].strip():
            continue
        lines.append(line)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return out, removed
