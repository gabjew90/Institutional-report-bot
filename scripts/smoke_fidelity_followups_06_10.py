"""Smoke test: 2026-06-10 fidelity-audit follow-ups.

Background: a content-fidelity audit of the 06-09 pulse against its
exact 117-PDF corpus (recovered from pulse-context git history) found:
  - Zero fabrications (all 17 load-bearing numbers traced to corpus) ✓
  - BUT high-conviction single-name calls were systematically dropped:
    Goldman's same-day MU price-target raise ($400 → $900) went uncited
    while the pulse closed TWO slots on Long $MU; a high-conviction
    BABA Buy (PT $186) vanished entirely.
  - AND only 13 of 86 extracted tension_points carried what_invalidates
    — starving the falsifiable_window close style downstream.

Fixes:
  A. Deep-analysis prompt: `what_invalidates` is REQUIRED on tension
     extraction — hunt for the breaking condition before leaving empty.
  B. synthesizer._format_high_conviction_calls: dedicated theme-coverage
     section for high-conviction US-tradable single-name calls, appended
     at BOTH theme_coverage call sites; DRAFT annotation item 6 binds
     the treatment (cite-when-it's-your-lean + surface-the-strongest).
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


# =====================================================================
# A — what_invalidates required
# =====================================================================

def test_tension_extraction_requires_invalidation():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    assert "`what_invalidates` is REQUIRED whenever you extract a tension point" in src, (
        "tension extraction must REQUIRE what_invalidates"
    )
    # Concrete hunt examples so the model knows what shapes to look for
    for shape in ("below the 50-day", "core CPI above", "if the June FOMC"):
        assert shape in src, f"rule should give hunt example: {shape!r}"
    # The audit stat as the anchor
    assert "13 of 86" in src, (
        "rule should anchor the 2026-06-10 audit stat (13/86 = 15% "
        "populated) so future readers know why this is binding"
    )
    _ok("tension extraction requires what_invalidates with hunt "
        "examples + audit anchor")


# =====================================================================
# B — high-conviction single-name calls section
# =====================================================================

def _mk_analyses():
    from ai_analysis.models import PdfAnalysis, MarketMover, TradeIdea
    return [
        PdfAnalysis(
            pdf_file_id=1, file_name="gs_mu.pdf", source="Goldman Sachs",
            title="Micron PT raise", report_type="research", priority="HIGH",
            market_movers=[
                MarketMover(ticker="MU", action="price_target_change",
                            rating="Neutral", price_target="$900",
                            rationale="Raising TP from $400 on DRAM pricing",
                            conviction="high"),
                # MEDIUM conviction — must NOT appear
                MarketMover(ticker="AAPL", action="reiterate", rating="Buy",
                            price_target="$250", rationale="steady",
                            conviction="medium"),
            ],
        ),
        PdfAnalysis(
            pdf_file_id=2, file_name="gs_japan.pdf", source="Goldman Sachs",
            title="Japan MLCC", report_type="research", priority="MEDIUM",
            market_movers=[
                # Foreign numeric ticker — must be filtered out
                MarketMover(ticker="6981", action="reiterate", rating="Buy",
                            price_target="", rationale="MLCC cycle",
                            conviction="high"),
                # European suffixed ticker — must be filtered out
                MarketMover(ticker="GIVN.S", action="upgrade", rating="Buy",
                            price_target="CHF3300", rationale="profit beat",
                            conviction="high"),
            ],
        ),
        PdfAnalysis(
            pdf_file_id=3, file_name="citi_tgs.pdf", source="Citi",
            title="TGS catalyst watch", report_type="research", priority="HIGH",
            trade_ideas=[
                TradeIdea(description="90-day upside Catalyst Watch on TGS",
                          rationale="NGLs FID", risk="delay",
                          conviction="high", time_horizon="3mo"),
                # low conviction — must NOT appear
                TradeIdea(description="Hold cash", rationale="meh",
                          risk="none", conviction="low"),
            ],
        ),
    ]


def test_high_conviction_section_filters_and_renders():
    from report import synthesizer
    block = synthesizer._format_high_conviction_calls(_mk_analyses())
    assert "HIGH-CONVICTION SINGLE-NAME CALLS" in block
    # The MU call with the PT
    assert "$MU" in block and "$900" in block, block
    assert "Goldman Sachs" in block
    # The high-conviction trade idea with horizon
    assert "TGS" in block and "[3mo]" in block
    # Filtered: foreign tickers + sub-high conviction
    assert "6981" not in block, "Japan numeric ticker must be filtered"
    assert "GIVN.S" not in block, "European suffixed ticker must be filtered"
    assert "AAPL" not in block, "medium conviction must be excluded"
    assert "Hold cash" not in block, "low conviction must be excluded"
    _ok("high-conviction section: renders US-tradable high-conviction "
        "calls, filters foreign + sub-high")


def test_high_conviction_section_empty_when_no_calls():
    from report import synthesizer
    from ai_analysis.models import PdfAnalysis
    quiet = [PdfAnalysis(pdf_file_id=1, file_name="x.pdf", source="Citi",
                         title="t", report_type="research", priority="LOW")]
    assert synthesizer._format_high_conviction_calls(quiet) == "", (
        "no high-conviction calls → empty string (no empty section "
        "header polluting the prompt)"
    )
    _ok("section renders empty (not a bare header) on quiet corpora")


def test_section_carries_binding_treatment():
    from report import synthesizer
    block = synthesizer._format_high_conviction_calls(_mk_analyses())
    assert "MUST cite the call as evidence" in block, (
        "the section header must bind the cite-when-it's-your-lean rule"
    )
    assert "$400→$900" in block or "$400" in block, (
        "the header should anchor the concrete 2026-06-09 MU failure"
    )
    _ok("section header carries binding treatment + the MU anchor")


def test_wired_into_both_theme_coverage_sites():
    from report import synthesizer
    src = inspect.getsource(synthesizer)
    n = src.count("_format_high_conviction_calls(analyses)")
    assert n >= 2, (
        f"_format_high_conviction_calls must be appended at BOTH "
        f"theme_coverage assembly sites (build_pulse_context + "
        f"synthesize_daily_pulse), got {n}"
    )
    _ok(f"wired into {n} theme_coverage assembly sites")


def test_draft_prompt_annotation_present():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    assert "**`HIGH-CONVICTION SINGLE-NAME CALLS`** section" in src, (
        "DRAFT annotations list needs item 6 for the new section"
    )
    assert "two slots closed Long $MU while Goldman's same-day" in src, (
        "annotation must anchor the 2026-06-09 MU-uncited failure"
    )
    assert "surface at least the single strongest remaining call" in src
    _ok("DRAFT annotation item 6 present with MU anchor + "
        "strongest-call rule")


if __name__ == "__main__":
    print("=== 2026-06-10 fidelity follow-ups smoke ===")
    test_tension_extraction_requires_invalidation()
    test_high_conviction_section_filters_and_renders()
    test_high_conviction_section_empty_when_no_calls()
    test_section_carries_binding_treatment()
    test_wired_into_both_theme_coverage_sites()
    test_draft_prompt_annotation_present()
    print("\nALL FIDELITY FOLLOW-UP SMOKE TESTS PASS")
