"""Smoke test: structural TRADE BOARD leans (2026-06-23).

The board used to scrape leans out of prose with a regex, which missed
natural phrasings ("Long the power names, $VST and $CEG", "owning $BNO
outright", "rotation is $RSP over $SPY", "accumulate $GLD") — the
2026-06-23 pulse showed only 1 of ~7 actual calls. Fix: DRAFT emits a
hidden `## _LEANS` block (the structural source); the bridge reads it,
builds the board, and strips it before the pulse reaches Discord. Prose
scraping stays only as a fallback when the block is absent.

Covers: parse_lean_block, strip_lean_block, _build_lean_display, the
routine-strip keep-exemption, the QC leak-check exemption, the DRAFT
emission instruction, and the bridge wiring.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_PULSE_WITH_LEANS = """# Gravity finds the casino

## 1. RECAP
Stuff.

## 2. INSIGHTS & ALPHA

### The lead
prose. Long the power and infrastructure names, $VST and $CEG.

### A brief
prose. accumulate $GLD on dips.

## 3. WHAT TO WATCH
- catalysts

## _LEANS (internal — TRADE BOARD source, stripped before publish)
- long | $VST, $CEG, $XLU | power/infra over chasing $SMH
- long | $TLT | 2-year repricing overdone, into PCE
- short | $SMH puts | cheap insurance on a stretched book
- long | $GLD | accumulate dips below $4,000
"""


def test_parse_lean_block_all_leans():
    from report.pulse_sections import parse_lean_block
    leans = parse_lean_block(_PULSE_WITH_LEANS)
    insts = [l["instrument"] for l in leans]
    assert insts == ["VST", "TLT", "SMH", "GLD"], insts
    # primary ticker keyed; multi-ticker line keeps all tickers in display
    vst = leans[0]
    assert vst["direction"] == "long"
    assert "$VST, $CEG, $XLU" in vst["context"], vst
    assert vst["context"].startswith("Long $VST"), vst
    # options: net direction short, no Long/Short prefix in display
    smh = leans[2]
    assert smh["direction"] == "short" and smh["context"].startswith("$SMH puts"), smh
    assert "Short" not in smh["context"], smh
    _ok("parse_lean_block: all leans parsed; primary keyed; multi-ticker + "
        "options display correct")


def test_parse_lean_block_absent():
    from report.pulse_sections import parse_lean_block
    no_block = "## 1. RECAP\nx\n\n## 2. INSIGHTS & ALPHA\ny\n"
    assert parse_lean_block(no_block) == [], "no block -> empty (prose fallback)"
    _ok("parse_lean_block: absent block -> [] (caller falls back to prose)")


def test_strip_lean_block_removes_it():
    from report.pulse_sections import strip_lean_block
    out = strip_lean_block(_PULSE_WITH_LEANS)
    assert "## _LEANS" not in out, "block must be stripped before publish"
    # everything else preserved
    assert "## 3. WHAT TO WATCH" in out and "Gravity finds the casino" in out
    # idempotent
    assert strip_lean_block(out) == out
    _ok("strip_lean_block: removes _LEANS, preserves the rest, idempotent")


def test_routine_strip_keeps_leans():
    """The routine's strip must KEEP _LEANS (so it survives to the bridge)
    while still removing _DRAFT NOTES / _EDIT NOTES."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_strip_internal_notes as p
    md = (
        "## 1. RECAP\nx\n\n"
        "## _DRAFT NOTES (internal)\n- folded a theme\n\n"
        "## _LEANS (internal — TRADE BOARD source)\n- long | $TLT | overdone\n"
    )
    out = p.strip_internal_notes(md)  # default keep=("_LEANS",)
    assert "## _DRAFT NOTES" not in out, "routine must strip _DRAFT NOTES"
    assert "## _LEANS" in out, "routine must KEEP _LEANS for the bridge"
    # bridge-style full strip (keep=()) removes everything internal
    full = p.strip_internal_notes(md, keep=())
    assert "## _LEANS" not in full and "## _DRAFT NOTES" not in full
    _ok("routine strip keeps _LEANS; full strip (keep=()) removes it")


def test_build_lean_display():
    from report.pulse_sections import _build_lean_display
    assert _build_lean_display("long", "$TLT", "overdone") == "Long $TLT — overdone"
    assert _build_lean_display("short", "$USO", "") == "Short $USO"
    assert _build_lean_display("short", "$SMH puts", "x").startswith("$SMH puts")
    # rationale clipped at a word boundary with an ellipsis
    long_r = "a very long rationale that keeps going well past the display " \
             "limit and should be clipped cleanly at a word boundary here"
    d = _build_lean_display("long", "$X", long_r)
    assert d.endswith("…") and " " not in d[-3:], d
    _ok("_build_lean_display: direction prefix, options bare, rationale clip")


def test_draft_emits_leans_block():
    from ai_analysis import prompts
    assert "## _LEANS" in prompts.DRAFT_USER, "DRAFT must emit the _LEANS block"
    assert "TRADE BOARD reads this" in prompts.DRAFT_USER, "purpose missing"
    assert "<direction> | <instrument" in prompts.DRAFT_USER, "format spec missing"
    _ok("DRAFT prompt: emits the machine-readable _LEANS block")


def test_qc_exempts_leans():
    from ai_analysis import prompts
    assert "_LEANS` is NOT a leak" in prompts.QC_USER, \
        "QC must exempt _LEANS from the internal-notes P0 check"
    _ok("QC prompt: _LEANS exempted from the leak check")


def test_bridge_uses_block_with_fallback():
    import github_bridge.jobs as jobs
    src = inspect.getsource(jobs)
    assert "parse_lean_block(markdown)" in src, "bridge must read the block"
    assert "extract_leans_from_markdown(markdown)" in src, "prose fallback missing"
    assert "strip_lean_block(markdown)" in src, "bridge must strip _LEANS"
    _ok("bridge: reads _LEANS block, prose fallback, strips before post")


def test_main_event_steering_rule():
    from ai_analysis import prompts
    assert "WHAT THE TAPE IS ACTUALLY DOING TODAY" in prompts.DRAFT_USER, \
        "main-event break-over-evergreen steering rule missing"
    _ok("DRAFT prompt: MAIN EVENT = the break, not the evergreen trend")


if __name__ == "__main__":
    print("=== structural leans smoke ===")
    test_parse_lean_block_all_leans()
    test_parse_lean_block_absent()
    test_strip_lean_block_removes_it()
    test_routine_strip_keeps_leans()
    test_build_lean_display()
    test_draft_emits_leans_block()
    test_qc_exempts_leans()
    test_bridge_uses_block_with_fallback()
    test_main_event_steering_rule()
    print("\nALL STRUCTURAL LEANS SMOKE TESTS PASS")
