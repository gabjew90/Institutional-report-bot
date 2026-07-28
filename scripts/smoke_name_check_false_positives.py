"""Smoke: name-check guard must not flag ordinary capitalized words.

Context (2026-07-28 audit): the week's ask-logs show chronic false
positives — `name-check:Great` (from "Great source you idiot"),
`name-check:Sell,Limit,Bid`, `name-check:Fuck,Cost,Expiration`,
`name-check:Damn`, `name-check:Nahhh,Hail,Simulated`. Each appends a
"verify these person names" note to the prompt — attention noise on
nearly every banter ask. Fix: extend the stoplist with common English
words + trading jargon. NOTE: a sentence-position heuristic was
rejected — "Morgan says you don't work very well" (the 07-17 incident
that created this guard) is sentence-initial and MUST keep firing.

Covers:
  - the observed false-positive words no longer flag
  - genuinely unknown person names still flag (Morgan regression)
  - known members still resolve as known
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_KNOWN = "ZHawk (.zhawk) bankerkyle BK abe abullish_xyz"


def test_observed_false_positives_die():
    from discord_bot.bot import _unknown_member_names
    cases = [
        "Great source you idiot",
        "Damn bro coming for my throat",
        "Sell Limit order sitting at Expiration",
        "Fuck the Cost basis on this one",
        "Nahhh Hail mary Simulated gains only",
    ]
    for q in cases:
        out = _unknown_member_names(q, _KNOWN)
        assert out == [], f"false positive(s) {out} for: {q!r}"
    _ok("observed false-positive words no longer flag as names")


def test_morgan_regression_still_fires():
    from discord_bot.bot import _unknown_member_names
    out = _unknown_member_names(
        "Morgan says you don't work very well", _KNOWN
    )
    assert "Morgan" in out, (
        f"the 07-17 Morgan case must still fire, got {out}"
    )
    _ok("sentence-initial unknown proper name (Morgan) still detected")


def test_known_members_not_flagged():
    from discord_bot.bot import _unknown_member_names
    out = _unknown_member_names(
        "is Zhawk still holding those calls", _KNOWN
    )
    assert out == [], f"known member wrongly flagged: {out}"
    _ok("known member names resolve as known")


if __name__ == "__main__":
    print("=== name-check false-positive smoke ===")
    test_observed_false_positives_die()
    test_morgan_regression_still_fires()
    test_known_members_not_flagged()
    print("\nALL NAME-CHECK FALSE-POSITIVE SMOKE TESTS PASS")
