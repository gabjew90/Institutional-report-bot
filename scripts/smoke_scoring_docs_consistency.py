"""Smoke: scoring rubric teaches exactly ONE mechanism — the real one.

Context (2026-07-29 scoring review): the trader-score rubric inside
PROFILE_PROMPT had accreted three incompatible mechanics — a removed
"honesty modifier" formula, a stale 14-day decay window next to the
real 21-day banded ledger, the pre-wins-only +3/+5/ghost point
schedule with worked examples doing "wins x 5" math, a "final 100 cap"
the code doesn't apply, and a /500 activity divisor that's actually
/300. Score ARITHMETIC was never wrong (code computes all points; the
LLM only picks the chatter bracket) — but the model writes rationales
while reading contradictory mechanics.

Also covers the leaderboard requested-N change: Mode C honors the
asked-for size up to 10 (default 5) instead of hardcoding 5.
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


def test_stale_mechanics_gone():
    import scripts.backfill_user_profiles as bf
    src = inspect.getsource(bf)
    for stale, name in [
        ("chatter_base + honesty", "removed honesty-modifier formula"),
        ("rolling 14-day window", "stale 14-day decay window"),
        ("+3 per entry posted", "pre-wins-only member point schedule"),
        ("wins × 5", "worked examples using the old +5 win value"),
        ("the only ceiling is the final 100 cap", "phantom 100 cap"),
        ("msgs/500", "stale /500 activity divisor"),
    ]:
        assert stale not in src, f"stale mechanic still taught: {name}"
    _ok("all five stale scoring mechanics removed")


def test_real_mechanics_present():
    from scripts.backfill_user_profiles import PROFILE_PROMPT as P
    assert "2 pts within 7 days" in P, "wins-only banded values missing"
    assert "min(1, msg_count / 300)" in P, "real activity divisor missing"
    assert "TOTAL RECEIPT POINTS FROM THE LEDGER BLOCK, VERBATIM" in P, (
        "no-inference ledger rule must survive"
    )
    _ok("the one true mechanism still fully described")


def test_leaderboard_requested_n():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._execute_user_profile)
    assert "min(" in src and "10" in src and "top_n" in src, (
        "Mode C must honor requested top_n capped at 10"
    )
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    assert "top 5" in S, "default top-5 framing must survive (diet anchor)"
    assert "max 10" in S, "prompt must state the requested-N cap"
    _ok("leaderboard honors requested N (default 5, max 10)")


if __name__ == "__main__":
    print("=== scoring docs consistency smoke ===")
    test_stale_mechanics_gone()
    test_real_mechanics_present()
    test_leaderboard_requested_n()
    print("\nALL SCORING DOCS CONSISTENCY SMOKE TESTS PASS")
