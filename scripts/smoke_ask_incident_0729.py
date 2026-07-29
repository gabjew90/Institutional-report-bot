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

(A score-chart suppression was added then REVERTED at owner request
2026-07-29 — "no need to suppress any data." Score/leaderboard charts
are allowed; only the cutoff and triple-graph fixes remain.)
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


if __name__ == "__main__":
    print("=== /ask 2026-07-29 incident smoke ===")
    test_summary_answer_not_clapback_shaped()
    test_real_clapback_is_clapback_shaped()
    test_only_final_chart_surfaced()
    print("\nALL 2026-07-29 INCIDENT SMOKE TESTS PASS")
