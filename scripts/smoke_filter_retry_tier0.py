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


def test_tier0_resends_unchanged():
    """Same contents, same config — not a rebuilt section list."""
    import discord_bot.bot as bot
    lad = _ladder(inspect.getsource(bot._answer_with_gemini))
    t0 = lad.find('"same-prompt"')
    strip = lad.find("_strip_voice_sections")
    window = lad[:strip]
    assert "contents=contents" in window, (
        "tier 0 must resend the ORIGINAL contents object unchanged"
    )
    assert t0 != -1 and "config=config" in window, (
        "tier 0 must reuse the original config (tools, system "
        "instruction, safety settings)"
    )
    _ok("tier 0 resends the original contents + config verbatim")


if __name__ == "__main__":
    print("=== filter-ladder tier-0 smoke ===")
    test_tier0_identical_retry_exists()
    test_tier0_runs_before_any_mutation()
    test_tier0_resends_unchanged()
    print("\nALL FILTER-LADDER TIER-0 SMOKE TESTS PASS")
