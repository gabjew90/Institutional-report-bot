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
    ):
        assert bot._is_context_dependent(q), f"must be context-dep: {q!r}"
    # self-contained questions — existential 'there' must NOT trip.
    # 2026-07-16: VERBATIM RECENT MESSAGES blocks no longer trip either —
    # they ride along on MOST subject-mention asks (the ALP 22:09 case:
    # a self-contained ticker question got a context-dep skip because
    # incidental channel scrollback was attached). Only explicit reply/
    # forward framing marks true context-dependence now.
    for q in (
        "is there a levered south africa etf like EZA",
        "are there any fed speakers thursday",
        "when does ASML report earnings",
        "why is the market down today",
        "what year did toy story 3 come out",
        "[VERBATIM RECENT MESSAGES — abe] x\n\nwhy is ALP down today",
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


def test_probe_refusal_detector():
    import discord_bot.bot as bot
    # the exact 2026-07-16 shipped refusal (grounded on a BYU page
    # about executioners)
    shipped = (
        'I cannot verify the terms "omniwiz" or "glw" in relation to '
        'being "cooked," as they do not appear in public records or '
        'common internet slang databases. I am unable to confirm your '
        'situation or provide advice on whether you will be saved.'
    )
    assert bot._probe_is_refusal(shipped), "the shipped refusal must match"
    for r in (
        "I cannot verify the existence of the report you mentioned.",
        "Unable to confirm the unlock schedule from available sources.",
        "No public records exist for this claim.",
    ):
        assert bot._probe_is_refusal(r), f"refusal must match: {r!r}"
    # real grounded answers never match
    for a in (
        "GLW is down 36% from its June 30 peak of $271.78.",
        "ASML reports Wednesday July 15 before the open.",
        "The ATF ruling classifies the BolaWrap 150 as a restraint device.",
    ):
        assert not bot._probe_is_refusal(a), f"real answer flagged: {a!r}"
    _ok("probe-refusal detector: refusals match, real answers don't")


def test_probe_refusal_catches_disambiguation():
    import discord_bot.bot as bot
    # 2026-07-15 ALP failure: "so alp never reported?" — the bare probe
    # lost the referent and shipped a "grounded" treatise on what the
    # letters ALP could mean. Disambiguation essays are refusals in a suit.
    for r in (
        "ALP is an acronym with several possible meanings, including "
        "alkaline phosphatase and the Australian Labor Party.",
        "The term has several common meanings depending on the context.",
        "You may be referring to the Australian Labor Party or the "
        "arm's-length principle; please provide more context.",
    ):
        assert bot._probe_is_refusal(r), f"disambiguation must match: {r!r}"
    # real financial answers with incidental hedging words stay clean
    for a in (
        "ALP (Alpine Income Property Trust) reported Q2 results July 10; "
        "FFO of $0.44 beat by a penny.",
        "The stock could move on the CPI print Thursday.",
    ):
        assert not bot._probe_is_refusal(a), f"real answer flagged: {a!r}"
    _ok("probe-refusal: disambiguation essays rejected, real answers pass")


def test_probe_topic_capsule():
    import discord_bot.bot as bot
    # cashtag in the question
    c = bot._probe_topic_capsule("thoughts on $ALP here", "")
    assert "$ALP" in c and "financial context" in c
    # caps ticker only in the PRIOR ANSWER (question is three lowercase
    # words — the exact decontextualization shape)
    c = bot._probe_topic_capsule(
        "so they never reported?",
        "ALP hasn't put out a Q2 number yet, next print is August.",
    )
    assert "$ALP" in c, "capsule must harvest tickers from the prior answer"
    # no tickers anywhere -> no capsule (don't invent a subject)
    assert bot._probe_topic_capsule("will i be saved?", "you're cooked") == ""
    # capsule is wired into the probe question
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_probe_topic_capsule(question, answer)" in src, \
        "capsule must be appended to probe_q"
    _ok("topic capsule: harvests tickers from question+answer, wired into probe")


def test_voice_cleaner_decodes_html_entities():
    import discord_bot.bot as bot
    # 2026-07-15: "Q&nbsp;strategy" shipped literally into Discord
    cleaned, _kinds = bot._clean_voice_violations(
        "the Q&nbsp;strategy note says P&amp;L was flat &gt; expectations"
    )
    assert "&nbsp;" not in cleaned and "&amp;" not in cleaned \
        and "&gt;" not in cleaned, f"entities must decode: {cleaned!r}"
    assert "P&L" in cleaned, "decoded ampersand must survive"
    assert "Q strategy" in cleaned, "NBSP must become a plain space"
    # text without entities passes through untouched
    same, _ = bot._clean_voice_violations("plain answer, no entities")
    assert "plain answer" in same
    _ok("voice cleaner: HTML entities decoded, NBSP -> space")


def test_grounding_accumulated_across_tool_rounds():
    """2026-07-16: 'what were TSM earnings this morning' came back with
    every number exactly right (verified vs the SEC 6-K) and STILL
    shipped a 'couldn't verify' hedge. In the unified mixed-tool config
    the model searches on an early round and then calls a function tool;
    the search's grounding_metadata rides on THAT round's response, and
    the code read gm only off the final text turn — throwing the receipt
    away and stamping a correct, freshly-searched answer 'ungrounded'.
    Grounding chunks must be accumulated across every round."""
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_round_gm_chunks" in src, "per-round grounding accumulator missing"
    # collected INSIDE the tool loop, right after each generate_content
    loop = src.split("for round_idx in range(", 1)[1]
    assert "_round_gm_chunks.extend(" in loop[:1600], \
        "each round's grounding chunks must be collected"
    # merged into the effective gm when the final turn has none
    assert "SimpleNamespace(grounding_chunks=_merged)" in src, \
        "earlier-round evidence must back-fill the final gm"
    assert "grounding recovered from earlier tool-loop round" in src, \
        "recovery must be logged for QC"
    # dedup by URI so repeated chunks don't inflate the sources footer
    assert "_seen_uris" in src, "chunk dedup missing"
    # the shim satisfies the detector
    from types import SimpleNamespace as _SN
    assert bot._grounding_has_sources(_SN(grounding_chunks=[object()])), \
        "detector must accept the merged shim"
    _ok("grounding: search evidence from earlier tool rounds is kept")


def test_probe_gated_and_refusal_rejected():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # LOCAL routes skip the probe entirely (before the context-dep skip)
    assert "elif not needs_web:" in src, \
        "bare probe must be gated to WEB routes"
    local_skip = src.split("elif not needs_web:", 1)[1][:1600]
    assert "hedged(local-skip)" in local_skip, \
        "LOCAL skip must stamp the audit line"
    assert src.index("elif not needs_web:") < \
        src.index("elif _is_context_dependent(question):") < \
        src.index("Stage 2 — BARE PROBE"), "skip branch order wrong"
    # refusal-shaped probe output is treated as no-ground
    probe = src.split("Stage 2 — BARE PROBE", 1)[1][:8000]
    assert "_probe_is_refusal(probe_answer)" in probe, \
        "probe acceptance must reject refusal-shaped output"
    assert '"refusal"' in probe, "refusal state must be stamped"
    _ok("probe: WEB-only + grounded refusals never replace an answer")


if __name__ == "__main__":
    print("=== opinion-grounding-exemption smoke ===")
    test_opinion_detector()
    test_web_trigger_suppressed_on_opinion()
    test_hard_fact_trigger_not_suppressed()
    test_both_call_sites_pass_opinion()
    test_context_dependent_detector()
    test_bare_probe_skipped_on_context_dependent()
    test_probe_refusal_detector()
    test_probe_refusal_catches_disambiguation()
    test_probe_topic_capsule()
    test_voice_cleaner_decodes_html_entities()
    test_grounding_accumulated_across_tool_rounds()
    test_probe_gated_and_refusal_rejected()
    print("\nALL OPINION-GROUNDING SMOKE TESTS PASS")
