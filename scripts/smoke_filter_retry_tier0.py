"""Smoke: the filter-block ladder retries the IDENTICAL prompt first.

2026-08-01 — "how many members in ommi chat" (nothing filterable in the
question) died on every rung of the ladder: voice-strip, slur-mask and
question-only all came back empty within one window, and the user got
the "Gemini bounced this one" wrapper.

2026-08-04 — replaying the EXACT logged prompt, byte-identical, against
the same model: passes 5/5 (full prompt, bare question, profiles alone,
with and without the system instruction). Gemini's unconfigurable
filter is non-deterministic near its threshold — the same content
flickers between pass and block across runs.

The ladder's tiers all MUTATE content on the assumption that some
ingredient is toxic. For a flickering filter the cheapest correct rung
is: send the same thing again. Tier 0 must be an identical retry —
same contents, same config — BEFORE any context gets amputated, so a
transient block costs nothing instead of degrading the answer (or
failing outright, as it did here).
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


def _ladder(src):
    i = src.find("if safety_blocked or prompt_block:")
    assert i != -1, "filter-block branch not found"
    j = src.find('_ask_meta["filter_retry"] = "failed"', i)
    assert j != -1, "ladder failure stamp not found"
    return src[i:j]


def test_tier0_identical_retry_exists():
    import discord_bot.bot as bot
    lad = _ladder(inspect.getsource(bot._answer_with_gemini))
    assert '"same-prompt"' in lad, (
        "no identical-retry tier — the ladder starts amputating context "
        "on the first block, but the filter is non-deterministic and "
        "the same prompt often passes on resend"
    )
    _ok("tier 0 (identical retry) exists in the ladder")


def test_tier0_runs_before_any_mutation():
    import discord_bot.bot as bot
    lad = _ladder(inspect.getsource(bot._answer_with_gemini))
    t0 = lad.find('"same-prompt"')
    strip = lad.find("_strip_voice_sections")
    assert strip != -1, "voice-strip tier missing"
    assert t0 < strip, (
        "identical retry must be the FIRST rung — before voice-strip — "
        "so a transient block costs no context"
    )
    _ok("tier 0 runs before voice-strip")


def test_tier0_resends_original_contents():
    """Original contents object — not a rebuilt section list."""
    import discord_bot.bot as bot
    lad = _ladder(inspect.getsource(bot._answer_with_gemini))
    t0 = lad.find('"same-prompt"')
    strip = lad.find("_strip_voice_sections")
    window = lad[:strip]
    assert t0 != -1 and "contents=contents" in window, (
        "tier 0 must resend the ORIGINAL contents object unchanged"
    )
    _ok("tier 0 resends the original contents verbatim")


def test_ladder_strips_function_tools():
    """2026-08-07, SV's "summarize the last 12 hours of chat": every
    tier resent with the ORIGINAL config, which carries all eight
    function tools. A single generate_content call with tools attached,
    on a question that REQUIRES a tool, returns a function_call part
    with empty .text — no tier executes tools, so all four tiers read
    as "empty" and the ladder shipped the failure wrapper for a reason
    unrelated to the filter. Retry tiers must use a tools-stripped
    config so the model answers from the context already in the prompt
    (the recent chat window rides every ask)."""
    import discord_bot.bot as bot
    lad = _ladder(inspect.getsource(bot._answer_with_gemini))
    assert "_ladder_config" in lad, (
        "no tools-stripped ladder config — tool-dependent questions "
        "make every tier return a function_call (empty text) and the "
        "ladder false-fails"
    )
    assert lad.count("config=config,") == 0, (
        "a ladder tier still reuses the tools-bearing original config"
    )
    assert lad.count("config=_ladder_config") >= 4, (
        f"all four tiers must use the stripped config, found "
        f"{lad.count('config=_ladder_config')}"
    )
    _ok("all ladder tiers use a tools-stripped config")


if __name__ == "__main__":
    print("=== filter-ladder tier-0 smoke ===")
    test_tier0_identical_retry_exists()
    test_tier0_runs_before_any_mutation()
    test_tier0_resends_original_contents()
    test_ladder_strips_function_tools()
    print("\nALL FILTER-LADDER TIER-0 SMOKE TESTS PASS")
