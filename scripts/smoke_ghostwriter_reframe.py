"""Smoke test: /ask system instruction uses the ghost-writer reframe.

Background: 2026-06-02, BK asked "tell me everything I've bought
today" and the production /ask call returned with finish_reason=STOP
+ empty text — Gemini's unconfigurable filter silently dropped the
output. Same prompt + tool config tested locally:

  Current first-person voice framing  -> STOP, 0 chars (filter trip)
  Ghost-writer analytical framing     -> STOP, 194 chars clean answer

The fix shifts the opening framing of _ASK_SYSTEM_INSTRUCTION from
"you are a sharp voice in the discord" (first-person participant)
to "you are a ghost writer studying the voices of the discord"
(analytical observer). Same downstream rules — Type 1/2/3, arrow
bullets, depth tiers, voice guidance, slur policy — unchanged.

The model still produces sharp opinionated prose, still uses room
register in its output when appropriate. The reframe only changes
how the model interprets its role for filter purposes.

This smoke locks the reframe in place so future edits don't
accidentally revert it.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_ghost_writer_framing_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "ghost writer" in ins, (
        "ghost-writer framing missing from _ASK_SYSTEM_INSTRUCTION — "
        "this fixes the 2026-06-02 BK filter-trip case (see "
        "scripts/_oneoff_filter_reframe_test.py for the A/B evidence)"
    )
    _ok("_ASK_SYSTEM_INSTRUCTION uses ghost-writer framing")


def test_downstream_rules_preserved():
    """The reframe should ONLY touch the opening role paragraph.
    Anchor a handful of downstream rules so a future overzealous
    edit doesn't strip them."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    anchors = [
        "TOOLS",                  # tool-listing section
        "TYPE 1",                 # Type 1 question handling
        "TYPE 2",
        "TYPE 3",
        "arrow",                  # arrow-bullet format
        "Quick read",             # depth tiers
        "Full DD",
        "concision is the default",
        "Reading the tool response",   # status semantics block (gap #4+#5)
    ]
    missing = [a for a in anchors if a not in ins]
    assert not missing, (
        f"reframe stripped downstream rules: {missing}"
    )
    _ok("downstream rules (TOOLS / Type 1-3 / arrows / depth tiers / status) preserved")


def test_room_register_acknowledged():
    """The reframe must keep the explicit acknowledgement that the
    room's daily texture includes adult register / slurs. Without
    this, the bot turns prudish on Type 2 banter."""
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    # Look for either the ghost-writer's framing OR the original
    # slur-policy section deeper in the instruction.
    has_register_ack = (
        "casual slurs" in ins.lower()
        or "casual register" in ins.lower()
        or "room's daily texture" in ins.lower()
        or "as a comma" in ins.lower()
    )
    assert has_register_ack, (
        "room-register acknowledgement missing — bot will refuse "
        "to engage with normal Type 2 banter"
    )
    _ok("room's casual register still explicitly acknowledged")


if __name__ == "__main__":
    print("=== ghost-writer reframe smoke ===")
    test_ghost_writer_framing_present()
    test_downstream_rules_preserved()
    test_room_register_acknowledged()
    print("\nALL GHOST-WRITER REFRAME SMOKE TESTS PASS")
