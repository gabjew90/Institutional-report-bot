"""Smoke test: format-overhaul Phase 3 — THE MAIN EVENT + BRIEFS split.

The pulse's INSIGHTS section is split into an inverted pyramid: the lead
theme becomes THE MAIN EVENT (deep), the rest become BRIEFS
(compressed). The split is DETERMINISTIC and runs at bridge post-time
AFTER inject_sections — so the DRAFT/AUDIT/lint/validator machinery
upstream keeps operating on the single `## 2. INSIGHTS & ALPHA` header
it was tuned for. Web-safe: both new headers render under the stable
`.insights` CSS class so the dashboard repo needs no change.

Covers: the split function (multi-slot, single-slot, zero-slot,
idempotent, renumbering), the full inject→split order, formatter colors,
the dashboard classifier + fragment wrapping, and the DRAFT depth rule.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_THREE_THEME_INSIGHTS = """# Wartime reigns

## 1. RECAP
Markets grinding higher.

## 2. INSIGHTS & ALPHA

### AI capex supercycle
*Long the builders.* Hyperscaler spend is accelerating. Mechanism: more
DC buildout pulls memory tight. The bull case is durable demand. Long $NVDA.

### Memory squeeze
*Tightness is real.* DRAM contracts firming. Long $MU into the print, inval below 200-day.

### Oil scarcity premium
*Physical is bid.* Brent backwardation steep. Long $BNO, inval on ceasefire.

## 3. WHAT TO WATCH

