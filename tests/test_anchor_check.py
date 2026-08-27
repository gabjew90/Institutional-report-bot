"""Anchor verification — redesign sequencing step 2.

The checker's job is to make extraction fidelity measurable, which
means BOTH failure directions matter: a normalizer too weak fails
honest quotes (measuring the PDF renderer, not the extraction), and a
normalizer too strong passes paraphrases (measuring nothing). The
artifact cases below are the real shapes PyMuPDF emits from two-column
bank PDFs; the paraphrase cases are the model behaviors the check
exists to catch.
"""
import sys

from ai_analysis.anchor_check import MIN_ANCHOR_CHARS, check_anchors, normalize


# ------------------------------------------------------- normalization

def test_line_wrap_hyphen_is_joined():
    """'infla-\\ntion' is a soft wrap, one word."""
    assert normalize("core infla-\ntion pressure") == \
        normalize("core inflation pressure")


def test_real_hyphen_survives():
    """'bear-steepener' has no line break — the hyphen is content."""
    assert "bear-steepener" in normalize("a bear-steepener move")
    assert normalize("bear-steepener") != normalize("bearsteepener")


def test_ligatures_fold():
    assert normalize("oﬃce ﬁnancing conﬂict") == \
        normalize("office financing conflict")


def test_smart_quotes_and_dashes_fold():
    assert normalize("the Fed’s “dot plot” — shifted") \
        == normalize("the Fed's \"dot plot\" - shifted")


def test_nbsp_and_thin_space_fold():
    assert normalize("4.4% on the 10Y") == \
        normalize("4.4% on the 10Y")


def test_case_and_whitespace_fold():
    assert normalize("  10Y   Broke\n4.4%  ") == normalize("10y broke 4.4%")


def test_digits_are_never_altered():
    """The conservative core: number formats must NOT be equated, or a
    paraphrase that rewrites '$751B' as '$751 billion' would pass."""
    assert normalize("$751B") != normalize("$751 billion")
    assert normalize("4.4%") != normalize("4.40%")


# --------------------------------------------------------- the checker

_DOC = (
    "Goldman raised its 2026 hyperscaler capex estimate to $751B this "
    "week, up $80B in two weeks. Meanwhile the 10-year yield broke "
    "4.4% for the first time since May, and L/S net leverage sits at "
    "a 5-year low.\n\nThe FOMC voted 8-4, the widest dissent since "
    "2019, and core infla-\ntion remains above target."
)


def _pt(anchor, figure="$751B"):
    return {"figure": figure, "anchor": anchor}


def test_verbatim_anchor_matches():
    s = check_anchors([_pt("capex estimate to $751B this week")], _DOC)
    assert s["matched"] == 1 and s["missed"] == 0
    assert s["match_rate"] == 1.0


def test_anchor_across_artifacts_matches():
    """An honest quote spanning a soft line-wrap hyphen must pass —
    this is the case that kills literal substring matching."""
    s = check_anchors([_pt("core inflation remains above target",
                           figure="inflation")], _DOC)
    assert s["matched"] == 1, s


def test_paraphrase_misses():
    """Same fact, reworded — the model reconstructed from memory
    instead of copying. This is the fidelity signal."""
    s = check_anchors(
        [_pt("Goldman lifted its capex forecast to $751 billion")], _DOC)
    assert s["missed"] == 1 and s["matched"] == 0
    assert s["match_rate"] == 0.0
    assert s["misses"][0]["figure"] == "$751B"


def test_reformatted_number_misses():
    """'8 to 4' for '8-4' is a paraphrase of the figure itself."""
    s = check_anchors([_pt("the FOMC voted 8 to 4, the widest",
                           figure="8-4")], _DOC)
    assert s["missed"] == 1


def test_empty_anchor_counted_separately():
    """Pre-existing rows and model omissions are 'empty', not failures
    — the match_rate must only measure verifiable anchors."""
    s = check_anchors([_pt(""), _pt("capex estimate to $751B this week")],
                      _DOC)
    assert s["empty"] == 1 and s["matched"] == 1
    assert s["match_rate"] == 1.0


def test_short_anchor_is_unverifiable_not_matched():
    """'4.4%' appears in nearly every rates document — matching it
    verifies nothing and inflates the rate."""
    assert len("4.4%") < MIN_ANCHOR_CHARS
    s = check_anchors([_pt("4.4%", figure="4.4%")], _DOC)
    assert s["too_short"] == 1 and s["matched"] == 0
    assert s["match_rate"] is None


def test_no_points_no_rate():
    s = check_anchors([], _DOC)
    assert s["total"] == 0 and s["match_rate"] is None


def test_dataclass_input_works():
    """Live path passes KeyDataPoint dataclasses, not dicts."""
    from ai_analysis.models import KeyDataPoint
    p = KeyDataPoint(figure="$751B", metric="capex", source_bank="GS",
                     anchor="capex estimate to $751B this week")
    s = check_anchors([p], _DOC)
    assert s["matched"] == 1


def test_checker_never_raises():
    """A garbage input produces a stats dict, never an exception — the
    checker must not be able to take down an analysis."""
    s = check_anchors([{"figure": None, "anchor": None}, "not a dict"],
                      None)
    assert isinstance(s, dict)


def test_misses_sample_is_capped():
    pts = [_pt(f"this anchor number {i} does not appear anywhere",
               figure=str(i)) for i in range(15)]
    s = check_anchors(pts, _DOC)
    assert s["missed"] == 15
    assert len(s["misses"]) == 10


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
