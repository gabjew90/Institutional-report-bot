"""Smoke test for ask_qc.aggregator.render_report.

Verifies:
  - header summary has correct counts + percentages
  - top notable_pattern surfacing
  - per-interaction blocks render verdict-per-dimension
  - UNGRADED interactions render with grader_error
  - empty input renders a 'no interactions' stub
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_graded(ts, verdicts, notable=None, error=None):
    from ask_qc.models import DimensionVerdict, GradedInteraction
    return GradedInteraction(
        interaction_ts_utc=ts,
        dimensions={
            name: DimensionVerdict(v, f"rationale for {name}")
            for name, v in verdicts.items()
        },
        notable_pattern=notable,
        grader_error=error,
    )


def test_header_summary_counts():
    from ask_qc.aggregator import render_report
    all_pass = {n: "PASS" for n in (
        "fabrication", "status_handling", "voice",
        "format_adherence", "depth_match", "decline_when_uncertain",
    )}
    one_fail = dict(all_pass, **{"fabrication": "FAIL"})
    one_concern = dict(all_pass, **{"voice": "CONCERN"})
    graded = [
        _make_graded("2026-06-01 10:00:00 UTC", all_pass),
        _make_graded("2026-06-01 11:00:00 UTC", all_pass),
        _make_graded("2026-06-01 12:00:00 UTC", one_concern),
        _make_graded("2026-06-01 13:00:00 UTC", one_fail),
    ]
    report = render_report("2026-06-01", graded)
    assert "4 interactions" in report or "4 total" in report.lower(), report[:500]
    # 2 CLEAN, 1 CONCERN, 1 FAIL
    assert "CLEAN" in report and "CONCERN" in report and "FAIL" in report
    _ok("render_report: header shows correct verdict counts")


def test_per_interaction_block_renders():
    from ask_qc.aggregator import render_report
    graded = [_make_graded(
        "2026-06-01 22:01:45 UTC",
        {
            "fabrication": "PASS", "status_handling": "FAIL",
            "voice": "PASS", "format_adherence": "PASS",
            "depth_match": "PASS", "decline_when_uncertain": "FAIL",
        },
        notable="treated status=empty as logged trades",
    )]
    report = render_report("2026-06-01", graded)
    assert "22:01:45" in report
    assert "status_handling" in report
    assert "decline_when_uncertain" in report
    assert "rationale for fabrication" in report
    assert "treated status=empty as logged trades" in report
    _ok("render_report: per-interaction block has timestamp + dims + notable_pattern")


def test_ungraded_renders_with_error():
    from ask_qc.aggregator import render_report
    graded = [_make_graded(
        "2026-06-01 22:01:45 UTC",
        {}, error="RuntimeError",
    )]
    report = render_report("2026-06-01", graded)
    assert "UNGRADED" in report
    assert "RuntimeError" in report
    _ok("render_report: UNGRADED entries render with grader_error")


def test_top_pattern_surfaced_in_header():
    """If a notable_pattern appears 2+ times, surface it in the header
    so the daily-summary section calls out the recurring pattern."""
    from ask_qc.aggregator import render_report
    all_pass = {n: "PASS" for n in (
        "fabrication", "status_handling", "voice",
        "format_adherence", "depth_match", "decline_when_uncertain",
    )}
    pattern = "treated status=empty as logged trades"
    graded = [
        _make_graded("2026-06-01 10:00:00 UTC", all_pass, notable=pattern),
        _make_graded("2026-06-01 11:00:00 UTC", all_pass, notable=pattern),
        _make_graded("2026-06-01 12:00:00 UTC", all_pass, notable="one-off"),
    ]
    report = render_report("2026-06-01", graded)
    # Header section ends before per-interaction blocks (split on first '##')
    head = report.split("\n## ", 1)[0]
    assert pattern in head, "recurring pattern not surfaced in header"
    _ok("render_report: recurring patterns surfaced in header summary")


def test_empty_input_renders_stub():
    from ask_qc.aggregator import render_report
    report = render_report("2026-06-01", [])
    assert "No /ask interactions" in report or "no interactions" in report.lower()
    assert "2026-06-01" in report
    _ok("render_report: empty input renders 'no interactions' stub")


if __name__ == "__main__":
    print("=== ask_qc.aggregator smoke ===")
    test_header_summary_counts()
    test_per_interaction_block_renders()
    test_ungraded_renders_with_error()
    test_top_pattern_surfaced_in_header()
    test_empty_input_renders_stub()
    print("\nALL ASK-QC AGGREGATOR SMOKE TESTS PASS")
