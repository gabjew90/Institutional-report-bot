"""Smoke test: voice_rules.py catches the 'mechanics are uncomfortable'
family flagged by the 2026-06-04 pulse QC review.

The QC flagged 'The mechanics are uncomfortable when laid out' as a
mild AI tell that slipped through lint. Same family as the existing
'the mechanism is straightforward' ban — banker-tic that signals
mechanism explanation without committing to it.

This smoke covers:
  - Literal ban on 'the mechanics are uncomfortable' (singular + plural)
  - Regex family for noun/adjective variants
  - Regex family for 'when laid out' / 'once laid out' closer
  - Sanity: regex does NOT false-positive on benign uses
"""

import re
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _compile_regexes():
    from ai_analysis.voice_rules import BANNED_AI_TELL_REGEXES
    return [(re.compile(p, re.IGNORECASE), k) for p, k in BANNED_AI_TELL_REGEXES]


def _matches(text):
    """Returns list of (kind, matched_text) for all regex hits."""
    hits = []
    for r, kind in _compile_regexes():
        for m in r.finditer(text):
            hits.append((kind, m.group(0)))
    return hits


def test_literal_phrase_in_banned_list():
    """Both singular ('mechanic is') and plural ('mechanics are') forms
    are literal banned strings."""
    from ai_analysis.voice_rules import BANNED_AI_TELLS
    assert "the mechanics are uncomfortable" in BANNED_AI_TELLS
    assert "the mechanic is uncomfortable" in BANNED_AI_TELLS
    _ok("literal banned-strings cover singular + plural forms")


def test_regex_catches_qc_exact_phrase():
    """The exact phrase from 2026-06-04 QC must trigger."""
    text = "The mechanics are uncomfortable when laid out, but the chart is clean."
    hits = _matches(text)
    assert any(k == 'banker-mechanics-tic' for k, _ in hits), (
        f"expected banker-mechanics-tic match on QC's exact phrase, got {hits}"
    )
    _ok("regex catches 'mechanics are uncomfortable when laid out' (QC's exact phrase)")


def test_regex_catches_family_variants():
    cases = [
        "the picture is ugly when laid out",
        "the dynamics are stark once written out",
        "the math is obvious when laid out",
        "the mechanic is brutal once laid out",
        "the mechanics are grim when spelled out",
        "the dynamic is clear once put out",
    ]
    for case in cases:
        hits = _matches(case)
        assert any(k == 'banker-mechanics-tic' for k, _ in hits), (
            f"missed family-variant: {case!r} -> hits={hits}"
        )
    _ok(f"regex catches {len(cases)} family variants (mechanics|dynamics|math|picture)")


def test_regex_catches_qualifier_alone():
    """'when laid out' / 'once laid out' as a closer should trigger
    even when the main clause varies."""
    cases = [
        # Main clause not in the noun family but the closer is the same tell
        "the trajectory is bleak when laid out",
        "the trade is ugly once laid out",
        "everything is grim when laid out",
    ]
    for case in cases:
        hits = _matches(case)
        assert any(k == 'banker-mechanics-tic' for k, _ in hits), (
            f"missed qualifier-alone case: {case!r}"
        )
    _ok("regex catches qualifier-alone closer ('when/once laid out')")


def test_regex_does_not_false_positive():
    """Sanity: legitimate uses of related words don't trigger."""
    benign_cases = [
        # Mechanics in trade context — not a tell
        "the mechanics of the rolldown trade favor the long side",
        # Dynamics in macro context — not a tell
        "the dynamics of the wage-price spiral remain uncertain",
        # Laid out without the closer pattern
        "the desk laid out three scenarios for the meeting",
        # Uncomfortable without the mechanics frame
        "the desk is uncomfortable with the current dollar level",
        # Math is obvious — but without the closer-qualifier
        "the math gets ugly fast when leverage compounds",
    ]
    for case in benign_cases:
        hits = _matches(case)
        # Allow other banned patterns to fire (we're not the only ones); just
        # the new mechanics-tic kind shouldn't false-positive.
        false_pos = [h for k, h in hits if k == 'banker-mechanics-tic']
        assert not false_pos, (
            f"FALSE POSITIVE on benign case: {case!r} -> {false_pos}"
        )
    _ok("regex does NOT false-positive on benign mechanics/dynamics/laid-out uses")


if __name__ == "__main__":
    print("=== banned 'mechanics are uncomfortable' family smoke ===")
    test_literal_phrase_in_banned_list()
    test_regex_catches_qc_exact_phrase()
    test_regex_catches_family_variants()
    test_regex_catches_qualifier_alone()
    test_regex_does_not_false_positive()
    print("\nALL BANNED-MECHANICS-UNCOMFORTABLE SMOKE TESTS PASS")
