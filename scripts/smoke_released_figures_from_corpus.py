"""Smoke: released-figure QC uses the corpus's own figure_status labels.

2026-08-04 review, G3 + the actuals gap: Finnhub's economic calendar
(the only actuals source) 403'd behind a paid tier in June; the
ForexFactory fallback hardcodes actual=None. CHECK 9's ground truth
came ONLY from calendar ACTUAL rows, so with no actuals in the calendar
the estimate-as-print check simply never ran — while the extractor was
labeling released figures (`MacroIndicator.status`,
`KeyDataPoint.figure_status`) in the very corpus the pulse synthesizes,
and those labels reached nothing. Four of seven pulses in the review
week told readers to "watch the reaction rather than the headline" for
prints that were public at publish time, and 08-04 claimed the ISM
actual "has not reached our feed" while Deutsche Bank's note in its own
corpus (analyzed 7h earlier) carried it: 55.6 vs 53.9 expected.

The validator now harvests released/forecast figures from
ctx['analyses_json'] in addition to calendar rows.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import pulse_draft_validate as pdv  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _ctx():
    """Calendar has NO actuals (the production condition since Finnhub
    403'd); the corpus carries the released print + the consensus."""
    return {
        "economic_calendar": (
            "STILL UPCOMING\n"
            "08-05 10:00 ET | [US] ISM Services PMI | est=54.5 prev=54.0\n"
        ),
        "analyses_json": json.dumps([
            {
                "source": "Deutsche Bank",
                "title": "Early Morning Reid",
                "macro": [
                    {"indicator": "ISM Manufacturing", "reading": "55.6%",
                     "interpretation": "beat", "status": "released",
                     "period": "2026-07"},
                ],
                "data_points": [
                    {"figure": "53.9%", "metric": "ISM Manufacturing consensus",
                     "source_bank": "Deutsche Bank", "context": "expected",
                     "figure_status": "forecast"},
                ],
            },
        ]),
    }


def test_corpus_released_rows_harvested():
    rows = pdv._released_event_rows(_ctx())
    assert "ISM" in rows, (
        f"corpus released macro rows must reach the ground-truth map "
        f"even when the calendar has no ACTUAL rows: {rows.keys()}"
    )
    assert any(abs(v - 55.6) < 0.01 for v in rows["ISM"]["ok"]), rows["ISM"]
    assert any(abs(v - 53.9) < 0.01 for v in rows["ISM"]["est"]), rows["ISM"]
    _ok("corpus figure_status labels feed the released-figure map")


def test_estimate_as_print_flagged_from_corpus_truth():
    md = "ISM manufacturing printed 53.9% this morning, a beat.\n"
    v = pdv._released_figure_violations(md, _ctx())
    assert v, (
        "quoting the consensus as the print must be flagged when the "
        "corpus carries the released actual"
    )
    _ok("estimate-as-print caught with corpus-only ground truth")


def test_true_actual_passes():
    md = "ISM manufacturing printed 55.6% this morning, a beat.\n"
    v = pdv._released_figure_violations(md, _ctx())
    assert not v, f"the real print must pass: {v}"
    _ok("the real released print passes")


def test_press_time_note_prefers_corpus_actual():
    """The routine's straddle note must send DRAFT to the corpus for
    the actual BEFORE falling back to reaction-framing, and must never
    narrate pipeline plumbing to readers."""
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = open(os.path.join(repo, "docs", "superpowers", "routines",
                            "synthesis-routine.md"), encoding="utf-8").read()
    assert "status\": \"released" in doc or "status='released'" in doc or \
        "figure_status" in doc, (
        "the press-time note never points DRAFT at the corpus's released "
        "figures — the ISM actual sat in a DB note for 7 hours while the "
        "pulse said it 'has not reached our feed'"
    )
    _ok("press-time note routes DRAFT to in-corpus actuals first")


def test_straddle_uses_zoneinfo_not_fixed_edt():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    doc = open(os.path.join(repo, "docs", "superpowers", "routines",
                            "synthesis-routine.md"), encoding="utf-8").read()
    assert "timedelta(hours=4)" not in doc, (
        "press-time straddle still hardcodes EDT — off by one hour "
        "November through March, misclassifying every 8:30 ET print"
    )
    assert "America/New_York" in doc, (
        "straddle conversion must use zoneinfo America/New_York"
    )
    _ok("straddle check converts ET via zoneinfo, not a fixed +4h")


if __name__ == "__main__":
    print("=== released-figures-from-corpus smoke ===")
    test_corpus_released_rows_harvested()
    test_estimate_as_print_flagged_from_corpus_truth()
    test_true_actual_passes()
    test_press_time_note_prefers_corpus_actual()
    test_straddle_uses_zoneinfo_not_fixed_edt()
    print("\nALL RELEASED-FIGURES-FROM-CORPUS SMOKE TESTS PASS")
