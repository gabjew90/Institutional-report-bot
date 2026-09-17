"""Gemini's grounded answers carry inline citation markers in the model
text ("[cite: 1.2.8]", "[1.0.1]"); four shipped 2026-09-14..16. Phase 10
strips them before the sources footer is appended (2026-09-16)."""
import inspect

from discord_bot import bot as B


def test_markers_with_and_without_the_cite_prefix_are_removed():
    a = ("→ **The FOMC statement** drops today at **2:00 PM ET** [cite: 1.2.8]\n\n"
         "→ Contango and volatility decay [1.0.1] if held past a quick swing.\n"
         "→ hotter August core inflation [cite: 1.2.8] and energy prints")
    out = B._strip_citation_markers(a)
    assert "[cite" not in out and "[1.0.1]" not in out
    assert out.splitlines()[0].endswith("**2:00 PM ET**")
    assert "volatility decay if held" in out


def test_ordinary_brackets_and_plain_numbers_survive():
    a = "Q3 [preliminary] EPS 1.5 vs [2] estimates; version 3.11 and $4.04"
    assert B._strip_citation_markers(a) == a


def test_phase_10_calls_the_strip_before_the_footer():
    src = inspect.getsource(B._ask_10_log_and_render)
    assert "_strip_citation_markers(answer)" in src
    assert src.index("_strip_citation_markers(answer)") < src.index("_build_sources_footer(")
