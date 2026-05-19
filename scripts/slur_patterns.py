"""Slur regex patterns for the user_metrics.slur_count_30d hidden hierarchy.

PURPOSE — read before editing:

This module exists solely to feed a numeric count to the user_metrics table
used by /ask to answer ORDINAL questions ("who's the worst," "is X more
than Y") in a private trading discord that runs slur-heavy banter as its
baseline register. The bot's behavior rules treat the raw count as
context for clapback calibration; it is NEVER surfaced as a literal
number to users, NEVER used to "name and shame," and NEVER exported.

The patterns below are intentionally crude — they will catch ironic /
reclaimed / quoted / song-lyric uses at the same weight as hostile uses.
That noise is acceptable for the use case (rough ordinal comparisons
between regulars in a known-crude room). It would NOT be acceptable for
any moderation / public-facing / employment-context use.

If you're reading this in a context outside this specific room's
clapback calibration, the patterns and the metric they produce are
not fit for purpose. Move on.
"""

import re

# Category labels are for debugging only — counts collapse to a single
# `slur_count_30d` integer. We use word-boundary matching so partial
# substrings don't count (e.g., "snigger" in "snickering" won't match
# the n-slur pattern).
#
# Each entry: a regex pattern (no flags — case-insensitive applied at
# compile time below). Patterns are intentionally narrow — we want to
# undercount rather than overcount false positives.

_PATTERNS_RAW: list[tuple[str, str]] = [
    # Category, regex pattern
    ("n-slur", r"\bn[i1!]gg(?:er|ah|a)s?\b"),
    ("n-slur-hard", r"\bn[i1!]gg[ae]r\b"),
    ("f-slur", r"\bf[a@]gg?(?:ot|y)?s?\b"),
    ("r-slur", r"\br[e3]t[a@]rd(?:s|ed|ation)?\b"),
    ("k-slur-jew", r"\bk[i1!]ke?s?\b"),
    ("c-slur-asian", r"\bch[i1!]nk(?:s|y)?\b"),
    ("g-slur-asian", r"\bg[o0]{1,2}k(?:s)?\b"),
    ("s-slur-latino", r"\bsp[i1!]c(?:s|k)?\b"),
    ("w-slur", r"\bwetb[a@]ck(?:s)?\b"),
    ("t-slur-trans", r"\btr[a@]nn(?:y|ies)?\b"),
    ("tow-slur", r"\btowel[\-\s]?head(?:s)?\b"),
    ("dyke", r"\bdyk[e3]s?\b"),
    ("paki", r"\bpaki(?:s)?\b"),
]

# Compile once at module load. IGNORECASE matches obfuscation with caps.
_COMPILED: list[tuple[str, re.Pattern]] = [
    (label, re.compile(pat, re.IGNORECASE)) for label, pat in _PATTERNS_RAW
]


def count_slurs_in_text(text: str) -> int:
    """Return the count of slur occurrences in `text`. Multiple matches
    in the same message are counted separately — five n-slurs in one
    rant counts as five, not one. That's intentional: the metric tracks
    raw usage volume, not "how many people use it."

    Word-boundary matching prevents partial-substring false positives.
    No context awareness — quoted, ironic, reclaimed, and hostile uses
    all count equally. See the module docstring for why that's OK for
    this use case.
    """
    if not text:
        return 0
    total = 0
    for _label, pat in _COMPILED:
        total += len(pat.findall(text))
    return total


def count_slurs_by_category(text: str) -> dict[str, int]:
    """Debugging helper — returns per-category counts. Not used by the
    production pipeline but useful when investigating outliers."""
    if not text:
        return {}
    out: dict[str, int] = {}
    for label, pat in _COMPILED:
        n = len(pat.findall(text))
        if n:
            out[label] = out.get(label, 0) + n
    return out
