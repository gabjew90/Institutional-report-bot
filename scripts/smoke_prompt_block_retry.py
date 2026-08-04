"""Smoke test for the /ask profiles-stripped retry on prompt_block.

Can't run the full _answer_with_gemini end-to-end (needs Gemini key
+ Discord client mocks), so the test does:

  1. Static: the retry branch is wired in _answer_with_gemini
  2. Static: the retry rebuilds user_content WITHOUT profiles_block
     but keeps fetched_urls, chat_context, separator, question
     (analyst_block was dropped from the prompt entirely in Task 9 —
     the lookup_trade_log tool replaces it on demand)
  3. Static: the retry refreshes grounding_metadata
  4. Static: the retry runs _clean_voice_violations on the new answer
  5. Static: the fallback message is still emitted when retry fails
  6. Static: the retry log line names profiles_block stripped chars
"""

import inspect
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_retry_branch_wired():
    """2026-06-03: the retry path was changed from 'strip ALL profile'
    to 'strip Voice subsections only' after empirical reproduction of
    the Ry_bry/Dovahjo AVGO trip showed chat alone trips the filter
    too. The success-log marker is preserved (renamed slightly)."""
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "retrying once with Voice sections" in src or "Voice sections stripped" in src, (
        "expected new 'Voice sections stripped' retry log line in _answer_with_gemini"
    )
    assert "_strip_voice_sections" in src, (
        "retry path must call _strip_voice_sections helper"
    )
    assert "retry succeeded" in src, (
        "expected success log line in _answer_with_gemini"
    )
    _ok("static: Voice-strip retry branch wired in _answer_with_gemini")


def test_stripped_content_keeps_voice_stripped_profile():
    """The retry builds stripped_sections from voice_stripped profile +
    fetched_urls + chat_context + separator+question. Previously this
    test asserted the full profile was dropped; we now keep it
    Voice-stripped because chat alone trips the filter."""
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    retry_start = src.find("stripped_sections: list[str] = [voice_stripped]")
    assert retry_start > 0, (
        "couldn't find new stripped_sections build (expected to be "
        "initialized with [voice_stripped])"
    )
    window = src[retry_start:retry_start + 4500]
    # analyst_block was DROPPED from the prompt in Task 9 — lookup_trade_log
    # replaces it as a tool call. Assert it's NOT in the retry path either.
    assert "stripped_sections.append(analyst_block)" not in window, (
        "retry must NOT include analyst_block — it's been dropped from the prompt"
    )
    assert "stripped_sections.append(fetched_urls)" in window, (
        "retry must include fetched_urls"
    )
    assert "stripped_sections.append(chat_context)" in window, (
        "retry must include chat_context (NOT dropped — the 2026-06-03 "
        "Ry_bry/Dovahjo trip showed chat is needed for context)"
    )
    assert "{separator}\\n{question}" in window, (
        "retry must include separator+question"
    )
    # And the original profiles_block must NOT be re-appended raw
    assert "stripped_sections.append(profiles_block)" not in window, (
        "retry must NOT include the unstripped profiles_block"
    )
    _ok(
        "static: retry rebuilds user_content with voice_stripped profile + "
        "urls + chat + question (NOT the raw profiles_block)"
    )


def test_grounding_metadata_refreshed():
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    retry_start = src.find("stripped_sections: list[str] = [voice_stripped]")
    window = src[retry_start:retry_start + 4500]
    assert (
        "grounding_metadata = (" in window
        and "stripped_resp.candidates[0].grounding_metadata" in window
    ), "retry must refresh grounding_metadata from stripped_resp"
    _ok("static: retry refreshes grounding_metadata for sources footer")


def test_clean_voice_violations_on_recovery():
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    retry_start = src.find("stripped_sections: list[str] = [voice_stripped]")
    window = src[retry_start:retry_start + 4500]
    assert "_clean_voice_violations(" in window, (
        "retry must run _clean_voice_violations on the recovery answer"
    )
    _ok("static: retry runs voice cleanup on the recovered answer")


def test_fallback_still_present():
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert (
        "Gemini bounced this one" in src
        and "Try asking a different way" in src
    ), "fallback message must still be present for when retry also fails"
    # And it must be gated by `if not retry_succeeded`
    fallback_idx = src.find("Gemini bounced this one")
    pre = src[max(0, fallback_idx - 300):fallback_idx]
    assert "if not retry_succeeded" in pre, (
        "fallback must be gated by `if not retry_succeeded`"
    )
    _ok("static: fallback still emits when retry also fails / is skipped")


def test_retry_only_when_profiles_present():
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    # The voice-strip rung must short-circuit when there's no
    # profiles_block to strip — stripping nothing produces the same
    # prompt tier 0 already resent. (2026-08-04: an IDENTICAL retry is
    # now deliberately tier 0 of the ladder — the filter proved
    # non-deterministic, see smoke_filter_retry_tier0 — so this rung's
    # job is purely the content surgery, and it needs content to cut.)
    retry_start = src.find("stripped_sections: list[str] = [voice_stripped]")
    pre = src[max(0, retry_start - 800):retry_start]
    assert "profiles_block and" in pre, (
        "voice-strip rung must be gated on profiles_block being present"
    )
    assert "not retry_succeeded" in pre, (
        "voice-strip rung must be skipped when tier 0 already recovered"
    )
    _ok("static: voice-strip rung gated on profiles_block + tier-0 miss")


if __name__ == "__main__":
    print("=== /ask prompt-block profiles-strip retry smoke ===")
    test_retry_branch_wired()
    test_stripped_content_keeps_voice_stripped_profile()
    test_grounding_metadata_refreshed()
    test_clean_voice_violations_on_recovery()
    test_fallback_still_present()
    test_retry_only_when_profiles_present()
    print("\nALL PROMPT-BLOCK RETRY SMOKE TESTS PASS")
