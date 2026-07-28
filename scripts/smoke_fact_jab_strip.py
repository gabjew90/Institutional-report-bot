"""Smoke test for the FACT-answer jab strip.

Context (2026-07-27): a sincere factual question ("how many planets in
the Milky Way?") got a correct answer that closed with a roast arrow
("...I'm sure you'll keep spamming **MU calls** until you've exhausted
every corner of the solar system") — the asker's explicit feedback:
"You stupid ai I didn't ask for the sarcasm at the end." The FACT
directive and Type 1 profile rules both ban this; neither is enforced.
This is the code-level guard: on FACT-routed answers, sentences that
combine second-person address with roast vocabulary get stripped.

Covers `_fact_jab_sentences`:
  - the planets jab detected, factual arrows untouched
  - legitimate second-person trade framing NOT flagged
  - pure factual answers produce no jab sentences
  - strip integration leaves a clean remainder
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_PLANETS_ANSWER = (
    "→ Astronomers estimate there are between **100 billion and 400 "
    "billion planets** in the Milky Way, with statistical models "
    "suggesting nearly every star hosts at least one.\n\n"
    "→ Data from the Kepler mission confirms that planets are the rule "
    "rather than the exception.\n\n"
    "→ Plenty of room for you to find a new place to trade, though I'm "
    "sure you'll keep spamming **MU calls** until you've exhausted "
    "every corner of the solar system."
)


def test_planets_jab_detected():
    from discord_bot.bot import _fact_jab_sentences
    jabs = _fact_jab_sentences(_PLANETS_ANSWER)
    assert jabs, "planets jab not detected"
    assert all("Kepler" not in j for j in jabs), jabs
    assert any("spamming" in j for j in jabs), jabs
    _ok("planets closing jab detected, factual arrows untouched")


def test_legit_second_person_not_flagged():
    from discord_bot.bot import _fact_jab_sentences
    ans = (
        "→ **Bear Case:** The balance sheet is under pressure with a "
        "debt-to-equity ratio near **300%**.\n\n"
        "→ **The Trade-off:** You're betting on whether OCI can scale "
        "fast enough to turn that massive backlog into cash before the "
        "debt load forces a valuation ceiling."
    )
    assert _fact_jab_sentences(ans) == [], _fact_jab_sentences(ans)
    _ok("legitimate 'you're betting on' trade framing not flagged")


def test_pure_factual_no_jabs():
    from discord_bot.bot import _fact_jab_sentences
    ans = (
        "→ Intel reports Q2 earnings Thursday July 23 after the close.\n\n"
        "→ Conference call follows at 2:00 PM PDT."
    )
    assert _fact_jab_sentences(ans) == []
    _ok("pure factual answer -> no jab sentences")


def test_strip_leaves_clean_remainder():
    from discord_bot.bot import _fact_jab_sentences, _strip_sentences
    jabs = _fact_jab_sentences(_PLANETS_ANSWER)
    remainder = _strip_sentences(_PLANETS_ANSWER, jabs)
    assert "Kepler" in remainder and "400" in remainder, remainder
    assert "spamming" not in remainder, remainder
    _ok("strip removes the jab, keeps the factual arrows")


if __name__ == "__main__":
    print("=== FACT jab strip smoke ===")
    test_planets_jab_detected()
    test_legit_second_person_not_flagged()
    test_pure_factual_no_jabs()
    test_strip_leaves_clean_remainder()
    print("\nALL FACT JAB STRIP SMOKE TESTS PASS")
