"""Smoke test: no dry/deadpan/passive-aggressive register (2026-06-19).

The 2026-06-17/18 ask logs showed the bot defaulting to a sardonic,
above-the-room register in Type 2 and Type 3 answers — mock-concession
openers ("If you say it's cap, it's cap, but…"), condescending
faux-advice ("maybe spend less time on X and more time on Y"), dry
blame-deflection ("that's on you"). The user flagged this register for
removal. The fix is a binding cross-type prompt rule banning those
constructions and redirecting to a direct register.

Voice is model-governed (no clean structural/lint lever like grounding
had), so this guards that the rule is present in the system instruction
and names the actual observed tells.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_rule_present_cross_type():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    low = ins.lower()
    assert "passive-aggressive" in low, "anti-passive-aggressive rule missing"
    assert "sardonic detachment" in low, "the named failure mode is missing"
    # Must be scoped to BOTH Type 2 and Type 3, not just one.
    assert "both type 2 and type 3" in low or (
        "type 2 and type 3" in low and "passive-aggressive" in low
    ), "rule must apply across Type 2 AND Type 3"
    _ok("rule present + scoped to both Type 2 and Type 3")


def test_names_observed_tells():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    # The actual constructions pulled from the logs must be named so the
    # model has concrete targets, not an abstract "don't be dry."
    for tell in [
        "if you say it's cap",          # mock-concession opener
        "spend less time",              # condescending faux-advice
        "half the energy",              # condescending faux-advice
        "that's on you",                # dry blame-deflection
    ]:
        assert tell in ins.lower(), f"observed tell not named in rule: {tell!r}"
    _ok("rule names the concrete observed tells (concession / faux-advice / "
        "blame-deflection)")


def test_redirects_to_direct():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    low = ins.lower()
    # Must not just ban — must redirect to the replacement register.
    assert "direct" in low and "silence beats" in low, (
        "rule must redirect to a direct register + allow silence over a "
        "limp passive-aggressive needle"
    )
    _ok("rule redirects to DIRECT register (and silence over the limp needle)")


if __name__ == "__main__":
    print("=== no passive-aggressive register smoke ===")
    test_rule_present_cross_type()
    test_names_observed_tells()
    test_redirects_to_direct()
    print("\nALL PASSIVE-AGGRESSIVE-REGISTER SMOKE TESTS PASS")
