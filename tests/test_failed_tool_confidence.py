"""Class 10: confidence despite a failed tool (QC queue finding 2).

The class is STATUS-gated: with no tool_status in ctx it can never
fire, which makes it inert across the whole recorded corpus (which
carries tool names only). Its false-positive surface therefore lives
in these tests and the live fixtures, not the sweep — the grounded
twin and the hedged answer are the load-bearing carve-outs.
"""
import sys

from scripts.ask_response_validate import (check_failed_tool_confidence,
                                           resolve_violations)

BAD = ("-> **August 27, 2026** after the market close (AMC) for the Q2 "
       "fiscal 2026 release\n"
       "-> **Consensus projections:** EPS expected around $0.48-$0.52 "
       "on $3.69B in revenue")
NO_DATA = {"lookup_earnings_date": "no_data"}


def test_incident_shape_flags():
    vs = check_failed_tool_confidence(BAD, tool_status=NO_DATA,
                                      grounded=False)
    assert vs and vs[0].rule == "failed-tool-confidence"


def test_grounded_twin_is_clean():
    """The same day had a near-identical GPS turn that grounded via a
    real search and was CORRECT to state the date. Keying on absent
    grounding, not tool failure alone, is the whole design."""
    assert check_failed_tool_confidence(BAD, tool_status=NO_DATA,
                                        grounded=True) == []


def test_successful_tool_is_clean():
    assert check_failed_tool_confidence(
        BAD, tool_status={"lookup_earnings_date": "ok"},
        grounded=False) == []


def test_no_status_ctx_is_inert():
    """The whole recorded corpus: names only, no statuses. The class
    must be structurally unable to fire there."""
    assert check_failed_tool_confidence(BAD, grounded=False) == []
    assert check_failed_tool_confidence(BAD, tool_status={},
                                        grounded=False) == []


def test_hedged_answer_is_clean():
    hedged = ("-> no date on file for GPS. typically reports late "
              "August, can not confirm this quarter without the "
              "announcement.")
    assert check_failed_tool_confidence(hedged, tool_status=NO_DATA,
                                        grounded=False) == []


def test_non_earnings_date_is_clean():
    """A macro date in the answer while the earnings tool failed is a
    different subject, not the violation."""
    macro = "-> CPI prints September 11 at 8:30 AM ET, next big catalyst"
    assert check_failed_tool_confidence(macro, tool_status=NO_DATA,
                                        grounded=False) == []


def test_error_and_empty_statuses_also_gate():
    for st in ("error", "empty", "not_found"):
        vs = check_failed_tool_confidence(
            BAD, tool_status={"lookup_earnings_date": st}, grounded=False)
        assert vs, f"status {st} should gate the class"


def test_retry_ctx_lets_a_grounded_retry_regenerate():
    """The ladder's retry runs WITH search. A retry that grounded
    itself must be judged grounded, or the ladder strips or refuses a
    correct regeneration."""
    final, outcome = resolve_violations(
        BAD, BAD, ["lookup_earnings_date"],
        retry_ctx={"grounded": True},
        tool_status=NO_DATA, grounded=False, question="", fetched=None)
    assert outcome == "regenerated"
    assert final == BAD


def test_without_retry_ctx_the_same_retry_is_rejected():
    """The mirror case: identical retry text, no grounding override —
    the ladder must NOT accept it."""
    final, outcome = resolve_violations(
        BAD, BAD, ["lookup_earnings_date"],
        tool_status=NO_DATA, grounded=False, question="", fetched=None)
    assert outcome != "regenerated"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
