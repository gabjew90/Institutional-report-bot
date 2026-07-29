"""Smoke: hitting the tool-round cap must still produce an ANSWER.

2026-07-29, third failure of "analyze trades opened by analysts
relative to qqq" — this time no 400 at all. The tool trace shows FOUR
successful calls (callers, 12K of trades, 16K of QQQ history, schema),
then the loop hit _CHAT_SEARCH_MAX_ROUNDS with calls still pending and
`break`-ed while the model was mid-work. The response it broke on was a
function-call-only turn, which carries NO text — so response.text was
empty and the user got "No response came back (reason: STOP)."

Two fixes:
  1. The cap is too low for a real multi-tool analysis (query_data ->
     price history -> compute). Raised.
  2. Exhausting the cap must degrade gracefully: make a final
     tools-disabled call so the model writes an answer from everything
     it already gathered, instead of returning nothing.
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


def test_round_cap_raised():
    from discord_bot.bot import _CHAT_SEARCH_MAX_ROUNDS
    assert _CHAT_SEARCH_MAX_ROUNDS >= 5, (
        f"a real analysis chains query_data -> price history -> compute; "
        f"cap of {_CHAT_SEARCH_MAX_ROUNDS} strands it mid-work"
    )
    _ok(f"tool-round cap raised to {_CHAT_SEARCH_MAX_ROUNDS}")


def test_cap_exhaustion_forces_a_final_answer():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    seg = src.split("hit tool-calling round cap", 1)
    assert len(seg) == 2, "round-cap branch missing"
    after = seg[1][:4200]
    # it must make one more model call with tools OFF so text is produced
    assert "generate_content" in after, (
        "cap exhaustion must make a final answer-now call, not just break"
    )
    # The final call must withhold the DATA tools (no more lookups) but
    # KEEP code execution — that's how the answer gets computed and
    # charted. tools=[] here shipped a chartless answer (2026-07-29).
    assert "code_execution" in after, (
        "the final call must KEEP code execution so it can still "
        "compute and chart from what was gathered"
    )
    assert "tools=[]" not in after, (
        "tools=[] kills the sandbox too — withhold only the data tools"
    )
    _ok("final call keeps code execution, withholds data tools")


def test_query_data_discourages_schema_probing():
    import discord_bot.bot as bot
    decl = inspect.getsource(bot._build_query_data_tool)
    low = decl.lower()
    assert "don't probe the schema" in low or "dont probe" in low, (
        "tool must tell the model not to burn rounds on discovery"
    )
    assert "pragma" in low, "must note PRAGMA is blocked"
    _ok("query_data tells the model not to probe the schema")


def test_final_call_is_guarded():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    after = src.split("hit tool-calling round cap", 1)[1][:4200]
    assert "except" in after, (
        "the final answer call must be exception-guarded — a failure "
        "there must not lose the whole reply"
    )
    _ok("final answer call is exception-guarded")


if __name__ == "__main__":
    print("=== round-cap answer smoke ===")
    test_round_cap_raised()
    test_cap_exhaustion_forces_a_final_answer()
    test_query_data_discourages_schema_probing()
    test_final_call_is_guarded()
    print("\nALL ROUND-CAP ANSWER SMOKE TESTS PASS")
