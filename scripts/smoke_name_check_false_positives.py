"""Smoke: name-check guard fires only on near-member name confusion (R2).

Contract v2 (2026-07-28). The v1 fix extended a stoplist with ~70
common words; within hours new false positives appeared (Weigh,
Alright, Serious, United States) — a word-list treadmill. v2 replaces
the heuristic: a capitalized token only warrants the note when it
FUZZY-MATCHES close to a known member/display name without being one
(the real mismap risk — "Monsoon" vs member "Moonsoon"). Tokens near
nobody (Great, Morgan, Serious) never fire; truly-unknown-person
handling is owned by the prompt's don't-invent-biography rule, which
the 3.5-tier model observably respects. This deliberately RETIRES the
v1 expectation that "Morgan" fires — the trade-off is documented in
the R2 recommendation and watched via QC.

Covers:
  - common capitalized words never flag (no list maintenance)
  - near-member confusion (Monsoon/Moonsoon) DOES flag
  - exact known member names don't flag
  - unknown names near nobody don't flag (Morgan retirement)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_KNOWN = ("Moonsoon (reportufirst) ZHawk (.zhawk) bankerkyle BK "
          "abe (abullish_xyz) Tulch (tulch)")


def test_common_words_never_flag():
    from discord_bot.bot import _unknown_member_names
    cases = [
        "Great source you idiot",
        "Damn bro coming for my throat",
        "Sell Limit order sitting at Expiration",
        "Weigh the Serious options Alright",
        "United States of Simulated gains",
        "Cathy said Spy puts print",
    ]
    for q in cases:
        out = _unknown_member_names(q, _KNOWN)
        assert out == [], f"false positive(s) {out} for: {q!r}"
    _ok("common capitalized words never flag — no word list needed")


def test_near_member_confusion_flags():
    from discord_bot.bot import _unknown_member_names
    out = _unknown_member_names(
        "I'm not Monsoon nigga I don't do clinical shifts", _KNOWN
    )
    assert "Monsoon" in out, (
        f"near-member name (Monsoon ~ Moonsoon) must flag, got {out}"
    )
    _ok("near-member name confusion (Monsoon/Moonsoon) flags")


def test_exact_known_members_dont_flag():
    from discord_bot.bot import _unknown_member_names
    out = _unknown_member_names(
        "is Zhawk still holding those calls with Tulch", _KNOWN
    )
    assert out == [], f"known member wrongly flagged: {out}"
    _ok("exact known member names resolve as known")


def test_unknown_near_nobody_does_not_flag():
    from discord_bot.bot import _unknown_member_names
    out = _unknown_member_names(
        "Morgan says you don't work very well", _KNOWN
    )
    assert "Morgan" not in out, (
        f"v2 contract: names near no member don't fire the note "
        f"(prompt rule owns unknown-person handling), got {out}"
    )
    _ok("unknown name near nobody stays silent (v2 contract)")


if __name__ == "__main__":
    print("=== name-check near-member smoke (v2) ===")
    test_common_words_never_flag()
    test_near_member_confusion_flags()
    test_exact_known_members_dont_flag()
    test_unknown_near_nobody_does_not_flag()
    print("\nALL NAME-CHECK NEAR-MEMBER SMOKE TESTS PASS")
