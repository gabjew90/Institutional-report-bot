"""Smoke test: prev_context directive includes the stance-inversion
naming rule (rule #5) + injects yesterday's pulse markdown so DRAFT
can do the comparison.

Background: 2026-06-04 QC review observed two day-over-day inversions
in one pulse (ECB stance: priced-hike → priced-cut; payrolls bias:
below-consensus → above-consensus) — neither acknowledged in the
prose. Daily reader's natural reaction: 'which one is the consensus
actually pricing?' Trust suffers from the unnamed swing, not from
the corpus genuinely changing.

Fix: rule #5 in the existing prev_context directive in
report/synthesizer.py requires DRAFT to walk yesterday's markdown,
compare directional verbs (hike/cut, above/below, long/short,
rip/fade) per recurring theme, and emit a one-line 'this reverses
yesterday's bias on X' / 'day-over-day setup flip' beat when the
read flipped overnight.

Plus prev_context now includes the first 4k chars of yesterday's
markdown — without that, the directive's 'walk yesterday's pulse
markdown' instruction has nothing to walk.

This smoke locks both pieces in place.
"""

import inspect
import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_directive_has_stance_inversion_rule():
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    assert "STANCE-INVERSION NAMING" in src, (
        "rule #5 'STANCE-INVERSION NAMING' missing from prev_context directive"
    )
    _ok("prev_context directive contains the STANCE-INVERSION NAMING rule")


def test_directive_names_concrete_inversion_examples():
    """The rule must include named concrete examples (ECB hike→cut,
    payrolls below→above) so DRAFT recognizes the pattern shape, not
    just abstract guidance."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    # ECB hike-cut example
    assert "hike" in src.lower() and "cut" in src.lower()
    assert "ECB" in src or "ecb" in src
    # Payrolls below-above example
    assert "above consensus" in src.lower() or "below consensus" in src.lower()
    # Specific date anchors from the 2026-06-03 / 2026-06-04 observation
    assert "2026-06-03" in src and "2026-06-04" in src
    _ok("rule names concrete ECB + payrolls inversion examples + date anchors")


def test_directive_lists_acceptable_inversion_phrasings():
    """Rule must list >=2 acceptable phrasings ('this reverses yesterday's
    bias on X', 'day-over-day setup flip', etc.) so DRAFT has concrete
    shape to copy."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    inversion_phrases = [
        "reverses yesterday",
        "day-over-day",
        "flipped overnight",
        "setup flip",
    ]
    matched = sum(1 for p in inversion_phrases if p in src)
    assert matched >= 2, (
        f"need >=2 acceptable inversion phrasings, got {matched}: "
        f"checked {inversion_phrases}"
    )
    _ok(f"rule lists {matched} acceptable inversion phrasings")


def test_prev_context_includes_yesterday_markdown():
    """prev_context must inject yesterday's pulse markdown (truncated)
    so DRAFT can actually do the comparison the directive asks for —
    without the markdown, 'walk yesterday's pulse' has nothing to walk."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    # Anchor: the new f-string section that prepends the markdown
    assert "YESTERDAY'S PULSE MARKDOWN" in src, (
        "prev_context should inject yesterday's pulse markdown as a "
        "named section so DRAFT can read it"
    )
    # Anchor: the truncation to ~4k chars (preserves token budget;
    # the recurring-theme directional-verb comparison only needs the
    # leading INSIGHTS sections)
    assert "[:4000]" in src or "[:5000]" in src or "[:3500]" in src, (
        "markdown should be truncated to ~4k chars to preserve "
        "DRAFT's token budget"
    )
    _ok("prev_context injects yesterday's pulse markdown (truncated)")


def test_directive_specifies_what_to_compare():
    """Rule must explicitly tell DRAFT WHAT to compare per theme —
    'directional verb' (hike/cut, above/below, long/short, rip/fade) —
    so the model has a concrete extraction task rather than the
    abstract 'compare stances'."""
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    verb_pairs = [
        ("hike", "cut"),
        ("above", "below"),
        ("long", "short"),
    ]
    matched_pairs = [(a, b) for a, b in verb_pairs if a in src and b in src]
    assert len(matched_pairs) >= 2, (
        f"need >=2 directional-verb pairs in the directive, got: "
        f"{matched_pairs}"
    )
    _ok(f"rule specifies {len(matched_pairs)} directional-verb pairs to compare")


if __name__ == "__main__":
    print("=== stance-inversion directive smoke ===")
    test_directive_has_stance_inversion_rule()
    test_directive_names_concrete_inversion_examples()
    test_directive_lists_acceptable_inversion_phrasings()
    test_prev_context_includes_yesterday_markdown()
    test_directive_specifies_what_to_compare()
    print("\nALL STANCE-INVERSION DIRECTIVE SMOKE TESTS PASS")
