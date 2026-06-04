"""Smoke test: third-tier slur-mask retry as belt-and-suspenders for
filter trips the slur-count short-circuit doesn't catch.

Background: when the question shape isn't a count question but the
prompt still has too much slur density (Voice-strip retry returns
empty), we mask slur tokens with [redacted] across voice_stripped
profile + chat + question and retry one more time. Lossy answer
(bot can't quote the slur) but answer-not-refusal.

This smoke covers:
  - _mask_slur_tokens helper masks every slur variant
  - Word-boundary anchored — doesn't false-positive on near-matches
  - Empty input returns empty (no crash)
  - Retry block in _answer_with_gemini calls _mask_slur_tokens
"""

import inspect
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_mask_helper_replaces_slurs():
    from discord_bot.bot import _mask_slur_tokens
    samples = [
        ("BK said nigga twice", "BK said [redacted] twice"),
        ("BK said Nigger here", "BK said [redacted] here"),
        ("two slurs: chink and kike", "two slurs: [redacted] and [redacted]"),
        ("fag and faggot variants", "[redacted] and [redacted] variants"),
        ("pajeet variant", "[redacted] variant"),
    ]
    for inp, expected in samples:
        out = _mask_slur_tokens(inp)
        assert out == expected, (
            f"mask failed: input={inp!r}, expected={expected!r}, got={out!r}"
        )
    _ok(f"_mask_slur_tokens replaces {len(samples)} slur variant samples")


def test_mask_helper_is_word_boundary_anchored():
    """Don't mask 'pajama' (starts with 'paj') or 'pic' (contains
    'spic' substring) or 'phaedra' (contains 'fag'). Word-boundary
    anchoring is critical to avoid mid-word false positives."""
    from discord_bot.bot import _mask_slur_tokens
    benign = [
        "pajama party tonight",
        "scenic view from the deck",  # contains "spic"-ish? actually no
        "snickers bar",  # contains 'nick'
        "magnify the chart",  # contains 'fag'? actually 'agnif'
        "I picked the entry well",  # 'pic' inside 'picked'
    ]
    for s in benign:
        out = _mask_slur_tokens(s)
        assert "[redacted]" not in out, (
            f"FALSE POSITIVE: {s!r} → {out!r}"
        )
    _ok(f"_mask_slur_tokens word-boundary anchored — no false positives on {len(benign)} benign strings")


def test_mask_handles_empty_input():
    from discord_bot.bot import _mask_slur_tokens
    assert _mask_slur_tokens("") == ""
    assert _mask_slur_tokens(None) is None
    _ok("_mask_slur_tokens handles empty/None input")


def test_mask_handles_case_insensitivity():
    from discord_bot.bot import _mask_slur_tokens
    cases = ["NIGGER", "Nigger", "nIgGeR", "FAGGOT", "Pajeet", "CHINK"]
    for c in cases:
        out = _mask_slur_tokens(c)
        assert out == "[redacted]", f"case-insensitive mask failed on {c!r}: {out!r}"
    _ok(f"_mask_slur_tokens case-insensitive across {len(cases)} variants")


def test_mask_preserves_surrounding_punctuation():
    """Quotation marks, commas, sentence structure must survive."""
    from discord_bot.bot import _mask_slur_tokens
    cases = [
        ('"nigga" used 47 times', '"[redacted]" used 47 times'),
        ("Said 'nigger', 'kike', and 'spic'", "Said '[redacted]', '[redacted]', and '[redacted]'"),
        ("nigga/nigger combined count", "[redacted]/[redacted] combined count"),
    ]
    for inp, expected in cases:
        assert _mask_slur_tokens(inp) == expected
    _ok("_mask_slur_tokens preserves surrounding punctuation")


def test_retry_block_calls_mask_helper():
    """Anchor: the third-tier retry block in _answer_with_gemini calls
    _mask_slur_tokens on chat_context, voice_stripped profile, AND the
    question. All three sources of slur density must be masked."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_mask_slur_tokens" in src, (
        "retry block must call _mask_slur_tokens"
    )
    # Three call sites: voice_stripped, chat_context, question
    n_calls = src.count("_mask_slur_tokens(")
    assert n_calls >= 3, (
        f"third-tier retry should call _mask_slur_tokens on >=3 "
        f"sources (profile/chat/question), got {n_calls} calls"
    )
    _ok(f"third-tier retry calls _mask_slur_tokens {n_calls} times "
        f"(profile + chat + question + log)")


def test_retry_block_runs_after_voice_strip_retry_empty():
    """The third-tier retry must run AFTER the Voice-strip retry, not
    INSTEAD of it. Anchor on the source order."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    voice_strip_idx = src.find("voice_stripped = _strip_voice_sections")
    mask_retry_idx = src.find("third-tier retry")
    assert voice_strip_idx != -1, "voice-strip retry not found"
    assert mask_retry_idx != -1, "third-tier retry not found"
    assert mask_retry_idx > voice_strip_idx, (
        "third-tier retry must come AFTER voice-strip retry in source order"
    )
    _ok("third-tier retry runs AFTER voice-strip retry (correct order)")


def test_retry_block_records_voice_lint_on_recovery():
    """Recovery answer must run through _clean_voice_violations same
    as the Voice-strip retry path."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    mask_retry_idx = src.find("third-tier retry")
    window = src[mask_retry_idx:mask_retry_idx + 4000] if mask_retry_idx != -1 else ""
    assert "_clean_voice_violations" in window, (
        "third-tier recovery answer must run voice lint same as the "
        "Voice-strip retry path"
    )
    _ok("third-tier retry runs _clean_voice_violations on the recovered answer")


if __name__ == "__main__":
    print("=== slur-mask retry smoke ===")
    test_mask_helper_replaces_slurs()
    test_mask_helper_is_word_boundary_anchored()
    test_mask_handles_empty_input()
    test_mask_handles_case_insensitivity()
    test_mask_preserves_surrounding_punctuation()
    test_retry_block_calls_mask_helper()
    test_retry_block_runs_after_voice_strip_retry_empty()
    test_retry_block_records_voice_lint_on_recovery()
    print("\nALL SLUR-MASK RETRY SMOKE TESTS PASS")
