"""Smoke test for the Type 3 anti-recycling rule added 2026-06-03.

Fix for the 2026-06-03 00:00-00:15 UTC failure where 9 sequential BK
clapbacks reused the same '12% refi' / 'speedrunning homelessness' /
'nasal semax' / 'Cisco CEO's daughter' material across all 9 answers.

(The self-harm de-escalation floor that was shipped in commit f2933f9
was reverted per user direction. This smoke covers only the
anti-recycling rule that remains.)
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_anti_recycling_across_clapbacks_present():
    """The 9-message BK clapback failure must be referenced as the
    concrete failure example. Without a named anti-pattern the abstract
    rule gets ignored."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "Anti-recycling across sustained clapbacks" in ins, (
        "anti-recycling-across-clapbacks rule missing"
    )
    assert "12% refi" in ins, (
        "concrete '12% refi' failure example missing"
    )
    assert "9 sequential clapbacks" in ins or "9 sequential" in ins, (
        "rule should cite the specific 9-clapback failure from 2026-06-03"
    )
    _ok("anti-recycling rule references the 9-clapback BK failure concretely")


def test_clapback_ceiling_specified():
    """Rule must give a numerical ceiling on how many Type 3 responses
    in a row before disengaging. Without a number, 'don't repeat'
    stays abstract and gets ignored."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    text = ins.lower()
    assert "three is" in text or "3 is" in text or "ceiling" in text, (
        "rule should specify roughly three as the ceiling on sequential clapbacks"
    )
    _ok("Type 3 ceiling (~3 clapbacks) specified before disengage")


def test_disengage_option_named():
    """The 'disengage' alternative must be explicit — the bot needs
    permission NOT to respond with another clapback when material is
    spent. Without that, the bot will keep firing because Type 3 was
    invoked."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    text = ins.lower()
    assert "disengage" in text, (
        "rule should explicitly use the word 'disengage' as a valid move"
    )
    assert "you done?" in text or "going in circles" in text or "leave it there" in text, (
        "rule should suggest specific disengage phrasings"
    )
    _ok("disengage option explicit + sample phrasings provided")


def test_anti_recycling_points_to_search_chat_messages():
    """When the profile is tapped, the rule must point to
    search_chat_messages — same direction as the per-asker
    catchphrase rule for non-Type-3 cases."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    # Scope to the Type 3 section
    type3_start = ins.find("### TYPE 3")
    type3_end = ins.find("---", type3_start + 100)
    body = ins[type3_start:type3_end] if type3_end != -1 else ins[type3_start:]
    assert "search_chat_messages" in body, (
        "anti-recycling rule in Type 3 should also point to search_chat_messages"
    )
    _ok("Type 3 anti-recycling rule points to search_chat_messages for fresh material")


def test_self_harm_floor_was_reverted():
    """Sanity check: the self-harm de-escalation floor that was added
    in commit f2933f9 was reverted per user direction. Confirm it's
    NOT in the system instruction."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "HARD DE-ESCALATION FLOOR" not in ins, (
        "self-harm floor should have been reverted but is still present"
    )
    _ok("self-harm floor reverted (per user direction)")


if __name__ == "__main__":
    print("=== Type 3 anti-recycling smoke ===")
    test_anti_recycling_across_clapbacks_present()
    test_clapback_ceiling_specified()
    test_disengage_option_named()
    test_anti_recycling_points_to_search_chat_messages()
    test_self_harm_floor_was_reverted()
    print("\nALL TYPE 3 ANTI-RECYCLING SMOKE TESTS PASS")
