"""Smoke test: adjacent-duplication collapse + immediate-dup detection
(2026-06-13 ask-log QC).

Background: the repetition detector + retry catches token loops, but on
verbatim adjacent doublings it FIRES-AND-FAILS — when the higher-temp
retry re-glitches, the code ships the original garbled text. Observed
in the 06-10 ask logs:
  - "turned increasingly turned cautious as cautious as the recent
     price action suggests" (phrase doubling, shipped garbled)
And a class the detector misses entirely:
  - doubled stopwords ("is is", "the the") — Gate 1 needs a content
    word 3x, Gates 2/3 need a content token in the bigram, so a
    doubled stopword slips every gate.

Fix:
  - _collapse_adjacent_dupes: deterministic cleanup in the final-clean
    chokepoint (_clean_voice_violations) — removes verbatim adjacent
    word / 2-3-word-phrase repeats regardless of whether the LLM retry
    cooperated. Units must start with a letter, so numeric sequences
    (strike lists "580 585 590", ranges "94, 99") are protected.
  - _has_repetition_glitch Gate 0: immediate exact alpha-token dup ->
    triggers the retry + flags the QC log.

This smoke covers the collapse cases, the must-not-touch regressions
(the false-positive risk), and the new detector gate.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_collapse_phrase_doubling():
    from discord_bot.bot import _collapse_adjacent_dupes as c
    out = c("has turned increasingly turned cautious as cautious as the "
            "recent price action suggests.")
    # The worst doubling ("cautious as cautious as") must be gone
    assert "cautious as cautious as" not in out
    assert "cautious as the recent" in out, out
    _ok("collapse: 'cautious as cautious as' -> 'cautious as'")


def test_collapse_word_and_stopword_doubling():
    from discord_bot.bot import _collapse_adjacent_dupes as c
    assert c("the company is is going to report") == \
        "the company is going to report"
    assert c("watch the the support level") == "watch the support level"
    assert c("we expect it to continue to continue higher") == \
        "we expect it to continue higher"
    _ok("collapse: 'is is' / 'the the' / 'continue to continue'")


def test_collapse_preserves_first_casing():
    from discord_bot.bot import _collapse_adjacent_dupes as c
    # IGNORECASE match, but the kept token uses the first occurrence
    assert c("The the market") == "The market"
    _ok("collapse: keeps first-occurrence casing")


def test_collapse_protects_numeric_sequences():
    """The false-positive risk: legit number lists / ranges / strike
    lists look like 'repeats' but must NOT be collapsed."""
    from discord_bot.bot import _collapse_adjacent_dupes as c
    protect = [
        "SPY 580, 585, 590 are the call walls",
        "pricing in a 94, 99% probability of a hike",
        "- GOOGL 355C 06-12 @1.75\n- NFLX 90C 06-12 @0.74",
        "strikes at 100 100 100 across the chain",  # digit run protected
    ]
    for s in protect:
        assert c(s) == s, f"must not touch: {s!r} -> {c(s)!r}"
    _ok("collapse: numeric sequences / strike lists / ranges untouched")


def test_collapse_leaves_distinct_words():
    from discord_bot.bot import _collapse_adjacent_dupes as c
    for s in ["buy the dip and sell the rip",
              "SPY up 1.4 percent and TSLA up 3 percent",
              "higher highs and higher lows into the close"]:
        assert c(s) == s, f"distinct words altered: {s!r} -> {c(s)!r}"
    _ok("collapse: distinct adjacent words left intact")


def test_detector_immediate_dup_gate():
    from discord_bot.bot import _has_repetition_glitch as g
    assert g("the company is is going to report earnings next week now")
    assert g("watch the the support level into the close today please")
    assert not g("the company will report earnings next week with guidance")
    _ok("detector Gate 0: immediate alpha-token dup flagged, clean clean")


def test_clean_path_records_adjacent_dupe_kind():
    """The collapser runs inside _clean_voice_violations and records an
    'adjacent_dupe' hit kind so the QC log shows it fired."""
    from discord_bot.bot import _clean_voice_violations
    cleaned, kinds = _clean_voice_violations(
        "the move should grind grind into the print")
    assert "grind grind" not in cleaned
    assert "adjacent_dupe" in kinds
    # A clean answer must not get the kind
    cleaned2, kinds2 = _clean_voice_violations(
        "the move should grind higher into the print")
    assert "adjacent_dupe" not in kinds2
    _ok("clean path: collapses + records 'adjacent_dupe'; clean answer "
        "unflagged")


if __name__ == "__main__":
    print("=== adjacent-dupe collapse + detection smoke ===")
    test_collapse_phrase_doubling()
    test_collapse_word_and_stopword_doubling()
    test_collapse_preserves_first_casing()
    test_collapse_protects_numeric_sequences()
    test_collapse_leaves_distinct_words()
    test_detector_immediate_dup_gate()
    test_clean_path_records_adjacent_dupe_kind()
    print("\nALL ADJACENT-DUPE SMOKE TESTS PASS")
