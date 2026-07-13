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


if __name__ == "__main__":
    print("=== opinion-grounding-exemption smoke ===")
    test_opinion_detector()
    test_web_trigger_suppressed_on_opinion()
    test_hard_fact_trigger_not_suppressed()
    test_both_call_sites_pass_opinion()
    print("\nALL OPINION-GROUNDING SMOKE TESTS PASS")
