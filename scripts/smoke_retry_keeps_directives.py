"""Smoke: answer-regenerating retries must keep the per-request directives.

2026-07-30 failure: "analyze trades opened by analysts relative to qqq"
completed correctly — guards show `analysis-directive, code-charts:1` —
and then the REPETITION RETRY replaced the whole analysis with a
personal roast of the asker ("f.jamal is sitting in a Dallas Marriott
bed at 3 AM crying over his webhook scripts...").

Cause: every retry rebuilt the system instruction with a bare
_build_runtime_system_instruction(), dropping `_prompt_extra` — the
per-request FACT / ANALYSIS directives. Without the analysis directive
the model reverts to default banter, and the repetition retry runs at
temperature 0.7, so it roasted instead of analyzing.

Every retry that REGENERATES THE ANSWER must pass _prompt_extra. Only
the initial config may build bare, because it's patched immediately
after the router returns.
"""

import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_only_initial_config_builds_bare():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    bare = len(re.findall(
        r"_build_runtime_system_instruction\(\)", src))
    assert bare <= 1, (
        f"{bare} call sites build the instruction WITHOUT _prompt_extra; "
        f"only the initial config may (it's patched after the router). "
        f"A retry without the directives reverts to banter."
    )
    _ok(f"only {bare} bare build (the initial config); retries carry extras")


def test_retries_pass_prompt_extra():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    withx = len(re.findall(
        r"_build_runtime_system_instruction\(_prompt_extra\)", src))
    # initial patch + repetition + revoice + grounding + TA
    assert withx >= 5, (
        f"expected >=5 directive-preserving builds, found {withx}"
    )
    _ok(f"{withx} call sites preserve the per-request directives")


def test_repetition_retry_specifically():
    """The retry that caused the incident, checked by name."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    seg = src.split("retry_config = types.GenerateContentConfig(", 1)
    assert len(seg) == 2, "repetition retry_config not found"
    window = seg[1][:400]
    assert "_prompt_extra" in window, (
        "the repetition retry must keep the analysis/FACT directive — "
        "without it, temperature 0.7 turns an analysis into a roast"
    )
    _ok("repetition retry keeps the per-request directive")


if __name__ == "__main__":
    print("=== retry directive-preservation smoke ===")
    test_only_initial_config_builds_bare()
    test_retries_pass_prompt_extra()
    test_repetition_retry_specifically()
    print("\nALL RETRY DIRECTIVE SMOKE TESTS PASS")
