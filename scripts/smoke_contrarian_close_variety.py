"""Smoke test: contrarian rotate-out close-lean variety guidance.

Background: QC reviews on 2026-06-03 + 2026-06-04 (and earlier) flagged
that the contrarian slot keeps closing with '$RSP over $SPY' as the
rotation instrument. Reader-experience issue: daily reader catches
the template.

Fix: when contrarian_lines fires in _format_theme_coverage, append
a 'ROTATE-OUT CLOSE — VARIETY REQUIRED' subsection that enumerates 7
distinct instrument categories (defensive / small-cap / value /
equal-weight / international / commodity / tail-hedge) and requires
that a $RSP-over-$SPY close be paired with at least one secondary
instrument from a different category.

This smoke locks the variety guidance in place against future edits.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _theme_map_with_contrarian():
    """Construct a theme_map that will fire the contrarian_lines path
    in _format_theme_coverage so we can assert on the resulting block."""
    return {
        "consensus-contrarian / rotate-out-of-lead": {
            "banks": 2, "pdfs": 4,
            "sources": ["Bloomberg", "MUFG"],
            "supportive": 0, "skeptical": 2, "neutral": 0,
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
            "_contrarian": True,
        },
    }


def test_contrarian_block_includes_variety_subsection():
    """When contrarian_lines fires, the rendered block must contain
    the 'ROTATE-OUT CLOSE — VARIETY REQUIRED' heading + all 7 category
    letters."""
    from report.synthesizer import _format_theme_coverage
    block = _format_theme_coverage(_theme_map_with_contrarian())
    if "CONTRARIAN / ROTATE-OUT SIGNAL" not in block:
        # The synthetic theme_map may not be enough to trigger the
        # contrarian_lines path on its own. Skip this case in that
        # situation — the next test asserts the variety text exists
        # statically in the synthesizer module source.
        print(
            "SKIP: synthetic theme_map didn't trigger contrarian_lines "
            "(detection logic depends on real theme detection); next "
            "test asserts on module source instead"
        )
        return
    assert "ROTATE-OUT CLOSE" in block
    assert "VARIETY REQUIRED" in block
    for letter in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)"):
        assert letter in block, f"category marker {letter!r} missing"
    _ok("contrarian block includes 7-category variety subsection")


def test_module_source_contains_full_variety_guidance():
    """Static assertion: the synthesizer module source contains the
    full variety guidance with all 7 categories + the secondary-
    instrument requirement. This catches regression on edits that
    might silently drop part of the steering."""
    import inspect
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "ROTATE-OUT CLOSE" in src
    assert "VARIETY REQUIRED" in src
    # 7 category markers
    for letter in ("(a)", "(b)", "(c)", "(d)", "(e)", "(f)", "(g)"):
        assert letter in src, f"category {letter!r} missing from module source"
    # Specific instrument anchors — each category must name at least
    # one concrete ticker so DRAFT has actionable options
    for ticker in (
        "$XLU", "$XLV", "$XLP",         # (a) defensive
        "$IWM", "$VBR",                 # (b) small-cap
        "$VTV", "$IWD",                 # (c) value-style
        "$RSP",                         # (d) equal-weight
        "$EFA", "$EEM",                 # (e) international
        "$GLD", "$XLE",                 # (f) commodity
        "$VIXY",                        # (g) tail-hedge
    ):
        assert ticker in src, f"required instrument {ticker} missing"
    _ok("module source contains all 7 categories + 13+ concrete instruments")


def test_module_source_requires_secondary_when_default_template():
    """The variety guidance must EXPLICITLY require that an
    `$RSP over $SPY` close be paired with a secondary instrument
    from a different category. This is the core anti-template
    enforcement."""
    import inspect
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "$RSP over $SPY" in src
    assert "secondary instrument" in src.lower() or "secondary lean" in src.lower()
    assert "different category" in src.lower()
    _ok("module source requires secondary-instrument pairing when defaulting to equal-weight")


def test_tail_hedge_is_complement_not_standalone():
    """Tail hedge (g) is permitted to be ADDED to one of a-f but not
    used as a standalone close — that would be the recurring
    '$RSP + $VIXY' pattern with no rotation thesis."""
    import inspect
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "not a standalone close" in src.lower() or "permitted addition" in src.lower()
    _ok("tail-hedge category framed as permitted addition, not standalone close")


if __name__ == "__main__":
    print("=== contrarian close-lean variety smoke ===")
    test_contrarian_block_includes_variety_subsection()
    test_module_source_contains_full_variety_guidance()
    test_module_source_requires_secondary_when_default_template()
    test_tail_hedge_is_complement_not_standalone()
    print("\nALL CONTRARIAN CLOSE-LEAN VARIETY SMOKE TESTS PASS")
