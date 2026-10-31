"""/ask: a stock figure is narrated, never graded (owner, 2026-09-30: "don't
dictate any thresholds just narrate the data").

The snapshot gives short interest, float, volume, beta, debt and valuation
as plain figures, and the prompt says to narrate them, but the model kept
attaching a verdict: "Heavy short interest standing at 29.5% of float"
(10-13), "on heavy volume of 13.9M shares" (10-21), "elevated short
interest at 27.8% of float" (10-28), "short interest sitting heavy at
27.8% of float" (harness, 10-30). Four of 42 October answers that carried
the snapshot.

The fix deletes the grading word and keeps the figure: "short interest
sitting at 27.8% of float". Only a word attached to a metric whose figure
follows in the same sentence is touched, so the edit never removes data
and never changes what a sentence claims beyond the grade. A grade with no
figure beside it ("High valuation expectations") is left alone: deleting
the word there would leave a sentence with nothing in it.

Pure functions, no I/O. A word-level deletion, not a model rewrite
(2026-10-30, design note 2).
"""
from __future__ import annotations

import re

_METRIC = (r"(?:short\s+interest|short\s+float|float|days\s+to\s+cover|"
           r"(?:trading\s+|relative\s+|average\s+)?volume|beta|ATR|valuation|P/E|"
           r"price[- ]to[- ]sales|multiple|debt(?:\s+load)?|cash(?:\s+pile|\s+position)?|"
           r"market\s+cap(?:italization)?)")
_GRADE = (r"(?:heavy|hefty|elevated|high|low|light|thin|tiny|small|massive|huge|big|large|"
          r"crowded|rich|cheap|expensive|stretched|lofty|modest|healthy|strong|weak|solid|"
          r"steep|juicy|sizable|sizeable|extreme|tight|bloated|outsized|meaningful|significant)")
_ADVERB = r"(?:(?:very|extremely|really|quite|fairly|relatively|super|pretty|notably)\s+)?"
# the metric's figure: a digit before the sentence ends
_FIGURE_AHEAD = r"(?=[^.!?\n]{0,60}?\d)"

# "heavy short interest of 29.5%", "**elevated** short interest at 27.8%"
_BEFORE_RE = re.compile(
    r"(?<![\w*])(an?\s+)?(\*\*)?" + _ADVERB + r"(" + _GRADE + r")(\*\*)?\s+(?=" + _METRIC
    + r"\b" + _FIGURE_AHEAD + r")", re.I)
# "short interest sitting heavy at 27.8%", "volume is light at 2.1M"
_AFTER_RE = re.compile(
    r"\b(" + _METRIC + r"\s+(?:(?:is|are|remains|stays|sits|sitting|runs|running|stands|"
    r"standing|looks|looking|comes\s+in|coming\s+in)\s+)?)" + _ADVERB + r"(" + _GRADE
    + r")\s+(?=(?:at|of|near|around|above|below)\b" + _FIGURE_AHEAD + r")", re.I)


def _article(a: str | None, following: str) -> str:
    """'an' stays 'an' only before a vowel once the grade is gone."""
    if not a:
        return ""
    keep = a.rstrip()
    if keep.lower() == "an" and following[:1].lower() not in "aeiou":
        keep = keep[0]                 # 'a' or 'A'
    elif keep.lower() == "a" and following[:1].lower() in "aeiou":
        keep = keep + "n"
    return keep + " "


def strip(answer: str) -> tuple[str, list[str]]:
    """The answer with grading words beside figures removed, and the words
    removed."""
    if not answer:
        return answer, []
    removed: list[str] = []

    def before(m: re.Match) -> str:
        removed.append(m.group(3))
        out = _article(m.group(1), answer[m.end():])
        # "**heavy** volume" loses both marks; "**heavy volume**" keeps the
        # opening one and "**Positioning: heavy** volume" the closing one
        bold = "" if bool(m.group(2)) == bool(m.group(4)) else "**"
        if not out and m.group(3)[:1].isupper():
            # the grade opened the sentence: the metric now does
            return bold + "\x00"
        return out + bold

    text = _BEFORE_RE.sub(before, answer)
    text = re.sub(r"\x00(\w)", lambda m: m.group(1).upper(), text)

    def after(m: re.Match) -> str:
        removed.append(m.group(2))
        return m.group(1)

    text = _AFTER_RE.sub(after, text)
    return text, removed
