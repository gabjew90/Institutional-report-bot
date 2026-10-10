"""The limit every after-the-fact /ask check shares: a check may trim an
answer, never gut it.

WHY (2026-10-10 audit). Checks that run after the model has written its
answer delete lines they cannot vouch for. Each one is right about the
line it reads and blind to what is left: "How many times do I need to 10x
$100 to get to $1b" shipped as "→ The ladder: $100" after the figure check
removed every rung, and a definition of superintelligence lost its second
half to the dead-ticker check, which read "(ASI)" as a symbol. When a
check would leave less than 40% of the words of a substantial answer, the
check is more likely wrong than the answer, so the answer ships as written
and the caller logs that the check stood down.

Short answers are exempt: removing one line of a two-line answer is the
check doing its job, and there is too little text to judge proportion.
"""
from __future__ import annotations

import re

_MIN_WORDS = 25        # below this the check decides alone
_KEEP_FRACTION = 0.4   # a check must leave at least this share of words


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%]+", text or ""))


def gutted(before: str, after: str) -> bool:
    """True when `after` keeps too little of `before` to ship."""
    b = _words(before)
    if b < _MIN_WORDS:
        return False
    return _words(after) < _KEEP_FRACTION * b
