"""Smoke: analysis requests get a per-request RUN-CODE directive.

Context (2026-07-29): "analyze the trader log" pulled the data but the
model computed win rates IN ITS HEAD and answered in arrows — no code
run (so the arithmetic is unverified), no visual. The "analysis → run
code" rule was one line buried in a 56K-char prompt; flash-lite
averages it away. Fix mirrors the proven _ASK_FACT_DIRECTIVE pattern:
detect an analysis request and APPEND a hard directive to the end of
the system instruction (recency = adherence), where a buried rule
can't reach.
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


def test_detector_fires_on_analysis():
    from discord_bot.bot import _is_analysis_request
    for q in ("analyze the trader log", "analyze ai data center market",
              "compare abe and bk win rates", "break down my trades",
              "run the numbers on this spread", "graph the capex trend",
              "correlate NVDA and SMCI", "what's the distribution of my P&L"):
        assert _is_analysis_request(q), f"should fire: {q!r}"
    _ok("detector fires on analysis-shaped requests")


def test_detector_ignores_plain():
    from discord_bot.bot import _is_analysis_request
    for q in ("what's TSLA at", "when does NVDA report",
              "roast bk", "is spy green today", "thanks"):
        assert not _is_analysis_request(q), f"should NOT fire: {q!r}"
    _ok("detector ignores plain lookups / banter")


# The 2026-07-29 follow-up: a reply to an analysis answer that refines
# the data scope but carries no analysis keyword of its own. The result
# markers ("Pearson correlation r = -0.31") sit thousands of chars up
# in the replied-to block — OUTSIDE the last-600-char ask tail — so the
# padding here mirrors production (only the refinement is in the tail).
_ANALYSIS_CONTEXT = (
    "[MESSAGE BEING REPLIED TO — from omniwiz]\n"
    "Running the matrix yields a Pearson correlation of r = -0.31 "
    "between trader rank and racism rank, a weak inverse trend.\n\n"
    + ("profile filler line about the room and the members. " * 40)
    + "\n[f.jamal's message to you]\n"
)


def test_scope_refinement_followup_fires():
    from discord_bot.bot import _is_analysis_request
    for tail in ("Use the entire population", "now weight it by volume",
                 "add BK and redo", "what about last month only",
                 "exclude the unranked accounts"):
        q = _ANALYSIS_CONTEXT + tail
        assert _is_analysis_request(q), f"follow-up must inherit: {tail!r}"
    _ok("scope-refinement reply to an analysis answer inherits the directive")


def test_banter_reply_to_analysis_does_not_fire():
    from discord_bot.bot import _is_analysis_request
    for tail in ("lol nice", "based", "you're wrong bot", "kys"):
        q = _ANALYSIS_CONTEXT + tail
        assert not _is_analysis_request(q), f"banter reply must not fire: {tail!r}"
    _ok("banter reply to an analysis answer does NOT fire the directive")


def test_directive_demands_code_and_visual():
    from discord_bot.bot import _ASK_ANALYSIS_DIRECTIVE as d
    low = d.lower()
    assert "run" in low and ("python" in low or "code" in low), (
        "directive must demand running code"
    )
    assert "head" in low or "estimate" in low, (
        "directive must forbid in-head / estimated stats"
    )
    assert "visual" in low or "chart" in low, "directive must want a visual"
    # rich, labeled visuals — not a bare matplotlib dump
    assert "axes labeled" in low or ("label" in low and "axes" in low), (
        "directive must require labeled axes"
    )
    assert "whitespace" in low and "tight_layout" in low, (
        "directive must require a tight, no-wasted-space layout"
    )
    _ok("directive demands run-code + eyeballing ban + rich labeled visual")


def test_directive_wired_alongside_fact():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_is_analysis_request(question)" in src, (
        "analysis directive must be composed per-request from the question"
    )
    assert "_ASK_ANALYSIS_DIRECTIVE" in src, "directive not wired in"
    _ok("analysis directive wired into the per-request instruction")


if __name__ == "__main__":
    print("=== analysis directive smoke ===")
    test_detector_fires_on_analysis()
    test_detector_ignores_plain()
    test_scope_refinement_followup_fires()
    test_banter_reply_to_analysis_does_not_fire()
    test_directive_demands_code_and_visual()
    test_directive_wired_alongside_fact()
    print("\nALL ANALYSIS DIRECTIVE SMOKE TESTS PASS")
