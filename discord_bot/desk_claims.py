"""A desk is credited only with what its own note says (owner, 2026-10-01).

2026-10-28 audit A4: "Goldman's desk points to Robinhood margin usage hitting
local highs while retail search interest sits near cycle lows, leaving the
stock hypersensitive to sudden volume dry-ups and after-hours sweeps." The
margin and search figures were in the note. The conclusion after "leaving"
was the model's, written as Goldman's. That trailing clause is what goes: the
rest of the sentence is the desk's, and no model is asked to rewrite it.
"""
from __future__ import annotations

import json
import re

# A consequence hung on the end of a sentence that names a desk.
_CLAUSE_RE = re.compile(
    r",\s*(?:leaving|making|setting|putting|meaning|creating|suggesting|implying"
    r"|signal+ing|which)\b.*?(?=[.!?]?\s*$)", re.I)      # to the sentence's end: "$1.2B" is no stop
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_WORD_RE = re.compile(r"[a-z][a-z'-]{3,}")
_STOP = frozenset("""
    leaving making setting putting meaning creating suggesting implying signaling
    signalling which that this these those with from into over under about than then
    them they their there have been being were will would could should more most much
    very only also just stock stocks shares name names market markets while when where
    what your you're still even near sits
""".split())
MIN_WORDS = 3
MIN_SUPPORT = 0.5


def _prefixes(text: str) -> set[str]:
    return {w[:5] for w in _WORD_RE.findall(text.lower())}


def _key(source: str) -> str:
    """How an answer names the bank: 'Goldman Sachs' -> 'goldman', and two
    words where the first alone is another bank's ('Morgan Stanley')."""
    words = (source or "").lower().split()
    if not words:
        return ""
    return " ".join(words[:2]) if words[0] in ("morgan", "bank", "jp") else words[0]


def notes_by_bank(notes: list[dict]) -> dict[str, str]:
    """bank key -> the text of everything its notes in this turn said."""
    out: dict[str, str] = {}
    for n in notes or []:
        key = _key(str(n.get("source") or ""))
        if key:
            out[key] = out.get(key, "") + " " + json.dumps(n, ensure_ascii=False, default=str)
    return out


def _unsupported(clause: str, note_text: str) -> bool:
    words = [w for w in _WORD_RE.findall(clause.lower()) if w not in _STOP]
    if len(words) < MIN_WORDS:
        return False
    known = _prefixes(note_text)
    return sum(w[:5] in known for w in words) / len(words) < MIN_SUPPORT


def strip_unsupported(answer: str, notes: list[dict]) -> tuple[str, list[str]]:
    """(the answer without those clauses, the clauses removed)."""
    banks = notes_by_bank(notes)
    if not answer or not banks:
        return answer, []
    removed: list[str] = []

    def _fix(sentence: str) -> str:
        low = sentence.lower()
        hits = [k for k in banks if re.search(r"\b" + re.escape(k) + r"\b", low)]
        if not hits:
            return sentence
        for m in reversed(list(_CLAUSE_RE.finditer(sentence))):
            clause = m.group(0)
            if clause.count("**") % 2:      # would leave bold unbalanced
                continue
            if all(_unsupported(clause, banks[k]) for k in hits):
                removed.append(clause.lstrip(", ").strip())
                sentence = sentence[:m.start()] + sentence[m.end():]
        return sentence

    lines = []
    for line in answer.split("\n"):
        parts = _SENTENCE_RE.split(line)
        lines.append(" ".join(_fix(p) for p in parts) if len(parts) > 1 or parts[0] else line)
    return ("\n".join(lines), removed) if removed else (answer, [])
