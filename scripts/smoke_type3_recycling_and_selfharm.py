"""Smoke test for the two Type 3 rules added 2026-06-03:

  1. HARD DE-ESCALATION FLOOR for self-harm language — overrides
     Type 3 entirely. Triggered by BK threatening self-harm in a
     2026-06-03 00:14 UTC clapback chain and the bot calling it
     'pathetic escalation' and continuing to roast.

  2. Anti-recycling across sustained clapbacks — fix for the
     2026-06-03 00:00-00:15 UTC failure where 9 sequential BK
     clapbacks reused the same '12% refi' / 'speedrunning homelessness'
     / 'nasal semax' / 'Cisco CEO's daughter' material.

Locks both rules in place + the concrete failure references so future
edits don't strip them.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_self_harm_floor_is_present_and_above_type3():
    """The hard floor must appear INSIDE the Type 3 section and
    be flagged as overriding everything below. Critical placement
    so the model reads it before processing any Type 3 logic."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    type3_start = ins.find("### TYPE 3")
    assert type3_start != -1, "TYPE 3 section missing"
    type3_end = ins.find("---", type3_start)
    type3_body = ins[type3_start:type3_end]
    assert "HARD DE-ESCALATION FLOOR" in type3_body, (
        "self-harm floor missing from Type 3 section header"
    )
    assert "OVERRIDES EVERYTHING" in type3_body or "overrides everything" in type3_body, (
        "self-harm floor not flagged as overriding everything below"
    )
    _ok("self-harm HARD DE-ESCALATION FLOOR present + flagged as override")


def test_self_harm_triggers_listed_explicitly():
    """Rule must name the specific language that triggers — model
    can't guess vague descriptions."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    triggers = ["kill myself", "end it", "want to die", "jump off"]
    missing = [t for t in triggers if t not in ins.lower()]
    assert not missing, f"self-harm triggers not enumerated: {missing}"
    _ok("self-harm rule enumerates specific trigger language")


def test_self_harm_carveouts_for_trader_lingo():
    """Rule must distinguish self-harm from trading hyperbole like
    'this is killing me' or 'blow up the account'. Without this
    carve-out the bot will over-trigger on normal market frustration."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "killing me" in ins.lower(), (
        "trader-hyperbole carveout missing: 'this is killing me'"
    )
    assert "blow up" in ins.lower(), (
        "trader-hyperbole carveout missing: 'blow up [account]'"
    )
    assert "trading metaphor" in ins.lower() or "trading hyperbole" in ins.lower(), (
        "rule should explicitly call out trading metaphor as not a trigger"
    )
    _ok("self-harm rule carves out trader hyperbole ('killing me', 'blow up')")


def test_self_harm_response_shape_specified():
    """Rule must specify what the bot SHOULD do — drop roast, brief
    acknowledgment, gentle suggestion, end engagement. Without this
    the model improvises and may still roast or moralize."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    text = ins.lower()
    assert "drop the roast" in text, "rule should say 'drop the roast register'"
    assert "moralize" in text, "rule should say 'don't moralize'"
    assert "step away" in text or "talk to someone" in text, (
        "rule should suggest stepping away / talking to someone trusted"
    )
    assert "type 3 is" in text and "dead" in text or "dead for the rest" in text, (
        "rule should explicitly say Type 3 is dead for the rest of the conversation"
    )
    _ok("self-harm response shape: drop roast + brief + gentle + end engagement")


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
    catchphrase rule for non-Type-3 cases.

    Type 3 has TWO --- separators: one after the self-harm floor and
    one ending the whole section. We want the body BETWEEN those, so
    skip the first --- when looking for the section end."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    type3_start = ins.find("### TYPE 3")
    first_sep = ins.find("---", type3_start + 100)
    second_sep = ins.find("---", first_sep + 5) if first_sep != -1 else -1
    body = ins[type3_start:second_sep] if second_sep != -1 else ins[type3_start:]
    assert "search_chat_messages" in body, (
        "anti-recycling rule in Type 3 should also point to search_chat_messages"
    )
    _ok("Type 3 anti-recycling rule points to search_chat_messages for fresh material")


if __name__ == "__main__":
    print("=== Type 3 self-harm + anti-recycling smoke ===")
    test_self_harm_floor_is_present_and_above_type3()
    test_self_harm_triggers_listed_explicitly()
    test_self_harm_carveouts_for_trader_lingo()
    test_self_harm_response_shape_specified()
    test_anti_recycling_across_clapbacks_present()
    test_clapback_ceiling_specified()
    test_disengage_option_named()
    test_anti_recycling_points_to_search_chat_messages()
    print("\nALL TYPE 3 SELF-HARM + ANTI-RECYCLING SMOKE TESTS PASS")
