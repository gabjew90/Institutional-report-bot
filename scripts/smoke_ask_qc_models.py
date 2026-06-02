"""Smoke test for ask_qc.models dataclasses.

Verifies the three dataclasses (AskInteraction, DimensionVerdict,
GradedInteraction) construct cleanly with the expected fields and
that GradedInteraction's overall_verdict computation is correct."""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_ask_interaction_construct():
    from ask_qc.models import AskInteraction
    a = AskInteraction(
        ts_utc="2026-06-01 22:01:45 UTC",
        asker_label="BK (`bankerkyle`)",
        asker_username="bankerkyle",
        channel="#stonks-yapping",
        question="who's the most racist?",
        answer="SV takes the crown.",
        prompt_block=None,
    )
    assert a.ts_utc.startswith("2026-")
    assert a.prompt_block is None
    _ok("AskInteraction constructs with None prompt_block (legacy log)")


def test_dimension_verdict_construct():
    from ask_qc.models import DimensionVerdict
    v = DimensionVerdict(verdict="PASS", rationale="grounded in tool output")
    assert v.verdict == "PASS"
    _ok("DimensionVerdict constructs")


def test_graded_interaction_rollup_clean():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="2026-06-01 22:01:45 UTC",
        dimensions={
            "fabrication": DimensionVerdict("PASS", "ok"),
            "status_handling": DimensionVerdict("N/A", "legacy"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("N/A", "all ok"),
        },
        notable_pattern=None,
    )
    assert g.overall_verdict == "CLEAN", g.overall_verdict
    _ok("GradedInteraction rollup CLEAN when all PASS/N/A")


def test_graded_interaction_rollup_fail_beats_concern():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={
            "fabrication": DimensionVerdict("FAIL", "fabricated price"),
            "status_handling": DimensionVerdict("CONCERN", "under-hedged"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("PASS", "ok"),
        },
        notable_pattern="fabricated $TSLA price",
    )
    assert g.overall_verdict == "FAIL", g.overall_verdict
    _ok("GradedInteraction rollup FAIL when any FAIL present")


def test_graded_interaction_rollup_concern():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={
            "fabrication": DimensionVerdict("PASS", "ok"),
            "status_handling": DimensionVerdict("CONCERN", "under-hedged"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("PASS", "ok"),
        },
        notable_pattern=None,
    )
    assert g.overall_verdict == "CONCERN", g.overall_verdict
    _ok("GradedInteraction rollup CONCERN when CONCERN but no FAIL")


def test_graded_interaction_ungraded_short_circuit():
    """If `grader_error` is set, the rollup short-circuits to UNGRADED
    regardless of dimension verdicts (which should be empty)."""
    from ask_qc.models import GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={},
        notable_pattern=None,
        grader_error="JSONDecodeError",
    )
    assert g.overall_verdict == "UNGRADED", g.overall_verdict
    _ok("GradedInteraction rollup UNGRADED when grader_error set")


if __name__ == "__main__":
    print("=== ask_qc.models smoke ===")
    test_ask_interaction_construct()
    test_dimension_verdict_construct()
    test_graded_interaction_rollup_clean()
    test_graded_interaction_rollup_fail_beats_concern()
    test_graded_interaction_rollup_concern()
    test_graded_interaction_ungraded_short_circuit()
    print("\nALL ASK-QC MODELS SMOKE TESTS PASS")
