"""Smoke test for _strip_voice_sections helper + Voice-strip retry path.

Background: 2026-06-03 19:49 UTC Ry_bry/Dovahjo AVGO replies got the
'Gemini bounced this one' wrapper. Investigation showed:

  - The unconfigurable filter trips on slur-heavy chat context, NOT
    only on slur-heavy profiles.
  - The pre-fix retry path stripped the ENTIRE profile block but
    kept chat_context. Empirically: profile-only-stripped retry
    BLOCKED (3/3 runs); chat alone trips with slur lines like
    'BK (bankerkyle): Nigga'.
  - Stripping ONLY the **Voice.** subsections from each profile (which
    quote each user's chat verbatim, slurs included) clears the
    trip 3/3 runs and is only 362 chars / 4.4% of the prompt.

This smoke locks the helper + the retry-path wiring in place.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_helper_drops_voice_bullets():
    from discord_bot.bot import _strip_voice_sections
    sample = (
        "**Personality and style.**\nchaos.\n\n"
        "**Voice.**\n"
        "- \"BING BONG\" — [winning kicker]\n"
        "- \"Nigga\" — [filler]\n"
        "- \"I am gonna scream nigger\" — [tilted]\n\n"
        "**Retarded takes.**\n"
        "- thing 1\n"
        "- thing 2\n"
    )
    out = _strip_voice_sections(sample)
    assert "BING BONG" not in out, "Voice sample 'BING BONG' should be stripped"
    assert "Nigga" not in out, "Voice sample slurs should be stripped"
    assert "Personality and style" in out, "Personality section must remain"
    assert "Retarded takes" in out, "Retarded takes section must remain"
    assert "thing 1" in out, "Retarded takes bullets must remain"
    _ok("Voice bullets stripped; other sections + bullets preserved")


def test_helper_keeps_voice_header():
    """The replacement leaves a stub `**Voice.**\\n- (omitted...)\\n`
    so the schema isn't visibly broken — the model still knows this
    user has a Voice section conceptually."""
    from discord_bot.bot import _strip_voice_sections
    sample = (
        "**Voice.**\n"
        "- \"X\" — [a]\n"
        "- \"Y\" — [b]\n"
    )
    out = _strip_voice_sections(sample)
    assert "**Voice.**" in out, "Voice header should remain as a stub"
    assert "omitted" in out.lower(), "Replacement should be visible"
    _ok("Voice header preserved as stub with 'omitted' marker")


def test_helper_noop_on_voice_free_profile():
    from discord_bot.bot import _strip_voice_sections
    sample = (
        "**Personality and style.**\nchaos.\n\n"
        "**Retarded takes.**\n- thing 1\n"
    )
    out = _strip_voice_sections(sample)
    assert out == sample, f"profile without Voice should be unchanged: {out!r}"
    _ok("no-op on profile without a Voice section")


def test_helper_handles_indented_bullets_defensively():
    """Production prompts don't indent bullets, but defensive parse
    handles them anyway in case future schema changes add indentation."""
    from discord_bot.bot import _strip_voice_sections
    sample = (
        "  **Voice.**\n"
        "  - \"BING BONG\"\n"
        "  - \"Nigga\"\n"
        "  **Retarded takes.**\n"
        "  - thing 1\n"
    )
    out = _strip_voice_sections(sample)
    assert "BING BONG" not in out
    assert "Nigga" not in out
    assert "Retarded takes" in out
    _ok("indented bullets handled defensively")


def test_helper_strips_multiple_profiles():
    """The block contains one bullet per profiled user. Each user's
    Voice section gets stripped independently."""
    from discord_bot.bot import _strip_voice_sections
    sample = (
        "**Voice.**\n- \"BING BONG\"\n- \"Nigga\"\n\n"
        "**Recent trades.**\n- TSLA\n\n"
        "**Voice.**\n- \"chocolate\"\n- \"playing in the mud\"\n\n"
        "**Recent trades.**\n- AAPL\n"
    )
    out = _strip_voice_sections(sample)
    assert "BING BONG" not in out
    assert "chocolate" not in out
    assert "playing in the mud" not in out
    assert "TSLA" in out and "AAPL" in out
    # Both Voice stubs should be present
    assert out.count("**Voice.**") == 2
    _ok("multiple Voice sections in same block stripped independently")


def test_retry_path_calls_strip_helper():
    """Sanity check the retry block in _answer_with_gemini actually
    uses _strip_voice_sections rather than dropping profiles_block
    entirely. Locks the wiring."""
    import inspect
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_strip_voice_sections" in src, (
        "retry path must call _strip_voice_sections rather than dropping "
        "profiles_block entirely (the old behavior failed when chat alone "
        "tripped the filter)"
    )
    assert "voice_stripped" in src, (
        "retry path should keep voice_stripped as a named local for "
        "clarity / future debugging"
    )
    _ok("retry path in _answer_with_gemini calls _strip_voice_sections")


if __name__ == "__main__":
    print("=== Voice-strip retry smoke ===")
    test_helper_drops_voice_bullets()
    test_helper_keeps_voice_header()
    test_helper_noop_on_voice_free_profile()
    test_helper_handles_indented_bullets_defensively()
    test_helper_strips_multiple_profiles()
    test_retry_path_calls_strip_helper()
    print("\nALL VOICE-STRIP RETRY SMOKE TESTS PASS")
