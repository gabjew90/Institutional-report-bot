"""Smoke test: conv-scoped racism-rank is not a global rank (2026-06-24).

The WHO'S TALKING block ranks racism ONLY among the people active in the
current conversation. When sunny was the only racism-scored person
active, his block read "racism-rank #1/1 in this conv" and the bot told
him "you're actually #1" — contradicting the global top-5 it had just
given (sunny absent, ZHawk #1). Two fixes:
  (1) db.format_user_profiles_for_context suppresses the meaningless
      tiny-denominator ordinal (<3 active) and labels the scope loudly
      when it does rank.
  (2) a binding /ask prompt rule: conv-scoped rank != global leaderboard;
      global-standing questions must use lookup_user_profile.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _profile(uid, name, humor):
    return {
        "display_name": name, "username": name.lower(),
        "racial_humor_score": humor, "slur_count": 19,
        "racism_rationale": "uses race-edged banter",
        "trader_rationale": "mid-pack", "profile_text": f"{name} is a trader.",
        "message_count_at_update": 700,
    }


def test_single_scored_user_no_meaningless_rank():
    import db
    profiles = {318: _profile(318, "SUNNY", 65)}
    with patch("db.get_profiles_for_users", return_value=profiles), \
         patch("db.get_global_trader_ranks", return_value=({318: 21}, 54)):
        out = db.format_user_profiles_for_context([318])
    assert "#1/1 in this conv" not in out, "meaningless '#1 of 1' must be gone"
    assert "racism-rank #1" not in out, f"must not assert a global-looking #1: {out}"
    assert "too few active here to rank" in out, out
    # the real signal is still shown (slurs), and the global pointer is there
    assert "slurs:19" in out and "lookup_user_profile" in out, out
    _ok("annotation: single racism-scored user gets signal, NOT a '#1/1' rank")


def test_three_scored_users_rank_with_loud_scope():
    import db
    profiles = {
        1: _profile(1, "ZHawk", 90),
        2: _profile(2, "SV", 80),
        3: _profile(3, "SUNNY", 65),
    }
    with patch("db.get_profiles_for_users", return_value=profiles), \
         patch("db.get_global_trader_ranks", return_value=({1: 5, 2: 6, 3: 21}, 54)):
        out = db.format_user_profiles_for_context([1, 2, 3])
    # With >=3 active it DOES rank, but the scope is made unmistakable.
    assert "ACTIVE here" in out and "NOT the global leaderboard" in out, out
    assert "racism-rank #1 of 3" in out, out
    _ok("annotation: >=3 active → ranked, but scope is loudly conv-not-global")


def test_prompt_rule_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    assert "CONV-SCOPED rank ≠ GLOBAL leaderboard" in ins, "rule missing"
    assert "not a license to invent a rank" in ins.lower() or \
        "roast is not a license to invent a rank" in ins.lower(), \
        "the no-fabricated-rank clause is missing"
    assert "lookup_user_profile" in ins, "must point global asks at the tool"
    _ok("prompt: conv-scoped-vs-global rank rule present + tool pointer")


if __name__ == "__main__":
    print("=== conv-rank scope smoke ===")
    test_single_scored_user_no_meaningless_rank()
    test_three_scored_users_rank_with_loud_scope()
    test_prompt_rule_present()
    print("\nALL CONV-RANK SCOPE SMOKE TESTS PASS")
