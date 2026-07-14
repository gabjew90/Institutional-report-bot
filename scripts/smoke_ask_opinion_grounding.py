"""Smoke: opinion requests exempt from the web-grounding backstop
(2026-07-13 kloh failure).

kloh: "review [substack url] and rank your top 5 most actionable trades
this week." First pass gave a GOOD in-voice answer — $WOLF/$IREN
relative strength, "wait for the bounce on the $80 support", don't
martingale $CRWV/$PLTR. The "$80" matched the factual-specific net, the
WEB-grounding backstop fired, and the bare probe REPLACED the answer
with a persona-less blog summary that "does not rank trades" + an NFA
disclaimer. kloh had to re-ask "now pick your favorites."

The specifics in a rank/pick answer are the bot's RECOMMENDATIONS, not
groundable claims. _is_opinion_request suppresses the broad web trigger
(and its retry-acceptance twin); the hard analyst-fact trigger stays.
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


def test_opinion_detector():
    import discord_bot.bot as bot
    for q in (
        "review https://open.substack.com/... and rank your top 5 most "
        "actionable trades for this week",
        "now pick your favorites out of those setups",
        "what are your top 3 plays this week",
        "thoughts on NVDA into earnings",
        "what do you think about buying the dip here",
        "rank these by conviction",
        "what's your take on the semis pullback",
        "best setups for tomorrow?",
        "would you buy CRWV here",
        "your favorite names right now",
        # the actual "fucking dumb" case (2026-07-13 13:51) — imperative
        # pick-request, matched none of the original patterns
        "Give us 5 names from there near optimal entry",
        "give me some names",
        "name 3 plays for tomorrow",
        "any tickers worth a look",
        "which names are you watching",
        "drop a few setups",
        "5 names near optimal entry",
    ):
        assert bot._is_opinion_request(q), f"must read as opinion: {q!r}"
    # pure factual lookups are NOT opinion — they still get grounded
    for q in (
        "when does ASML report earnings",
        "what is GEO Group's bed count",
        "why is WRAP stock up today",
        "is warsh speaking today",
        "what year did toy story 3 come out",
        "how many beds does GEO operate",
        "what time does the market close friday",
    ):
        assert not bot._is_opinion_request(q), \
            f"factual lookup must NOT read as opinion: {q!r}"
    _ok("opinion detector: rank/pick/take fire; factual lookups don't")


def test_web_trigger_suppressed_on_opinion():
    import discord_bot.bot as bot
    # the exact shape that clobbered kloh: WEB, ungrounded, has a "$80"
    ans = ("wait for the bounce on the $80 support you were asking about, "
           "keep an eye on $WOLF and $IREN relative strength, don't "
           "martingale into $CRWV and $PLTR")
    # normal factual question -> trigger FIRES (it's a groundable claim)
    assert bot._ungrounded_web_specifics(ans, None, was_web=True,
                                         is_opinion=False) is True
    # opinion request -> trigger SUPPRESSED (picks aren't claims)
    assert bot._ungrounded_web_specifics(ans, None, was_web=True,
                                         is_opinion=True) is False
    _ok("web trigger: fires on factual, suppressed on opinion")


def test_hard_fact_trigger_not_suppressed():
    import discord_bot.bot as bot
    # a fabricated PRICE TARGET inside an opinion answer is still a claim
    # — _is_ungrounded_market_fact has no opinion exemption and must
    # still catch it.
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_ground_trigger_shape = _is_ungrounded_market_fact(" in src
    # shape trigger call takes NO is_opinion arg
    shape_call = src.split("_ground_trigger_shape = _is_ungrounded_market_fact(", 1)[1][:120]
    assert "is_opinion" not in shape_call, \
        "the hard analyst-fact trigger must NOT be opinion-suppressed"
    _ok("hard analyst-fact trigger stays active inside opinion answers")


def test_both_call_sites_pass_opinion():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # the fire trigger AND the retry-acceptance re-check both pass it
    assert src.count("is_opinion=_is_opinion_request(question)") >= 2, \
        "both the trigger and the retry-acceptance check must pass opinion"
    _ok("both web-trigger call sites carry the opinion flag")


def test_context_dependent_detector():
    import discord_bot.bot as bot
    # deictic follow-ups (only resolve against the live thread)
    for q in (
        "Give us 5 names from there near optimal entry",
        "now pick your favorites out of those setups",
        "which of those is best",
        "rank them by conviction",
        "pick from the report you mentioned",
        "[MESSAGE BEING REPLIED TO — from omniwiz] x\n\ngive us the best",
        "[VERBATIM RECENT MESSAGES — abe] x\n\nwhat's he holding",
    ):
        assert bot._is_context_dependent(q), f"must be context-dep: {q!r}"
    # self-contained questions — existential 'there' must NOT trip
    for q in (
        "is there a levered south africa etf like EZA",
        "are there any fed speakers thursday",
        "when does ASML report earnings",
        "why is the market down today",
        "what year did toy story 3 come out",
    ):
        assert not bot._is_context_dependent(q), \
            f"self-contained must NOT be context-dep: {q!r}"
    _ok("context detector: deixis + reply-blocks fire; existential 'there' safe")


def test_bare_probe_skipped_on_context_dependent():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # the skip branch precedes the bare-probe else branch
    assert "elif _is_context_dependent(question):" in src, \
        "bare probe must be skipped for context-dependent follow-ups"
    skip = src.split("elif _is_context_dependent(question):", 1)[1][:1400]
    assert "context-blind bare probe" in skip, "skip branch mislabeled"
    assert 'hedged(context-dep-skip)' in skip, "skip must stamp the audit line"
    # and the branch order: the skip is BEFORE the bare-probe else
    assert src.index("elif _is_context_dependent(question):") < \
        src.index("Stage 2 — BARE PROBE"), \
        "skip branch must precede the bare-probe branch"
    _ok("bare probe: skipped on context-dependent follow-ups, keeps in-voice + hedge")


if __name__ == "__main__":
    print("=== opinion-grounding-exemption smoke ===")
    test_opinion_detector()
    test_web_trigger_suppressed_on_opinion()
    test_hard_fact_trigger_not_suppressed()
    test_both_call_sites_pass_opinion()
    test_context_dependent_detector()
    test_bare_probe_skipped_on_context_dependent()
    print("\nALL OPINION-GROUNDING SMOKE TESTS PASS")
