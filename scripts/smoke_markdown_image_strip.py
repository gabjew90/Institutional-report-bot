"""Smoke: leaked markdown-image tags stripped + no-fabricated-axis rule.

2026-07-29: a code-execution answer led with a raw
`![S&P 500 vs Slur Volume](market_slur_correlation.png)` because the
model wrote a markdown image embed into its text — Discord doesn't
render it and the chart posts as its own embed, so it showed as junk.
Also: the S&P axis of that chart was FABRICATED (historical index
closes aren't in query_data, market tools are current-only, sandbox
has no network) — the directive must forbid inventing a second series.
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


# The exact strip the executor runs on the answer text.
def _strip_md_images(answer):
    a = re.sub(r"!\[([^\]]*)\]\([^)]*\)",
               lambda m: m.group(1).strip(), answer or "")
    return re.sub(r"\n{3,}", "\n\n", a).strip()


def test_markdown_image_stripped():
    a = ("![S&P 500 vs Slur Volume](market_slur_correlation.png)\n\n"
         "→ Peak slur output hit 416 in late May.")
    out = _strip_md_images(a)
    assert "](market_slur" not in out and "![" not in out, out
    assert "Peak slur output hit 416" in out, out
    _ok("leaked markdown image tag stripped, real text kept")


def test_plain_text_untouched():
    a = "→ NVDA is up 2% today.\n\n→ Watch the FOMC."
    assert _strip_md_images(a) == a
    _ok("plain answer untouched by the strip")


def test_strip_wired_in_executor():
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert r"!\[([^\]]*)\]\([^)]*\)" in src, (
        "markdown-image strip must run in the answer pipeline"
    )
    _ok("markdown-image strip wired into the answer pipeline")


def test_directive_forbids_fabricated_axis():
    from discord_bot.bot import _ASK_ANALYSIS_DIRECTIVE as d
    low = d.lower()
    assert "no network" in low and "no price history" in low, (
        "directive must state the sandbox/tools can't get price history"
    )
    assert "fabricate" in low and ("series" in low or "correlation" in low), (
        "directive must forbid inventing a second data series"
    )
    _ok("directive forbids fabricating an unsourceable data series")


if __name__ == "__main__":
    print("=== markdown strip + fabricated-axis smoke ===")
    test_markdown_image_stripped()
    test_plain_text_untouched()
    test_strip_wired_in_executor()
    test_directive_forbids_fabricated_axis()
    print("\nALL MARKDOWN STRIP SMOKE TESTS PASS")
