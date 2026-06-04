"""Smoke test: synthesis-routine.md STEP 5.7.3 contains the
deterministic post-hoc skip-enforcement revert block.

Background: STEP 5.7.1 emits SCRUB_DECISION={dispatch|skip} based on
hard lint issue count. The 'skip' branch instructs the routine runner
not to dispatch SCRUB. Three consecutive QC reviews (2026-06-02 /
2026-06-03 / 2026-06-04) flagged that the runner ignored the gate
and dispatched SCRUB on 0-lint input anyway, producing cosmetic
delta and recurring QC complaints.

Fix: STEP 5.7.3 starts with a bash check that reverts /tmp/final.md
to /tmp/pre_scrub_final.md when the decision was 'skip' but the file
changed. This makes the gate enforceable regardless of runner
behavior.

This smoke locks the revert block in place against future edits.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _routine_text():
    return open(
        "docs/superpowers/routines/synthesis-routine.md",
        encoding="utf-8",
    ).read()


def test_step_5_7_1_emits_scrub_decision_token():
    """The decision file path that 5.7.3 reverts on must exist as a
    real artifact written by 5.7.1. If 5.7.1 stops writing it, 5.7.3's
    revert is a no-op."""
    text = _routine_text()
    assert "scrub_decision.txt" in text, (
        "STEP 5.7.1 should write /tmp/scrub_decision.txt for 5.7.3 to read"
    )
    assert "SCRUB_DECISION:" in text, (
        "STEP 5.7.1 should emit the SCRUB_DECISION token literally"
    )
    _ok("STEP 5.7.1 writes /tmp/scrub_decision.txt + emits SCRUB_DECISION token")


def test_step_5_7_3_has_revert_block():
    text = _routine_text()
    # Anchor the revert block to ensure it exists
    assert "Post-hoc skip enforcement" in text, (
        "STEP 5.7.3 should have the 'Post-hoc skip enforcement' heading"
    )
    # The bash check key shape
    assert 'cat /tmp/scrub_decision.txt' in text, (
        "revert must read the SCRUB_DECISION file deterministically"
    )
    assert "skip" in text and "pre_scrub_final.md" in text, (
        "revert must cp pre_scrub_final.md back to final.md on skip"
    )
    _ok("STEP 5.7.3 contains the post-hoc skip-enforcement revert block")


def test_revert_only_on_skip_not_dispatch():
    """The revert must NOT fire when SCRUB_DECISION=dispatch (that would
    undo legitimate SCRUB work). Anchor the conditional check."""
    text = _routine_text()
    # Find the revert block and check the conditional
    idx = text.find("Post-hoc skip enforcement")
    assert idx != -1
    # Look for the bash conditional 'if [[ ... skip ]]'
    window = text[idx:idx + 1500]
    assert '== "skip"' in window or "= \"skip\"" in window, (
        "revert conditional must check for SCRUB_DECISION=skip "
        "(not unconditional revert)"
    )
    # And require pre_scrub_final.md to exist before reverting
    assert "-f /tmp/pre_scrub_final.md" in window, (
        "revert should require pre_scrub_final.md to exist (avoids "
        "no-op on the correctly-skipped path)"
    )
    _ok("revert fires only when SCRUB_DECISION=skip AND pre-SCRUB file exists")


def test_revert_uses_cmp_to_detect_change():
    """The revert should use `cmp -s` (or equivalent) to detect that
    SCRUB actually modified /tmp/final.md before doing the cp. If we
    cp unconditionally we'd lose forensic evidence of what SCRUB did."""
    text = _routine_text()
    idx = text.find("Post-hoc skip enforcement")
    window = text[idx:idx + 1500]
    assert "cmp -s" in window or "cmp " in window, (
        "revert should use cmp to detect SCRUB modified the file before "
        "reverting (otherwise we silently revert even on correctly-skipped paths)"
    )
    _ok("revert uses cmp to detect actual SCRUB modification before reverting")


if __name__ == "__main__":
    print("=== SCRUB post-hoc skip-enforcement revert smoke ===")
    test_step_5_7_1_emits_scrub_decision_token()
    test_step_5_7_3_has_revert_block()
    test_revert_only_on_skip_not_dispatch()
    test_revert_uses_cmp_to_detect_change()
    print("\nALL SCRUB POST-HOC REVERT SMOKE TESTS PASS")
