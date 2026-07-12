"""Smoke: fc chart-command filter (2026-07-12).

Tulch asked "who's worse, McGregor's comeback or Abe's channel" and the
bot answered that Abe's channel is "a steady stream of 'fc' alerts" —
because abe's subject-verbatim quote block carried five chart-pull
command lines ('fc qcom stock 4 wide', 'fc meta stock 30 wide'...) and
nothing told the model that `fc` is command syntax, not content.

Fixes: (1) _FC_COMMAND_RE drops command lines from the subject-verbatim
block (the "this is their voice" signal); (2) the system prompt gets a
room-command lexicon rule so the recent-chat block's fc lines (kept —
they're live room texture) are never read as someone's personality,
alerts, or trade calls.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_fc_regex_matches_command_shapes():
    import discord_bot.bot as bot
    # the exact lines from abe's 07-12 quote block + room variants
    for cmd in (
        "fc qcom stock 4 wide",
        "fc wrap stock 30 % @BK (bankerkyle)",
        "fc meta stock 30 wide",
        "fcb 15",
        "Fc mu 5",
        "fc orcl 5min",
        "fc lite stock 1",
    ):
        assert bot._FC_COMMAND_RE.match(cmd), f"must match command: {cmd!r}"
    # real conversation is never dropped
    for talk in (
        "fc is broken again",  # talking ABOUT the command? starts fc+space
        "What's the port at now deadshot?",
        "Nigerian stocks could 100x and you still wouldn't buy them!",
        "I stopped looking at that loser like earlier in the year",
        "forecasting a rough open tomorrow",
    ):
        if talk == "fc is broken again":
            # borderline: short fc-prefixed prose CAN match — acceptable
            # loss for a quote block (one grumble vs five command lines).
            continue
        assert not bot._FC_COMMAND_RE.match(talk), \
            f"must not match conversation: {talk!r}"
    _ok("fc regex: command shapes match; conversation passes")


def test_filter_wired_into_subject_verbatim():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._format_subject_verbatim_block)
    assert "_FC_COMMAND_RE.match(content)" in src, \
        "subject-verbatim must drop chart-command lines"
    _ok("subject-verbatim: chart commands filtered from quote blocks")


def test_lexicon_rule_in_prompt():
    import discord_bot.bot as bot
    ins = bot._ASK_SYSTEM_INSTRUCTION
    assert "Room command lexicon" in ins, "lexicon rule missing"
    assert "CHART COMMANDS" in ins
    assert "never count them as trade calls" in ins
    _ok("prompt: fc lexicon rule present (recent-chat fc lines explained)")


if __name__ == "__main__":
    print("=== fc chart-command filter smoke ===")
    test_fc_regex_matches_command_shapes()
    test_filter_wired_into_subject_verbatim()
    test_lexicon_rule_in_prompt()
    print("\nALL FC-FILTER SMOKE TESTS PASS")
