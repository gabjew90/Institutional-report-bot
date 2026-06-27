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


def test_draft_requires_main_event_lean_first():
    from ai_analysis import prompts
    assert "FIRST line MUST be the MAIN EVENT" in prompts.DRAFT_USER, \
        "the main-event-lean-first rule is missing from DRAFT"
    assert "main-event-lean-missing" in prompts.DRAFT_USER, \
        "DRAFT must name the validator flag so the model knows the stakes"
    _ok("DRAFT prompt: MAIN EVENT trade must be the first _LEANS line")


# A DRAFT whose MAIN EVENT proposes a rotation ($RSP/$IWM/$GLD) but whose
# _LEANS lists only the briefs' trades — the exact 2026-06-26 board gap.
_DRAFT_MAIN_EVENT_LEAN_MISSING = """# The rubber band snaps

## 1. RECAP
Tech sold off.

## 2. INSIGHTS & ALPHA

### Rotate out of the crowded trade
The leverage unwound. Long the equal-weight and small-cap side ($RSP, $IWM) \
over the cap-weighted index, with $GLD added on dips as the rotation hedge.

### The power bottleneck
Electricity is the ceiling. Long $XLU.

### Oil priced for peace
Crude sold off. Long $XLE into the next Hormuz headline.

## 3. WHAT TO WATCH
- NFP July 2

## _LEANS (internal — TRADE BOARD source, stripped before publish)
- long | $XLE | into the next Hormuz headline
- long | $XLU | power bottleneck
- long | $MU | over the spenders
"""

_DRAFT_MAIN_EVENT_LEAN_PRESENT = _DRAFT_MAIN_EVENT_LEAN_MISSING.replace(
    "- long | $XLE | into the next Hormuz headline",
    "- long | $RSP, $IWM | concentration unwind, equal-weight over $SPY\n"
    "- long | $XLE | into the next Hormuz headline",
)


def test_validator_flags_missing_main_event_lean():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_draft_validate as v
    violations = v.validate(_DRAFT_MAIN_EVENT_LEAN_MISSING, {"theme_map": {}})
    kinds = [x["kind"] for x in violations]
    assert "main-event-lean-missing" in kinds, (
        f"must flag the lead trade absent from _LEANS; got {kinds}")
    flag = next(x for x in violations if x["kind"] == "main-event-lean-missing")
    assert flag["severity"] == "hard", "lead-trade-on-board is a hard violation"
    # it identifies the dropped instruments, not the briefs' present ones
    assert "RSP" in flag["main_event_tickers"], flag
    assert "XLE" not in flag["main_event_tickers"], flag
    _ok("validator: MAIN EVENT trade absent from _LEANS -> hard flag")


def test_validator_passes_when_main_event_lean_present():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_draft_validate as v
    violations = v.validate(_DRAFT_MAIN_EVENT_LEAN_PRESENT, {"theme_map": {}})
    kinds = [x["kind"] for x in violations]
    assert "main-event-lean-missing" not in kinds, (
        f"one overlapping instrument must clear the check; got {kinds}")
    # dollar AMOUNTS in the MAIN EVENT must never be read as tickers
    me_trade = v._trade_tickers_in(
        "Leveraged ETFs are a $200B pile. Long the rotation, $RSP over $SPY.")
    assert me_trade == {"RSP", "SPY"}, me_trade
    _ok("validator: overlapping lead lean clears; $200B not parsed as a ticker")


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
    test_draft_requires_main_event_lean_first()
    test_validator_flags_missing_main_event_lean()
    test_validator_passes_when_main_event_lean_present()
    print("\nALL STRUCTURAL LEANS SMOKE TESTS PASS")
