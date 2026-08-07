"""Smoke: extraction depth scales with document density; register is plain.

2026-08-07 fidelity probe (52 on-disk PDFs vs their stored analyses):
every document gets the same ~5-8 insight budget regardless of size. A
25-page JPM morning briefing with 271 distinct percentage figures kept
28; a 10-name rated buy screen produced ~2 market_movers, losing ~8
rated names permanently (PDFs are deleted after processing — the
analysis row is the only surviving artifact, and the HC board plus the
future corpus-query product can only rank what extraction kept).

Same audit, register side: the extraction prompt had zero style rules
for free-text fields and modeled dense em-dash usage itself. Output is
currently clean by accident of the model's terse default; these rules
make it enforced.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ai_analysis.prompts import ANALYSIS_SYSTEM_PROMPT  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_density_scaling_rule():
    p = ANALYSIS_SYSTEM_PROMPT
    assert "one insight per distinct section" in p, (
        "key_insights needs the density-scaling teeth — 'no artificial "
        "cap' alone left dense briefings compressed to ~5 insights"
    )
    assert "10-15 entries is normal" in p, (
        "the scaling rule must state the expected magnitude for "
        "multi-topic documents or the model keeps its terse default"
    )
    _ok("extraction depth scales with document density")


def test_every_rated_name_rule():
    p = ANALYSIS_SYSTEM_PROMPT
    assert "one market_movers entry per rated or price-targeted name" in p, (
        "rated-name completeness missing — a 10-name buy screen "
        "produced 2 market_movers and the HC board lost 8 calls"
    )
    _ok("every rated/PT name becomes a market_movers entry")


def test_register_rule():
    p = ANALYSIS_SYSTEM_PROMPT
    assert "neutral and factual" in p, "register rule missing"
    assert "numbers and named mechanisms, not its adjectives" in p, (
        "the carry-numbers-not-adjectives rule is missing — source "
        "promotional framing launders into the pulse as analysis"
    )
    assert "No em-dashes or semicolons in any free-text field" in p, (
        "punctuation rule missing — the prompt itself models em-dashes "
        "and extracted text is quoted into the pulse upstream of lint"
    )
    _ok("free-text register: neutral, numbers-not-adjectives, no em-dash")


def test_new_voice_bans_fire():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "scripts"))
    from pulse_lint import lint_markdown
    bad = (
        "# T\n\n## 1. RECAP\n\n"
        "This is a crucial moment for the pivotal AI tapestry.\n"
        "Additionally, the selloff was a bloodbath.\n"
        "In conclusion, buy calls. Taken together, it works.\n"
    )
    kinds = {i["kind"] for i in lint_markdown(bad)}
    hits = " ".join(str(i) for i in lint_markdown(bad)).lower()
    for w in ("crucial", "pivotal", "tapestry", "additionally",
              "bloodbath", "in conclusion", "taken together"):
        assert w in hits, f"{w!r} not flagged by lint: {kinds}"
    _ok("new bans (crucial/pivotal/tapestry/melodrama/wrap-ups) fire")


def test_accurate_directional_verbs_not_banned():
    """'FCF collapsed 91%' is technically accurate, not melodrama —
    the melodrama family is deliberately pure-theatrics only."""
    from pulse_lint import lint_markdown
    ok = (
        "# T\n\n## 1. RECAP\n\n"
        "Free cash flow collapsed 91% to $784m. Yields soared is not "
        "here. The 30-year plunged 12 points on the print.\n"
    )
    hits = [i for i in lint_markdown(ok)
            if i["kind"] == "melodrama"]
    assert not hits, f"accurate directional verbs over-flagged: {hits}"
    _ok("collapsed/plunged stay legal — accuracy beats blanket bans")


if __name__ == "__main__":
    print("=== extraction prose + fidelity smoke ===")
    test_density_scaling_rule()
    test_every_rated_name_rule()
    test_register_rule()
    test_new_voice_bans_fire()
    test_accurate_directional_verbs_not_banned()
    print("\nALL EXTRACTION PROSE/FIDELITY SMOKE TESTS PASS")
