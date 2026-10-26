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


# Text a rewrite model writes about its task rather than as the reply.
_ECHO_RE = re.compile(
    r"->|=>|```|\bkeep it simple\b|\brewrite\b|\boutput only\b|\bthe reply\b"
    r"|\boriginal (?:reply|answer)\b|\bhere(?:'s| is) the\b", re.I)


def plausible_rewrite(before: str, after: str) -> bool:
    """True when a rewrite still reads as a reply that can ship.

    2026-10-11 audit: the naming check asked for a rewrite that named the
    member, got back the model's working notes ("**...)` -> Keep it
    simple: `→ **Week 5 Matchup:** ... up **25.3 points"), saw the name in
    it and shipped it. Every check that swaps in a rewrite also asks this:
    no task echo or code marks the original did not have, no unclosed
    bold, and a length in proportion to the original."""
    if not after or not after.strip():
        return False
    for mark in ("`",):
        if mark in after and mark not in (before or ""):
            return False
    if _ECHO_RE.search(after) and not _ECHO_RE.search(before or ""):
        return False
    if after.count("**") % 2:
        return False
    b, a = _words(before), _words(after)
    if b >= 8 and not (0.5 * b <= a <= 2.0 * b):
        return False
    return True


def keeps_figures(before: str, after: str) -> bool:
    """Every figure in `before` is still in `after` (formatting aside)."""
    from discord_bot.data_footer import numeric_cores
    return numeric_cores(before) <= numeric_cores(after)


def acceptable_rewrite(before: str, after: str) -> bool:
    """The one test a reword must pass before it replaces an answer: it reads
    as a reply (plausible_rewrite) and keeps every figure. 2026-10-26 audit
    A14: the repeat-avoidance reword dropped an answer's EPS and revenue
    figures (#59) and turned another into a two-line fragment (#71)."""
    return plausible_rewrite(before, after) and keeps_figures(before, after)
