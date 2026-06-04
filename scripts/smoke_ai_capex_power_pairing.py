"""Smoke test: mechanical-pairing detector fires AI-capex ↔ data-center
power demand steering in theme_coverage block.

Background: 2026-06-04 QC review observed that AI capex shipped as
INSIGHTS #2 (TS Lombard 85% recycled-capex framing + Broadcom guide-
down + Alphabet $35B equity raise) while data-center power demand
(4 banks: ANZ + BofA + RBC + UBS, promoted by Phase B) got DROPPED
entirely from INSIGHTS and WATCH. The power-demand side is the
mechanically-downstream trade ($XLE / $LNG / $VST / $CEG / $XLU) —
when both surface in the corpus, both must appear in the pulse.

Fix: deterministic pairing detector in _detect_ai_capex_power_demand_pairing
+ a 'MECHANICAL PAIRINGS DETECTED' steering block in the theme_coverage
output that DRAFT reads as a forcing function.

This smoke locks the detector in place.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_detector_fires_on_classic_co_occurrence():
    """AI capex primary + power demand discovered with sufficient
    bank counts → pairing dict returned."""
    from report.synthesizer import _detect_ai_capex_power_demand_pairing
    theme_map = {
        "ai infrastructure and capex": {
            "banks": 7, "sources": ["BofA", "Citi", "GS", "JPM",
                                    "Mizuho", "MS", "UBS"],
            "supportive": 5, "skeptical": 2, "neutral": 0,
            "discovered": False,
        },
        "data center power demand": {
            "banks": 4, "sources": ["ANZ", "BofA", "RBC", "UBS"],
            "supportive": 0, "skeptical": 0, "neutral": 4,
            "discovered": True,
        },
    }
    pairing = _detect_ai_capex_power_demand_pairing(theme_map)
    assert pairing is not None
    assert "ai infrastructure" in pairing["ai_theme"]
    assert "power demand" in pairing["power_theme"]
    assert pairing["ai_banks"] == 7
    assert pairing["power_banks"] == 4
    assert "BofA" in pairing["ai_sources"]
    assert "RBC" in pairing["power_sources"]
    _ok("detector fires on classic AI capex + power demand co-occurrence")


def test_detector_silent_when_only_ai_capex():
    """AI capex without power demand → no pairing."""
    from report.synthesizer import _detect_ai_capex_power_demand_pairing
    theme_map = {
        "ai infrastructure and capex": {
            "banks": 7, "sources": ["A", "B"], "supportive": 5,
            "skeptical": 2, "neutral": 0, "discovered": False,
        },
    }
    pairing = _detect_ai_capex_power_demand_pairing(theme_map)
    assert pairing is None
    _ok("detector silent when only AI capex side present")


def test_detector_silent_when_only_power_demand():
    from report.synthesizer import _detect_ai_capex_power_demand_pairing
    theme_map = {
        "data center power demand": {
            "banks": 4, "sources": ["ANZ", "BofA", "RBC", "UBS"],
            "supportive": 0, "skeptical": 0, "neutral": 4,
            "discovered": True,
        },
    }
    pairing = _detect_ai_capex_power_demand_pairing(theme_map)
    assert pairing is None
    _ok("detector silent when only power demand side present")


def test_detector_silent_below_bank_threshold():
    """AI capex with only 4 banks (threshold = 5) → no pairing fired
    even if power demand is also present. Prevents false-pairs on
    thin AI coverage."""
    from report.synthesizer import _detect_ai_capex_power_demand_pairing
    theme_map = {
        "ai infrastructure and capex": {
            "banks": 4, "sources": ["A", "B", "C", "D"],
            "supportive": 3, "skeptical": 1, "neutral": 0,
            "discovered": False,
        },
        "data center power demand": {
            "banks": 4, "sources": ["ANZ", "BofA", "RBC", "UBS"],
            "supportive": 0, "skeptical": 0, "neutral": 4,
            "discovered": True,
        },
    }
    pairing = _detect_ai_capex_power_demand_pairing(theme_map)
    assert pairing is None, (
        "should not fire when AI capex side has fewer than 5 banks"
    )
    _ok("detector silent below 5-bank threshold on AI capex side")


def test_detector_matches_label_variants():
    """Label variants (hyperscaler / ai buildout / ai chip / etc) on
    upstream side; data-center power / electricity demand / natural
    gas demand on downstream side."""
    from report.synthesizer import _detect_ai_capex_power_demand_pairing
    variants = [
        ("hyperscaler capex cycle", "data-center power demand"),
        ("ai chip and infrastructure", "electricity demand for AI"),
        ("ai buildout funding", "ai energy buildout"),
    ]
    for ai_label, power_label in variants:
        theme_map = {
            ai_label: {
                "banks": 6, "sources": list("ABCDEF"),
                "discovered": False, "supportive": 4, "skeptical": 2,
                "neutral": 0,
            },
            power_label: {
                "banks": 3, "sources": ["ANZ", "BofA", "RBC"],
                "discovered": True, "supportive": 0, "skeptical": 0,
                "neutral": 3,
            },
        }
        pairing = _detect_ai_capex_power_demand_pairing(theme_map)
        assert pairing is not None, (
            f"detector missed variant: ai={ai_label!r} pwr={power_label!r}"
        )
    _ok("detector matches label variants (3 ai × 3 power)")


def test_theme_coverage_block_includes_pairing_steering():
    """When the detector fires, the rendered theme_coverage block must
    include the 'MECHANICAL PAIRINGS DETECTED' section + the explicit
    instrument list ($XLE / $LNG / $VST / $CEG / $XLU)."""
    from report.synthesizer import _format_theme_coverage
    theme_map = {
        "ai infrastructure and capex": {
            "banks": 7, "pdfs": 8, "sources": ["BofA", "Citi", "GS", "JPM"],
            "supportive": 5, "skeptical": 2, "neutral": 0,
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
        },
        "data center power demand": {
            "banks": 4, "pdfs": 4, "sources": ["ANZ", "BofA", "RBC", "UBS"],
            "supportive": 0, "skeptical": 0, "neutral": 4,
            "discovered": True, "non_bank_only": False,
        },
    }
    block = _format_theme_coverage(theme_map)
    assert "MECHANICAL PAIRINGS DETECTED" in block
    assert "AI CAPEX" in block
    assert "POWER DEMAND" in block
    # Required instrument list
    for ticker in ("$XLE", "$LNG", "$VST", "$CEG", "$XLU"):
        assert ticker in block, f"missing instrument {ticker} in pairing block"
    _ok("theme_coverage block includes pairing steering + instrument list")


def test_theme_coverage_block_silent_when_no_pairing():
    """When no pairing fires, the block should NOT contain the steering
    section (no false steering)."""
    from report.synthesizer import _format_theme_coverage
    theme_map = {
        "ecb rate policy": {
            "banks": 5, "pdfs": 5, "sources": ["UBS", "Deutsche", "Danske", "Mizuho", "UniCredit"],
            "supportive": 3, "skeptical": 2, "neutral": 0,
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
        },
    }
    block = _format_theme_coverage(theme_map)
    assert "MECHANICAL PAIRINGS DETECTED" not in block
    _ok("theme_coverage block silent when no pairing fires")


if __name__ == "__main__":
    print("=== AI-capex + power-demand pairing smoke ===")
    test_detector_fires_on_classic_co_occurrence()
    test_detector_silent_when_only_ai_capex()
    test_detector_silent_when_only_power_demand()
    test_detector_silent_below_bank_threshold()
    test_detector_matches_label_variants()
    test_theme_coverage_block_includes_pairing_steering()
    test_theme_coverage_block_silent_when_no_pairing()
    print("\nALL AI-CAPEX + POWER-DEMAND PAIRING SMOKE TESTS PASS")
