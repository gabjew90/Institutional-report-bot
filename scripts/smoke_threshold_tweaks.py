"""Smoke test for the two threshold tweaks:

  1. TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS: 500 -> 300
     Lowers the denominator on the activity-multiplier so moderately-
     active users (100-499 msgs) get fairer credit for their chatter
     bracket. The 100-499 bucket is 18 of 62 active users — they were
     getting their bracket scaled down by 0.2-0.6 even when their
     behavior was sharp.

  2. MIN_MESSAGES_FOR_PROFILE_FALLBACK: 100 -> 30
     Aligns the code fallback with settings.profile_min_messages (30).
     The mismatch meant if the settings value ever got unset (env
     var reset), the floor would silently jump 30 -> 100 and 4
     currently-profiled users would lose their profiles on the next
     refresh.

The constants live in scripts/backfill_user_profiles.py.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_activity_multiplier_denominator_is_300():
    """The runtime constant should be 300."""
    import scripts.backfill_user_profiles as bf
    assert bf.TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS == 300, (
        f"expected TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS == 300, "
        f"got {bf.TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS}"
    )
    _ok("TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS = 300 (was 500)")


def test_min_messages_fallback_is_30():
    """The cold-start floor fallback should match settings (30)."""
    import scripts.backfill_user_profiles as bf
    assert bf.MIN_MESSAGES_FOR_PROFILE_FALLBACK == 30, (
        f"expected MIN_MESSAGES_FOR_PROFILE_FALLBACK == 30, "
        f"got {bf.MIN_MESSAGES_FOR_PROFILE_FALLBACK}"
    )
    _ok("MIN_MESSAGES_FOR_PROFILE_FALLBACK = 30 (was 100)")


def test_prompt_text_references_300_not_500():
    """The Gemini-facing prompt text references the denominator in 4
    places. Each should now say 300, not 500."""
    src = open(
        "scripts/backfill_user_profiles.py", encoding="utf-8"
    ).read()
    # The prompt strings + docstrings reference "msg_count / N".
    # After the change, "/ 500" should NOT appear in those contexts.
    bad = []
    for ln_no, ln in enumerate(src.splitlines(), 1):
        # Skip pure comment lines (# ...)
        if ln.lstrip().startswith("#"):
            continue
        # Look for /500 in prompt-text refs to activity multiplier.
        if "msg_count / 500" in ln or "/ 500)" in ln:
            bad.append((ln_no, ln.strip()))
    if bad:
        for ln_no, ln in bad[:5]:
            print(f"    line {ln_no}: {ln[:120]}")
        _fail(
            f"{len(bad)} prompt-text reference(s) to /500 remain — "
            "Gemini would frame its chatter brackets around the old "
            "denominator"
        )
    _ok("prompt text consistently references /300 (no stale /500 refs)")


if __name__ == "__main__":
    print("=== threshold tweaks smoke ===")
    test_activity_multiplier_denominator_is_300()
    test_min_messages_fallback_is_30()
    test_prompt_text_references_300_not_500()
    print("\nALL THRESHOLD-TWEAKS SMOKE TESTS PASS")
