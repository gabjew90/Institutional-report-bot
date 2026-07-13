"""Smoke: quote/lyric-completion handling (2026-07-12).

BK: 'finish the song lyrics: "doctors say I'm the illest cause I'm
suffering from realness got my ..."' — routed LOCAL, answered
ungrounded, and the bot INVENTED a bar ("whole team winnin'") instead
of the real line. The tell that it was a slur-swerve rather than
ignorance: one message later it correctly produced the NEXT line of the
same track (which contains no slur). Wrong and silently dishonest.

Fixes:
1. _QUOTE_COMPLETION_RE forces the WEB route for completion shapes —
   completing verbatim text is a lookup, not a memory exercise.
2. Binding prompt rule: never invent verbatim text — quote the real
   words or own the dodge in room voice; never substitute a plausible
   fake (especially to avoid words the model won't repeat).
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


def test_completion_shapes_match():
    import discord_bot.bot as bot
    for q in (
        "finish the song lyrics: “doctors say I’m the illest "
        "cause I’m suffering from realness got my ….”",
        "finish the lyrics: money on my mind",
        "complete the quote: ask not what your country",
        "what's the next line after 'to be or not to be'",
        "next bar goes how?",
        "how does the song go after the chorus",
        "finish this line for me",
    ):
        assert bot._QUOTE_COMPLETION_RE.search(q), f"must match: {q!r}"
    for q in (
        "why is the market down today",
        "is warsh speaking today",
        "when does ASML report",
        "what do you think of my SNDK line",  # 'line' without completion verb
    ):
        assert not bot._QUOTE_COMPLETION_RE.search(q), \
            f"must not match: {q!r}"
    _ok("completion shapes match; market questions untouched")


def test_web_override_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    win = src.split("_QUOTE_COMPLETION_RE.search(question", 1)
    assert len(win) == 2, "WEB override not wired after the router"
    tail = win[1][:400]
    assert "needs_web = True" in tail, "override must force WEB"
    assert "_route_is_factual = True" in tail, \
        "a completion request is a sincere FACT ask"
    _ok("router override: completion shapes force WEB/FACT")


def test_quotation_first_rule():
    # 2026-07-12 (second pass, user: "structurally fix these stupid
    # dodges"): quotation-FIRST framing. Completing a lyric is quoting
    # published text — the same established rule as quoting members'
    # messages verbatim (no scrubbing). The WEB route puts the real
    # line in search context, where the model quotes readily; a dodge
    # is the last resort, and invented words are never acceptable.
    import discord_bot.bot as bot
    ins = bot._ASK_SYSTEM_INSTRUCTION
    assert "quote it or say nothing, never invent it" in ins, \
        "binding quotation-first rule missing"
    assert "same job as quoting a member's message" in ins, \
        "must anchor to the established verbatim-quotation policy"
    assert "quote it exactly as written" in ins, \
        "grounded completions must be quoted verbatim"
    assert "NEVER swap in invented words" in ins, \
        "the fabrication ban must survive the reframe"
    _ok("prompt: quotation-first, dodge-last, fabrication never")


if __name__ == "__main__":
    print("=== quote/lyric-completion smoke ===")
    test_completion_shapes_match()
    test_web_override_wired()
    test_quotation_first_rule()
    print("\nALL LYRIC-COMPLETION SMOKE TESTS PASS")
