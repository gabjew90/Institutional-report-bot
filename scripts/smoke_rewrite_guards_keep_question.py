"""Smoke: answer-rewriting guards must keep answering the question.

2026-07-30 — BK asked "who are the happiest people in the chat? How
about the angriest". The model produced BOTH halves:

    -> **The happiest:** anyone who bought puts before the semi massacre...
    -> **The angriest:** you, twenty minutes after looking at your credit...

The roast-recycle guard fired and the rewrite shipped ONE arrow — the
angriest — aimed at the asker. Half the question silently vanished.

Cause: both rewrite guards open with "Rewrite the following Discord bot
roast" and are handed ONLY the answer. They never see the question, so
they cannot know the answer had two parts, or that it was answering
anything at all. Every answer that reaches them is treated as a single
jab at the asker, and a two-part answer gets collapsed into one.

Both rewriters must receive the question and be told the output is
still an ANSWER: same parts, same format.
"""

import inspect
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _guard_window(src, marker, size=2200):
    i = src.find(marker)
    assert i != -1, f"marker {marker!r} not found — guard renamed?"
    return src[i:i + size]


def test_recycle_rewrite_receives_the_question():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    win = _guard_window(src, "_rr_prompt = (")
    assert "{question" in win, (
        "the roast-recycle rewrite prompt never includes the question, "
        "so it cannot know what the answer was supposed to cover"
    )
    _ok("roast-recycle rewrite is given the question")


def test_pnl_rewrite_receives_the_question():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    win = _guard_window(src, "_pm_prompt = (")
    assert "{question" in win, (
        "the P&L-monotone rewrite prompt never includes the question"
    )
    _ok("P&L-monotone rewrite is given the question")


def test_both_rewrites_demand_the_answer_survive():
    """Not enough to see the question — the prompt must say the output
    is still an answer to it, in the same shape."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    for marker, name in (("_rr_prompt = (", "roast-recycle"),
                         ("_pm_prompt = (", "P&L-monotone")):
        win = _guard_window(src, marker)
        low = win.lower()
        assert "still answer" in low, (
            f"{name} rewrite must require the answer keep answering "
            f"the question — otherwise a two-part answer collapses "
            f"into one jab"
        )
        assert "arrow" in low, (
            f"{name} rewrite must preserve the arrow-bullet format; "
            f"a Type 1 answer rewritten as prose is a format break"
        )
    _ok("both rewrites require the answer (and its shape) to survive")


def test_rewrites_do_not_assume_a_roast():
    """The opening line taught the model that whatever it received was
    a roast at the asker. That framing is what redirected a question
    about the ROOM into a jab at BK."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    for marker, name in (("_rr_prompt = (", "roast-recycle"),
                         ("_pm_prompt = (", "P&L-monotone")):
        win = _guard_window(src, marker)
        assert "Rewrite the following Discord bot roast." not in win, (
            f"{name} still opens by calling the input a roast — that "
            f"framing collapses non-roast answers into jabs at the asker"
        )
    _ok("neither rewrite assumes its input is a roast")


def test_roast_guards_skip_analysis_answers():
    """A roast rewriter has no business touching an analysis.

    Both guards are BANTER-gated (`not _route_is_factual`), and a
    room-ranking question routes LOCAL/BANTER — so an answer built from
    query_data + Python, with a chart attached, was still eligible to be
    "rewritten as a roast". Preserving the question makes that survivable;
    not running it at all is the actual fix.
    """
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    for marker, name in (("_prior_bot_answer_texts):", "roast-recycle"),
                         ("_roast_is_pnl_monotone(answer, profiles_block)):",
                          "P&L-monotone")):
        i = src.find(marker)
        assert i != -1, f"{name} guard condition not found — renamed?"
        cond = src[max(0, i - 320):i + len(marker)]
        assert "_analysis_extra" in cond, (
            f"the {name} guard does not exclude analysis answers — a "
            f"chart-backed ranking can still be rewritten as a roast"
        )
    _ok("roast rewrite guards skip analysis answers")


def test_roast_guards_require_clapback_shape():
    """2026-08-03 — "why is Boeing up 8% today". The RAW answer was
    three clean factual arrows (FAA cert, BNP upgrade, Dow points).
    The pnl-monotone guard then classified it as a lazy roast — it is
    dense with trading vocabulary and has zero personal color, which
    is the DEFINITION of a factual answer — and its rewrite stapled a
    personal jab onto every arrow ("your ex's Instagram stories",
    "your Monday morning hot takes"). The asker complained in the room:
    "Can't even ask the bot real questions anymore."

    Route is the wrong gate: the router misroutes factual questions to
    BANTER regularly (citadel, Boeing, earnings calendar all routed
    LOCAL/BANTER). The answer's own shape is the signal — a roast
    addresses the asker in second person. Both roast guards must
    require _is_clapback_shaped(answer) before treating an answer as a
    roast to fix.
    """
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    for marker, name in (("_prior_bot_answer_texts):", "roast-recycle"),
                         ("_roast_is_pnl_monotone(answer, profiles_block)):",
                          "P&L-monotone")):
        i = src.find(marker)
        assert i != -1, f"{name} guard condition not found — renamed?"
        cond = src[max(0, i - 480):i + len(marker)]
        assert "_is_clapback_shaped(answer)" in cond, (
            f"the {name} guard treats every BANTER-routed answer as a "
            f"roast; it must require the answer to actually address "
            f"the asker (clapback shape) first — this is the guard "
            f"that vandalized the Boeing answer"
        )
    _ok("roast guards require clapback shape, not just BANTER route")


def test_boeing_shape_not_roastable():
    """The exact raw Boeing answer must not qualify as roast-fixable."""
    import discord_bot.bot as bot
    raw = (
        "→ **FAA certification** of the **737 MAX 7** after years of "
        "regulatory delays, clearing the smallest variant for "
        "commercial service\n\n"
        "→ **BNP Paribas upgrade** from Underperform to Outperform "
        "with a price target bump to **$300**\n\n"
        "→ **Index leadership** as the top gainer in the **Dow Jones "
        "Industrial Average**, adding over **80 points** to the index"
    )
    assert not bot._is_clapback_shaped(raw), (
        "a factual answer with no second-person address must not be "
        "clapback-shaped"
    )
    # And a real roast still qualifies:
    roast = ("you're crying about fills again? your last three entries "
             "were chased green candles and you know it")
    assert bot._is_clapback_shaped(roast)
    _ok("factual arrows aren't clapback-shaped; a real roast is")


if __name__ == "__main__":
    print("=== rewrite-guard question-preservation smoke ===")
    test_recycle_rewrite_receives_the_question()
    test_pnl_rewrite_receives_the_question()
    test_both_rewrites_demand_the_answer_survive()
    test_rewrites_do_not_assume_a_roast()
    test_roast_guards_skip_analysis_answers()
    test_roast_guards_require_clapback_shape()
    test_boeing_shape_not_roastable()
    print("\nALL REWRITE-GUARD SMOKE TESTS PASS")
