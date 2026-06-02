"""Smoke test that the trader_score formula no longer caps at 100."""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_score_formula_uses_additive_no_cap():
    """The runtime formula line should be `max(0, scaled_chatter +
    receipt_pts)` — no `min(100, ...)` clip."""
    import re
    src = open("scripts/backfill_user_profiles.py", encoding="utf-8").read()
    # Positive identification of the new formula
    assert (
        "max(0, _scaled_chatter + _receipt_pts)" in src
    ), "expected `max(0, _scaled_chatter + _receipt_pts)` in source (no cap)"
    # Negative: there should be no `min(100, ...)` left in the RUNTIME
    # formula line. (Comments / docstrings / prompt-text may still
    # mention it for historical context.)
    runtime_lines = [
        ln for ln in src.splitlines()
        if "trader_score =" in ln and not ln.lstrip().startswith("#")
    ]
    for ln in runtime_lines:
        if "min(100" in ln:
            _fail(f"runtime formula still has min(100,...): {ln.strip()!r}")
    _ok("formula uses additive `max(0, scaled_chatter + receipt_pts)` (no cap)")


def test_prompt_text_no_longer_promises_cap():
    """Prompt-text strings shown to Gemini should no longer assert
    the min(100,...) cap as truth — would mislead the model into
    rubric-anchoring around a cap that's been removed."""
    src = open("scripts/backfill_user_profiles.py", encoding="utf-8").read()
    # Look for prompt-string lines (start with quote or f-quote) that
    # still mention min(100,...) as the formula.
    suspect_lines = []
    for ln in src.splitlines():
        stripped = ln.strip()
        if "min(100" not in ln or stripped.startswith("#"):
            continue
        # Skip lines that legitimately use min(100, ...) for non-
        # trader_score reasons:
        #   - racial_humor_score (rh) is intentionally 0-100 bounded
        #   - the runtime trader_score = ... line is checked separately
        if "rh = " in ln or "trader_score =" in ln:
            continue
        suspect_lines.append(ln)
    if suspect_lines:
        for ln in suspect_lines[:5]:
            print(f"    suspect: {ln.strip()[:120]}")
        _fail(
            f"{len(suspect_lines)} non-runtime line(s) still mention "
            "min(100,...) — prompt text or docstrings would mislead Gemini "
            "into anchoring around a cap that doesn't exist"
        )
    _ok("prompt-text / docstring references to min(100,...) removed")


if __name__ == "__main__":
    print("=== no-cap formula smoke ===")
    test_score_formula_uses_additive_no_cap()
    test_prompt_text_no_longer_promises_cap()
    print("\nALL NO-CAP SMOKE TESTS PASS")
