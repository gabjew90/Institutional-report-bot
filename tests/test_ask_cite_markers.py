"""Gemini's grounded answers carry inline citation markers in the model
text ("[cite: 1.2.8]", "[1.0.1]", "[1]", "[cite: 1.2.8, 1.2.9]"); four
shipped 2026-09-14..16. Phase 10 strips them before the sources footer
is appended (2026-09-16; widened 2026-09-17 after review: comma lists,
undotted cite markers, and a marker that opens a paragraph used to take
the paragraph break with it)."""
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


def test_comma_lists_undotted_and_plain_numeric_markers_are_removed():
    assert B._strip_citation_markers("prints [cite: 1.2.8, 1.2.9] and") == "prints and"
    assert B._strip_citation_markers("x [cite: 3] y") == "x y"
    assert B._strip_citation_markers("consensus is 55,000 [1] and [2, 3] falling") == "consensus is 55,000 and falling"


def test_a_marker_that_opens_a_paragraph_keeps_the_break():
    a = "…drops today.\n\n[cite: 1.0.1] Contango decays.\n→ a [1.0.1]\n→ b"
    assert B._strip_citation_markers(a) == "…drops today.\n\nContango decays.\n→ a\n→ b"


def test_ordinary_brackets_and_plain_numbers_survive():
    a = "Q3 [preliminary] EPS 1.5 vs [est.] guidance; version 3.11 and $4.04"
    assert B._strip_citation_markers(a) == a


def test_phase_10_calls_the_strip_before_the_footer():
    src = inspect.getsource(B._ask_10_log_and_render)
    assert "_strip_citation_markers(answer)" in src
    assert src.index("_strip_citation_markers(answer)") < src.index("_build_sources_footer(")


def test_the_prompt_no_longer_carries_the_marker_rule():
    # CLAUDE.md policy: a rule enforced in code is deleted from the prompt.
    from discord_bot import ask_prompt
    assert "numeric markers" not in ask_prompt._ASK_SYSTEM_INSTRUCTION
