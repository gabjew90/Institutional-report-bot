"""Smoke: rank-trajectory guard (2026-07-05).

lookup_user_profile returns only the CURRENT rank (#N/M) — there is no
historical rank data. So "you lost your top-5 spot", "dropped from #3",
"used to be #1", "climbed the board" are invented. The bot told SV he
"lost your spot in the top 5" three hours after stating he was #9 — no
top-5 to lose. Same family as the time-series guard: a snapshot narrated
as a trajectory. Movement claims get stripped; the current rank + jab
survive.
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


def test_detector_fires_on_movement_claims():
    import discord_bot.bot as bot
    for s in [
        "all you did was lose your edge and your spot in the top 5.",
        "you dropped from #3 to #9 this week.",
        "you used to be #1 but the board moved on.",
        "you climbed the leaderboard fast.",
        "monsoon and big g knocked you out of the top 5.",
    ]:
        v = bot._rank_trajectory_violations(s)
        assert len(v) == 1, f"movement claim must flag: {s!r} -> {v}"
    _ok("detector: rank-movement/history sentences flagged")


def test_detector_leaves_current_rank_and_non_rank_alone():
    import discord_bot.bot as bot
    # a CURRENT rank statement (no movement verb) is fine
    for s in [
        "you're sitting at #13, right in the heavy-usage bracket.",
        "spamming the hard-r doesn't make you #1.",
        "you're #9, a predictable core feature of your daily volatility.",
    ]:
        assert bot._rank_trajectory_violations(s) == [], f"current rank ok: {s!r}"
    # movement verbs OUTSIDE a rank context must not trip it
    for s in [
        "you dropped the ball on that GEO trade.",
        "the stock climbed 12% after the print.",
        "you fell for the pump again.",
    ]:
        assert bot._rank_trajectory_violations(s) == [], f"non-rank move ok: {s!r}"
    _ok("detector: current-rank statements + non-rank movement left alone")


def test_strip_keeps_the_jab():
    import discord_bot.bot as bot
    ans = ("You're stuck at the pharmacy dealing with crackheads. You took "
           "two weeks off and lost your spot in the top 5. You're just a "
           "Long Island pharmacist with too much beta.")
    v = bot._rank_trajectory_violations(ans)
    assert len(v) == 1, v
    stripped = bot._strip_sentences(ans, v)
    assert "top 5" not in stripped, "the fabricated trajectory must be gone"
    assert "pharmacy" in stripped and "Long Island pharmacist" in stripped, \
        "the sourced jabs must survive"
    _ok("strip: fabricated trajectory removed, the real jabs survive")


def test_guard_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_rank_trajectory_violations(answer)" in src, "guard not wired"
    # getsource returns the split string literals, so check substrings
    # that don't straddle a line break.
    window = src.split("Rank-trajectory guard", 1)[1][:4500]
    assert "rank history" in window and "Add no new facts" in window, \
        "rewrite directive must forbid history + new facts"
    assert "_strip_sentences(answer, _rank_viol)" in window, "strip fallback missing"
    _ok("guard wired: detect -> no-history rewrite -> strip fallback")


if __name__ == "__main__":
    print("=== /ask rank-trajectory guard smoke ===")
    test_detector_fires_on_movement_claims()
    test_detector_leaves_current_rank_and_non_rank_alone()
    test_strip_keeps_the_jab()
    test_guard_wired()
    print("\nALL RANK-TRAJECTORY SMOKE TESTS PASS")
