"""Smoke: HC-board rating truncation (2026-07-09).

The 07-09 TRADE BOARD shipped "UBS $AVGO — Improvin" and "UBS $ASML —
Most": _norm_rating's "word-boundary clip to 8" was a plain slice for
single words (shipping the exact mid-word stub the comment claimed to
prevent), and chopped multi-word ratings to their first word ("Most
Preferred" -> "Most", which reads as gibberish). Fixes:
  - UBS house scale + RBC Hold-equivalents added to _RATING_NORM
  - single words ship whole (extraction caps the field at 12 chars)
  - multi-word fallback clips at a word boundary WITH an ellipsis so a
    dropped word is visible instead of silent
  - the monospace board's own [:8] column widened to 10
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report.pulse_sections import _norm_rating  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_the_shipped_stubs_are_fixed():
    # the two 2026-07-09 board entries
    assert _norm_rating("Improving", "") == "Improving", \
        f"single word must ship whole: {_norm_rating('Improving', '')!r}"
    assert _norm_rating("Most Preferred", "") == "Most Pref", \
        f"UBS scale must map: {_norm_rating('Most Preferred', '')!r}"
    assert _norm_rating("Least Preferred", "") == "Least Pref"
    _ok("shipped stubs fixed: Improving whole, Most Preferred -> Most Pref")


def test_known_map_still_works():
    assert _norm_rating("Overweight", "") == "OW"
    assert _norm_rating("outperform", "") == "OP"
    assert _norm_rating("Sector Perform", "") == "Hold"
    assert _norm_rating("", "initiate") == "initiate"  # action fallback
    _ok("known ratings map; action fallback intact")


def test_multiword_clip_is_visible_not_silent():
    # an unmapped long multi-word rating clips at a word boundary and
    # SHOWS the clip
    got = _norm_rating("Speculative Outperform Overdrive", "")
    assert got.endswith("…"), f"clip must be visible: {got!r}"
    assert not got[:-1].endswith(" "), f"no trailing space: {got!r}"
    # a short multi-word passes through
    assert _norm_rating("Top Pick", "") == "Top Pick"
    _ok("multi-word: short passes, long clips visibly with ellipsis")


def test_live_renderer_does_not_reclip_ratings():
    """render_desk_signal_board (the monospace board this test used to
    pin) was removed 2026-08-04 as dead code — the live HC renderer is
    _render_hc_subsection, which uses markdown bullets with no column
    widths. Guard the surviving rule: the live renderer must not slice
    _norm_rating's output back down (the 8-slice is what shipped
    'Improvin')."""
    import report.pulse_sections as ps
    src = inspect.getsource(ps._render_hc_subsection)
    seg = src.split("_norm_rating", 1)[1][:200]
    assert "[:8]" not in seg and "[:9]" not in seg, \
        "live HC renderer re-slices the rating — mid-word stubs return"
    _ok("live HC renderer ships _norm_rating output unclipped")


if __name__ == "__main__":
    print("=== HC-board rating truncation smoke ===")
    test_the_shipped_stubs_are_fixed()
    test_known_map_still_works()
    test_multiword_clip_is_visible_not_silent()
    test_live_renderer_does_not_reclip_ratings()
    print("\nALL RATING-TRUNCATION SMOKE TESTS PASS")
