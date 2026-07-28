"""Contract smoke for the router's feedback-register clause.

Context (2026-07-27): ZHawk's "Timothy Sykes is a serial scammer and
con artist" — feedback agreeing with a sourcing complaint about the
bot's previous answer — routed BANTER and earned an invented-premise
clapback ("Calling me an idiot is rich..."). One minute later: "That
bot has hilariously degraded." Feedback about the bot's own output
must register FACT so the straight-answer directive + asker-mockery
guard apply.

Contract assertions on _ASK_ROUTER_INSTRUCTION (same style as
smoke_ask_prompt_contract.py — the router prompt is tiny and its
classification behavior can't be unit-tested offline, so the guarded
surface is the instruction text):
  - the feedback-register clause is present
  - the two-word output contract is intact
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_feedback_register_clause_present():
    from discord_bot.bot import _ASK_ROUTER_INSTRUCTION as r
    assert "feedback about the bot's previous answer" in r, (
        "feedback-register clause missing from router instruction"
    )
    assert "not a comeback" in r, "acknowledgment framing missing"
    _ok("router instruction carries the feedback-register clause")


def test_output_contract_intact():
    from discord_bot.bot import _ASK_ROUTER_INSTRUCTION as r
    assert "Output EXACTLY two words" in r, "two-word output contract lost"
    for verdict in ("'WEB FACT'", "'WEB BANTER'", "'LOCAL FACT'",
                    "'LOCAL BANTER'"):
        assert verdict in r, f"verdict vocabulary lost: {verdict}"
    _ok("two-word output contract intact")


if __name__ == "__main__":
    print("=== router feedback-register smoke ===")
    test_feedback_register_clause_present()
    test_output_contract_intact()
    print("\nALL ROUTER FEEDBACK-REGISTER SMOKE TESTS PASS")