### Today
- CPI at 8:30 AM ET.
"""


def test_split_three_themes():
    from report.pulse_sections import split_main_event_briefs
    out = split_main_event_briefs(_THREE_THEME_INSIGHTS)
    assert "## 2. THE MAIN EVENT" in out, out
    assert "## 3. BRIEFS" in out, out
    assert "## 4. WHAT TO WATCH" in out, "WHAT TO WATCH must renumber to 4"
    assert "## 2. INSIGHTS & ALPHA" not in out, "old header must be gone"
    # MAIN EVENT carries the FIRST theme only; the next two are under BRIEFS.
    me = out.index("## 2. THE MAIN EVENT")
    br = out.index("## 3. BRIEFS")
    ww = out.index("## 4. WHAT TO WATCH")
    assert me < br < ww, "order must be MAIN EVENT -> BRIEFS -> WHAT TO WATCH"
    main_block = out[me:br]
    briefs_block = out[br:ww]
    assert "AI capex supercycle" in main_block, "lead theme is the MAIN EVENT"
    assert "AI capex supercycle" not in briefs_block
    assert "Memory squeeze" in briefs_block and "Oil scarcity" in briefs_block
    _ok("split: 3-theme INSIGHTS -> MAIN EVENT(1) + BRIEFS(2), WATCH renumbered")


def test_split_idempotent():
    from report.pulse_sections import split_main_event_briefs
    once = split_main_event_briefs(_THREE_THEME_INSIGHTS)
    twice = split_main_event_briefs(once)
    assert once == twice, "second split must be a no-op"
    _ok("split: idempotent on an already-split pulse")


def test_split_single_theme():
    from report.pulse_sections import split_main_event_briefs
    one = _THREE_THEME_INSIGHTS.split("### Memory squeeze")[0] + \
        "## 3. WHAT TO WATCH\n\n### Today\n- CPI.\n"
    out = split_main_event_briefs(one)
    assert "## 2. THE MAIN EVENT" in out, "single theme still becomes MAIN EVENT"
    assert "## BRIEFS" not in out, "no empty BRIEFS section for a single theme"
    assert "## 3. WHAT TO WATCH" in out, "WATCH not renumbered when no BRIEFS"
    _ok("split: single-theme -> MAIN EVENT only, no empty BRIEFS, no renumber")


def test_split_no_slots_untouched():
    from report.pulse_sections import split_main_event_briefs
    md = "## 1. RECAP\ntext\n\n## 2. INSIGHTS & ALPHA\nprose with no h3\n\n## 3. WHAT TO WATCH\nx\n"
    out = split_main_event_briefs(md)
    assert out == md, "an INSIGHTS section with no H3 slots is left untouched"
    _ok("split: zero-slot INSIGHTS left untouched (defensive)")


def test_full_inject_then_split_order():
    from report.pulse_sections import inject_sections, split_main_event_briefs
    # 2026-06-19: WHAT CHANGED + DESK SIGNAL BOARD cut; board-only inject.
    board = "## TRADE BOARD\n\n- **NEW** Long $NVDA\n"
    injected = inject_sections(_THREE_THEME_INSIGHTS, board)
    out = split_main_event_briefs(injected)
    order = [
        out.index("## 1. RECAP"),
        out.index("## 2. THE MAIN EVENT"),
        out.index("## 3. BRIEFS"),
        out.index("## TRADE BOARD"),
        out.index("## 4. WHAT TO WATCH"),
    ]
    assert order == sorted(order), f"section order wrong: {order}"
    # The cut sections must NOT appear.
    assert "## WHAT CHANGED" not in out and "## DESK SIGNAL" not in out
    _ok("full pipeline: RECAP->MAIN EVENT->BRIEFS->TRADE BOARD->WHAT TO WATCH "
        "(WHAT CHANGED + DESK SIGNAL cut)")


def test_leans_extracted_pre_split():
    # extract_leans runs on the INSIGHTS header BEFORE the split in the
    # bridge, so it must still find the closing leans of every slot.
    from report.pulse_sections import extract_leans_from_markdown
    leans = extract_leans_from_markdown(_THREE_THEME_INSIGHTS)
    instruments = {l["instrument"] for l in leans}
    assert "NVDA" in instruments and "MU" in instruments and "BNO" in instruments, instruments
    _ok("leans: still extracted from INSIGHTS pre-split (all 3 slot closes)")


def test_formatter_colors():
    from report.formatter import _get_section_color, SECTION_COLORS
    me = _get_section_color("2. THE MAIN EVENT")
    br = _get_section_color("3. BRIEFS")
    assert me == SECTION_COLORS["MAIN EVENT"], me
    assert br == SECTION_COLORS["BRIEFS"], br
    assert me != br, "MAIN EVENT and BRIEFS should be visually distinct"
    _ok("formatter: MAIN EVENT + BRIEFS get distinct section colors")


def test_dashboard_classifier_and_wrap():
    from scripts.pulse_dashboard import render_pulse_fragment
    md = "---\npdf_count: 5\n---\n" + split_helper()
    html = render_pulse_fragment(md)
    # Both new sections render under the stable .insights class so the
    # dashboard repo's existing CSS styles them (web-safe contract).
    assert 'class="insights"' in html, "MAIN EVENT must carry .insights"
    assert "briefs" in html, "BRIEFS must carry its marker class"
    assert '<div class="insights-body">' in html, "both sections wrap in insights-body"
    # No duplicate #pulse-insights id (only the single-class MAIN EVENT).
    assert html.count('id="pulse-insights"') == 1, "exactly one insights id anchor"
    _ok("dashboard: MAIN EVENT + BRIEFS map to .insights, body-wrapped, single id")


def split_helper():
    from report.pulse_sections import split_main_event_briefs
    return split_main_event_briefs(_THREE_THEME_INSIGHTS)


def test_draft_prompt_depth_rule():
    from ai_analysis import prompts
    src = prompts.DRAFT_USER
    assert "THE MAIN EVENT" in src, "DRAFT must name the lead->MAIN EVENT mapping"
    assert "BRIEFS" in src, "DRAFT must name the rest->BRIEFS mapping"
    assert "250-350" in src, "lead theme deep-word target missing"
    assert re.search(r"80-120|3-4 sentences", src), "brief compression target missing"
    _ok("DRAFT prompt: position-dependent depth (lead deep, rest compressed)")


def test_audit_preserves_profile():
    from ai_analysis import prompts
    audit = prompts.AUDIT_SYSTEM + prompts.AUDIT_USER
    assert "inverted-pyramid depth profile" in audit, \
        "AUDIT must be told not to pad briefs / trim the lead"
    _ok("AUDIT prompt: preserves the inverted-pyramid depth profile")


if __name__ == "__main__":
    print("=== format-overhaul Phase 3 smoke ===")
    test_split_three_themes()
    test_split_idempotent()
    test_split_single_theme()
    test_split_no_slots_untouched()
    test_full_inject_then_split_order()
    test_leans_extracted_pre_split()
    test_formatter_colors()
    test_dashboard_classifier_and_wrap()
    test_draft_prompt_depth_rule()
    test_audit_preserves_profile()
    print("\nALL FORMAT-OVERHAUL PHASE 3 SMOKE TESTS PASS")
