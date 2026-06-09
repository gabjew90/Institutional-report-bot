"""Smoke test: STITCH preserves $SPX/$NDX/$RUT when paired with an
index-scale level, swaps when paired with ETF-scale prices.

Background: 2026-06-08 pulse slot #1 said "$SPY futures' 7,375 zone is
the must-hold for the systematic complex" — the 7,375 level is the
$SPX number (S&P 500 trades around 7,375) but the ticker was wrongly
swapped to $SPY (which trades around $738). Same bug hit "$QQQ 29,000
Fibonacci level" — 29,000 is the $NDX level, not the $QQQ price.

Root cause: STITCH's TICKER_NORMALIZE swap was unconditional — every
$SPX became $SPY regardless of whether the level next to it was index-
scale (4+ digits) or ETF-scale (<1000). A reader scanning fast reads
"$SPY 7,375" as a typo.

Fix: a same-sentence lookahead detects 4+ digit numbers near the
ticker. When found, preserve the original index ticker; when absent,
swap to the ETF as before.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_spx_with_index_level_preserved():
    """The exact 2026-06-08 failure case: $SPX paired with 7,375 zone
    must stay as $SPX, not become $SPY."""
    from scripts.pulse_stitch import stitch
    md = "$SPX futures' 7,375 zone is the must-hold for the systematic complex."
    new_md, fixes, _ = stitch(md)
    assert "$SPX" in new_md, (
        f"$SPX paired with index-scale level 7,375 must be preserved, "
        f"got: {new_md!r}"
    )
    assert "$SPY 7,375" not in new_md, (
        f"$SPY 7,375 is the wrong-scale failure mode — must not appear, "
        f"got: {new_md!r}"
    )
    assert any("preserved $SPX" in f for f in fixes), (
        f"fixes log must record the preservation for audit, got: {fixes}"
    )
    _ok("$SPX 7,375 → preserved (not swapped to $SPY 7,375)")


def test_ndx_with_index_level_preserved():
    """The 2026-06-08 sibling failure: $NDX 29,000 must stay as $NDX."""
    from scripts.pulse_stitch import stitch
    md = "$NDX 29,000 is the Fibonacci level the systematic complex watches."
    new_md, fixes, _ = stitch(md)
    assert "$NDX" in new_md
    assert "$QQQ 29,000" not in new_md, (
        f"$QQQ 29,000 is wrong-scale (QQQ trades around $700), got: "
        f"{new_md!r}"
    )
    _ok("$NDX 29,000 → preserved (not swapped to $QQQ 29,000)")


def test_spx_with_etf_scale_price_still_swaps():
    """$SPX paired with an ETF-scale price (no 4+ digit number nearby)
    must still swap to $SPY — the carve-out only fires on index-scale."""
    from scripts.pulse_stitch import stitch
    md = "$SPX is currently -2.58%, the worst single-day reset of the cycle."
    new_md, _, _ = stitch(md)
    assert "$SPY" in new_md and "$SPX" not in new_md, (
        f"$SPX without a nearby index-scale level must still swap to "
        f"$SPY (the carve-out only fires when index-level is present), "
        f"got: {new_md!r}"
    )
    _ok("$SPX -2.58% → swapped to $SPY (no index-scale level present)")


def test_rut_with_index_level_preserved():
    """$RUT (Russell 2000) trades around 2,000-2,500 — 4-digit index
    levels still trigger preservation."""
    from scripts.pulse_stitch import stitch
    md = "$RUT 2,150 is the level small-caps need to defend."
    new_md, _, _ = stitch(md)
    assert "$RUT" in new_md
    assert "$IWM 2,150" not in new_md, (
        "$IWM 2,150 is wrong-scale (IWM trades around $200)"
    )
    _ok("$RUT 2,150 → preserved (not swapped to $IWM 2,150)")


def test_mixed_doc_preserves_and_swaps_correctly():
    """A doc with BOTH index-level references AND bare ticker references
    must preserve the levels and swap the bares — both rules must
    co-exist in one pass."""
    from scripts.pulse_stitch import stitch
    md = """
Heading into Monday's open, $SPX closed -2.58% with $NDX leading the
damage at -4.80%. Below the $SPX 7,375 zone the next gravity is 7,200
(the 50-day). $NDX 29,000 is the Fibonacci level the systematic
complex watches. $RUT also leaked.
""".strip()
    new_md, fixes, _ = stitch(md)
    # Preserve index tickers paired with levels
    assert "$SPX 7,375" in new_md, f"got: {new_md!r}"
    assert "$NDX 29,000" in new_md, f"got: {new_md!r}"
    # Bare tickers (no nearby 4+ digit number) should swap
    assert "$SPY closed" in new_md or "$SPY" in new_md, (
        f"bare $SPX (in '$SPX closed -2.58%') should swap to $SPY, "
        f"got: {new_md!r}"
    )
    assert "$IWM also leaked" in new_md, (
        f"bare $RUT should swap to $IWM, got: {new_md!r}"
    )
    # Fix log should record both swaps and preserves
    swap_lines = [f for f in fixes if "ticker-normalize:" in f]
    preserve_lines = [f for f in fixes if "preserved" in f]
    assert swap_lines, f"expected swap log entries, got: {fixes}"
    assert preserve_lines, f"expected preserve log entries, got: {fixes}"
    _ok("mixed doc: bare tickers swap + index-level pairings preserved, "
        "both logged separately")


def test_sentence_boundary_limits_lookahead():
    """The lookahead is bounded to the same sentence to avoid false
    preserves when a 4+ digit number appears later in unrelated context.

    Edge case: '$SPX is up today. The 5-year Treasury yield is at
    4,200 bps.' Without sentence bounding, the 4,200 would falsely
    preserve $SPX even though it's about a different instrument in a
    different sentence.
    """
    from scripts.pulse_stitch import stitch
    md = "$SPX is up today. The benchmark 5-year Treasury yield sits at 4,200 bps."
    new_md, _, _ = stitch(md)
    # The sentence containing $SPX has NO index-scale level — should swap
    assert "$SPY is up today" in new_md, (
        f"sentence-bounded lookahead: $SPX in 'is up today' should swap "
        f"despite 4,200 appearing in the next sentence, got: {new_md!r}"
    )
    _ok("sentence-bounded lookahead: $SPX swaps despite 4-digit number "
        "in next sentence (no false preserve)")


if __name__ == "__main__":
    print("=== STITCH index-level preservation smoke ===")
    test_spx_with_index_level_preserved()
    test_ndx_with_index_level_preserved()
    test_spx_with_etf_scale_price_still_swaps()
    test_rut_with_index_level_preserved()
    test_mixed_doc_preserves_and_swaps_correctly()
    test_sentence_boundary_limits_lookahead()
    print("\nALL STITCH INDEX-LEVEL PRESERVATION SMOKE TESTS PASS")
