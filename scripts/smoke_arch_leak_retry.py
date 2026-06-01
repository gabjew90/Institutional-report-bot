"""Smoke test for the /ask architecture-leak rewrite path.

Validates:
  1. _clean_voice_violations correctly detects 'meta-narration' on
     "the chat logs available to me don't cover that block of time in
     enough detail" (the actual phrase that shipped 2026-06-01).
  2. The detection covers the full set of BANNED_META_NARRATION
     phrases relevant to architecture leaks ("available to me",
     "in enough detail to", "the chat logs available", "in my context").
  3. Static check: the retry branch is wired into bot.py source.
"""

import inspect
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# 1. Detection on the actual offending phrase
def test_detection_on_shipped_phrase():
    from discord_bot.bot import _clean_voice_violations
    shipped = (
        "Can't pull a clean summary for that specific window — the chat "
        "logs available to me don't cover that block of time in enough "
        "detail to give you a reliable read on it."
    )
    cleaned, kinds = _clean_voice_violations(shipped)
    assert "meta-narration" in kinds, (
        f"expected 'meta-narration' in kinds for shipped phrase, "
        f"got {sorted(set(kinds))}"
    )
    _ok("detection: 2026-06-01 shipped phrase trips 'meta-narration'")


# 2. Coverage of full set
def test_coverage_of_architecture_leak_phrases():
    from discord_bot.bot import _clean_voice_violations
    phrases = [
        "available to me",
        "in my context",
        "the chat logs available",
        "in enough detail to",
        "the chat logs i have",
        "the data i have",
    ]
    for p in phrases:
        sample = f"Some answer with {p} included in it."
        cleaned, kinds = _clean_voice_violations(sample)
        assert "meta-narration" in kinds, (
            f"phrase {p!r} did NOT trip 'meta-narration' "
            f"(kinds={sorted(set(kinds))})"
        )
    _ok(f"detection: all {len(phrases)} architecture-leak phrases trip 'meta-narration'")


# 3. Clean answer doesn't fire
def test_clean_answer_no_meta_narration():
    from discord_bot.bot import _clean_voice_violations
    clean = (
        "SMCI and TMO are carrying the room right now, HPE/Dell trade as a "
        "pair, rest is just momentum chasing."
    )
    cleaned, kinds = _clean_voice_violations(clean)
    assert "meta-narration" not in kinds, (
        f"clean answer falsely tripped 'meta-narration': {sorted(set(kinds))}"
    )
    _ok("clean answer: no false-positive meta-narration hit")


# 4. Static check: retry branch wired in
def test_retry_branch_wired():
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "meta-narration" in src, "expected 'meta-narration' hit-check in _answer_with_gemini"
    assert "architecture-leak rewrite" in src, (
        "expected architecture-leak rewrite log line in _answer_with_gemini"
    )
    assert "rewrite_prompt" in src, "expected rewrite_prompt assembly in _answer_with_gemini"
    _ok("static: meta-narration retry branch wired in _answer_with_gemini")


if __name__ == "__main__":
    print("=== /ask architecture-leak rewrite smoke ===")
    test_detection_on_shipped_phrase()
    test_coverage_of_architecture_leak_phrases()
    test_clean_answer_no_meta_narration()
    test_retry_branch_wired()
    print("\nALL ARCH-LEAK RETRY SMOKE TESTS PASS")
