"""Smoke test: _ASK_SYSTEM_INSTRUCTION has the anti-catchphrase-stamping rule.

Background: QC on the 2026-06-02 ask log surfaced the bot closing
3 different answers to BK with 'BING BONG.' within 90 seconds.
Similar template-stamping with 'speedrunning homelessness' (2 in
24h, both BK). The bot was reading Voice samples from WHO'S TALKING
as STAMPS rather than as study material.

Fix added a 'Catchphrase stamping' subsection + 'Scan-before-stamp'
rule to the existing 'Don't repeat yourself' / 'Topic rotation'
block. This smoke locks those in so a future edit doesn't drop them.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_catchphrase_subsection_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "Catchphrase stamping" in ins, (
        "Catchphrase stamping subsection missing — bot will recycle "
        "BING BONG / speedrunning / signature voice-line closers"
    )
    _ok("system instruction has 'Catchphrase stamping' subsection")


def test_scan_before_stamp_rule_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "Scan-before-stamp" in ins, (
        "Scan-before-stamp rule missing — bot won't actively check "
        "[YOU said earlier]: lines for kicker reuse"
    )
    _ok("system instruction has 'Scan-before-stamp' rule")


def test_voice_samples_called_study_material_not_stamps():
    """The rule must explicitly reframe Voice samples as study material,
    not as a phrase bank to pull from. Critical because the ghost-writer
    reframe says 'study these voices' — Voice samples are the texture
    to OBSERVE, not phrases to RE-DEPLOY as bot output kickers."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "study material" in ins, (
        "Voice samples should be explicitly called 'study material' "
        "to prevent the bot from treating them as a stamp bank"
    )
    _ok("Voice samples explicitly framed as 'study material', not stamps")


def test_concrete_failure_example_present():
    """Concrete failure example (BING BONG x3 within 90s on 2026-06-02)
    is in the rule. Without a concrete example the abstract rule gets
    ignored by the model."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "BING BONG" in ins, (
        "Concrete 'BING BONG' example missing — the rule needs a "
        "named anti-pattern to be effective"
    )
    _ok("concrete 'BING BONG x3 in 90s' failure example present")


def test_kicker_is_optional_not_load_bearing():
    """The rule must say 'kicker is optional'. Without that, the bot
    feels compelled to write SOMETHING punchy at the end and reaches
    for whatever stamp is handy."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    text = ins.lower()
    assert "optional" in text or "not load-bearing" in text or "drop the kicker" in text, (
        "rule needs to explicitly say a closing kicker is optional"
    )
    _ok("rule explicitly permits dropping the kicker rather than recycling")


def test_tapped_out_profile_routes_to_chat_search():
    """When the profile is drained (already used in a prior reply),
    the rule must point to search_chat_messages as the fresh-material
    source instead of recycling."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    text = ins.lower()
    assert "tapped out" in text or "drained for this conversation" in text or "dry well" in text, (
        "rule needs to explicitly call the profile 'tapped out' / 'drained' "
        "when material has been used"
    )
    assert "search_chat_messages" in ins, (
        "rule needs to direct the bot to search_chat_messages for fresh material"
    )
    _ok("'tapped out profile' rule routes to search_chat_messages for fresh material")


def test_search_chat_tool_section_mentions_followup_case():
    """The search_chat_messages 'When to call' list must include the
    follow-up-with-tapped-out-profile case so the model recognizes it
    as a legitimate use."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    # Find the search_chat_messages tool section and check it mentions
    # the profile-mined / follow-up case
    sect_start = ins.find("### 2. `search_chat_messages")
    assert sect_start != -1, "search_chat_messages tool section missing"
    sect_end = ins.find("### 3.", sect_start)
    sect = ins[sect_start:sect_end]
    assert ("Profile already mined" in sect
            or "tapped out" in sect.lower()
            or "fresh material" in sect.lower()), (
        "search_chat_messages 'When to call' should mention the "
        "follow-up / tapped-out-profile case"
    )
    _ok("search_chat_messages section calls out the follow-up case")


if __name__ == "__main__":
    print("=== anti-catchphrase-stamping smoke ===")
    test_catchphrase_subsection_present()
    test_scan_before_stamp_rule_present()
    test_voice_samples_called_study_material_not_stamps()
    test_concrete_failure_example_present()
    test_kicker_is_optional_not_load_bearing()
    test_tapped_out_profile_routes_to_chat_search()
    test_search_chat_tool_section_mentions_followup_case()
    print("\nALL ANTI-CATCHPHRASE-STAMPING SMOKE TESTS PASS")
