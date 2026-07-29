"""Smoke: three 2026-07-29 /ask incidents from freshly-shipped code.

1. CUTOFF — "summarize the chat" (BANTER) got mangled mid-sentence
   because the new clapback-fidelity guard fired on it: a summary names
   OTHER members legitimately, the guard read those as non-asker
   material and forced a rewrite that stripped it to "...and". Fix:
   the fidelity guard only applies to CLAPBACK-shaped answers (ones
   that address the asker in second person), never third-person
   informational answers like summaries/leaderboards.

2. TRIPLE GRAPH — the model iterated a chart (draft→draft→final); each
   plt.show() emitted an image part and all three posted. Fix: surface
   only the FINAL rendered image.

3. SCORE CHART — "graph the racism leaderboard" rendered a 0-100 axis,
   exposing the hidden hierarchy the disclosure rules forbid. Fix:
   suppress chart attachment when the topic is the racism/trader
   score hierarchy (text answer, which passes disclosure guards, still
   ships), plus a prompt rule.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_summary_answer_not_clapback_shaped():
    from discord_bot.bot import _is_clapback_shaped
    summary = (
        "→ kyle got dragged over his #8 racism ranking and 0DTE lottos.\n"
        "→ semis & memory weighting sparked a QQQ-vs-VOO debate.\n"
        "→ zebracake claimed the top is in."
    )
    assert not _is_clapback_shaped(summary), (
        "a third-person chat summary must NOT count as a clapback"
    )
    _ok("third-person summary is not clapback-shaped (fidelity skips it)")


def test_real_clapback_is_clapback_shaped():
    from discord_bot.bot import _is_clapback_shaped
    roast = ("you're squatting at #8 while you stress over your overnight "
             "XSP calls, grinding Valorant in Crocs.")
    assert _is_clapback_shaped(roast), (
        "a second-person roast at the asker must still be clapback-shaped"
    )
    _ok("second-person roast still triggers the fidelity guard")


def _img(data, mime="image/png"):
    return SimpleNamespace(
        inline_data=SimpleNamespace(data=data, mime_type=mime),
        text=None, executable_code=None, code_execution_result=None,
        function_call=None)


def test_only_final_chart_surfaced():
    from discord_bot.bot import _extract_code_images
    resp = SimpleNamespace(candidates=[SimpleNamespace(
        content=SimpleNamespace(parts=[
            _img(b"DRAFT1"), _img(b"DRAFT2"), _img(b"FINAL")]))])
    imgs = _extract_code_images(resp)
    assert len(imgs) == 1 and imgs[0][0] == b"FINAL", (
        f"only the final rendered chart should surface, got {imgs}"
    )
    _ok("iterated charts collapse to the final render only")


def test_score_hierarchy_chart_suppressed():
    from discord_bot.bot import _is_score_chart_topic
    for q in ("graph the racism leaderboard",
              "chart everyone's trader score",
              "plot the racism rankings"):
        assert _is_score_chart_topic(q, ""), f"must suppress: {q!r}"
    # a normal chart request is fine
    assert not _is_score_chart_topic(
        "plot the payoff of my NVDA spread", ""), "payoff chart must pass"
    _ok("score/rank-hierarchy chart requests flagged for suppression")


def test_prompt_bans_score_charts():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    low = S.lower()
    assert "never" in low and "chart" in low and (
        "score" in low or "hierarch" in low), (
        "prompt must forbid charting the hidden score hierarchies"
    )
    _ok("prompt forbids charting the hidden score hierarchies")


if __name__ == "__main__":
    print("=== /ask 2026-07-29 incident smoke ===")
    test_summary_answer_not_clapback_shaped()
    test_real_clapback_is_clapback_shaped()
    test_only_final_chart_surfaced()
    test_score_hierarchy_chart_suppressed()
    test_prompt_bans_score_charts()
    print("\nALL 2026-07-29 INCIDENT SMOKE TESTS PASS")
