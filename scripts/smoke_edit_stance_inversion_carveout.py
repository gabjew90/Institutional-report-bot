"""Smoke test: EDIT prompt has carve-outs that preserve stance-inversion
callouts DRAFT writes.

Background: 2026-06-05 QC found two stance inversions ($AVGO short→long,
ECB hike-asymmetry inverted) that the bot did NOT name in the pulse,
despite the synthesizer shipping a binding STANCE-INVERSION NAMING rule
to DRAFT in `report/synthesizer.py` (rule #5 in prev_context + yesterday's
markdown injection). Investigation: the EDIT prompt in `ai_analysis/
prompts.py` says "Each pulse is standalone. Do not reference previous
pulses, do not compare to yesterday's themes" — DIRECT contradiction.
Even when DRAFT wrote "this reverses yesterday's $AVGO call," EDIT
would strip it as a previous-pulse reference.

Fix: carve out a narrow exception inside the EDIT prompt that preserves
ONE-SENTENCE stance-inversion callouts inside the theme that owns them,
without re-opening the gate to general prev-pulse commentary.

This smoke covers:
  - Mid-prompt cull rule still says "standalone for CULL decisions"
    (intent preserved: don't carry themes by inertia)
  - Mid-prompt has the explicit stance-inversion EXCEPTION block
  - Closing instruction references the same exception
  - Concrete inversion-callout examples are quoted so EDIT recognizes
    the shape ($AVGO, ECB, payrolls — the observed misses)
  - One-sentence cap is explicit (prevents EDIT from over-extending
    into multi-paragraph day-over-day comparison)
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


def _load_prompts_source():
    from ai_analysis import prompts
    return inspect.getsource(prompts)


def test_cull_rule_intent_preserved():
    """The original 'standalone for cull decisions' intent must remain —
    we don't want EDIT carrying themes forward by inertia just because
    yesterday covered them."""
    src = _load_prompts_source()
    assert "standalone for CULL decisions" in src, (
        "the mid-prompt cull rule must keep its 'standalone for cull "
        "decisions' framing — the carve-out is scoped to inversion "
        "callouts only, not to cull logic"
    )
    # The 'did yesterday cover it?' anti-inertia bit must still be there
    assert "did yesterday cover it" in src, (
        "anti-inertia line ('not did yesterday cover it') must remain"
    )
    _ok("cull-rule intent preserved (no inertia from yesterday's coverage)")


def test_mid_prompt_has_inversion_exception():
    """The mid-prompt EDIT pass must explicitly carve out the stance-
    inversion exception with a recognizable header."""
    src = _load_prompts_source()
    assert "Stance-inversion exception" in src, (
        "mid-prompt must contain a 'Stance-inversion exception' header "
        "so EDIT recognizes the carve-out as an EXCEPTION, not as a "
        "competing rule it has to reconcile"
    )
    # Must explicitly instruct EDIT to PRESERVE these callouts
    assert "PRESERVE these" in src or "Preserve these" in src, (
        "the carve-out must explicitly say PRESERVE, not just describe "
        "the exception (EDIT defaults to stripping anything that looks "
        "like a prev-pulse reference)"
    )
    _ok("mid-prompt contains explicit Stance-inversion exception with "
        "PRESERVE directive")


def test_carveout_quotes_concrete_examples():
    """The carve-out must include concrete example callouts so EDIT
    can pattern-match. The 2026-06-05 QC observed three specific
    inversions: $AVGO short→long, ECB hike asymmetry, payrolls bias."""
    src = _load_prompts_source()
    # At least one of the observed inversion shapes must be quoted
    inversion_shapes = [
        "AVGO",
        "ECB asymmetry",
        "labor-print bias",
        "below-consensus to above",
        "reverses yesterday",
    ]
    matched = [s for s in inversion_shapes if s in src]
    assert len(matched) >= 2, (
        f"carve-out should quote >=2 concrete inversion shapes so EDIT "
        f"recognizes them, got {matched}. Checked: {inversion_shapes}"
    )
    _ok(f"carve-out quotes {len(matched)} concrete inversion-callout "
        f"examples ({matched})")


def test_carveout_caps_at_one_sentence():
    """The exception must be narrow — one sentence inside the theme,
    NOT multi-paragraph day-over-day commentary. Without the cap, EDIT
    treats this as license to add a 'Since yesterday:' frame back."""
    src = _load_prompts_source()
    cap_phrases = [
        "one sentence",
        "one-sentence",
        "ONE sentence",
        "ONE-SENTENCE",
    ]
    assert any(p in src for p in cap_phrases), (
        "carve-out must explicitly cap inversion callouts at one "
        "sentence to prevent EDIT from over-extending"
    )
    # And it must say "do not extend" or equivalent prohibition
    assert ("Do not extend" in src or "do not extend" in src
            or "not invent inversions" in src
            or "do not invent" in src), (
        "carve-out must explicitly forbid EDIT from inventing inversions "
        "or extending the callout — preservation only, not generation"
    )
    _ok("carve-out caps at one sentence + forbids EDIT from extending "
        "or inventing inversions")


def test_closing_instruction_references_exception():
    """The very last line of the EDIT prompt (the closing 'standalone'
    repeat) must also reference the carve-out — otherwise EDIT sees a
    final instruction that contradicts the mid-prompt exception."""
    src = _load_prompts_source()
    # Anchor: the closing repeat exists
    assert "Each pulse is standalone — do not compare to or reference previous pulses" in src, (
        "closing 'Each pulse is standalone — do not compare to or "
        "reference previous pulses' instruction missing"
    )
    # And it now has the EXCEPT clause
    closing_window_start = src.rfind(
        "Each pulse is standalone — do not compare to or reference previous pulses"
    )
    closing_window = src[closing_window_start:closing_window_start + 500]
    assert "EXCEPT" in closing_window or "except preserve" in closing_window.lower(), (
        "the closing standalone-instruction must include an EXCEPT "
        "clause referencing the stance-inversion carve-out, otherwise "
        "EDIT's last instruction contradicts the mid-prompt exception"
    )
    _ok("closing standalone-instruction references the stance-inversion "
        "exception (no last-line contradiction)")


def test_synthesizer_still_ships_inversion_rule():
    """Sanity check: the DRAFT-side STANCE-INVERSION NAMING rule in
    synthesizer.py is unchanged. The EDIT carve-out only works because
    DRAFT actually writes the callout in the first place."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "STANCE-INVERSION NAMING" in src, (
        "DRAFT-side STANCE-INVERSION NAMING rule got removed — the "
        "EDIT carve-out preserves callouts DRAFT makes, but if DRAFT "
        "stops making them, the carve-out has nothing to preserve"
    )
    assert "YESTERDAY'S PULSE MARKDOWN" in src, (
        "yesterday's pulse markdown injection got removed — DRAFT "
        "can't write inversion callouts without seeing yesterday's text"
    )
    _ok("synthesizer.py still ships STANCE-INVERSION NAMING rule + "
        "yesterday's markdown to DRAFT (the carve-out's input source)")


if __name__ == "__main__":
    print("=== EDIT stance-inversion carve-out smoke ===")
    test_cull_rule_intent_preserved()
    test_mid_prompt_has_inversion_exception()
    test_carveout_quotes_concrete_examples()
    test_carveout_caps_at_one_sentence()
    test_closing_instruction_references_exception()
    test_synthesizer_still_ships_inversion_rule()
    print("\nALL EDIT STANCE-INVERSION CARVE-OUT SMOKE TESTS PASS")
