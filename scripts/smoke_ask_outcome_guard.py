"""Smoke: member-outcome guard + passive-aggressive lint (2026-07-02).

QC watch items from v5's first day, fixed structurally (detect→rewrite→
strip, same family as the TA guard — not another advisory prompt line):

#1 Clapbacks can't have no truth behind them: the bot told Cpig he's
   "underwater on your own bags" — his ledger shows ZERO documented
   outcomes. A second-person P&L-STATE assertion now requires a source:
   a lookup_trade_log call this turn, an attribution marker (their own
   claim / a documented post), or a percentage that appears verbatim in
   the injected context. Otherwise: rewrite onto documented behavior,
   strip as fallback.

#2 The condescending faux-advice template ("maybe focus on your own
   portfolio instead of my hydration") now lints as 'passive-aggressive'
   (/ask-only — not in the shared pulse patterns) and feeds the same
   detect->rewrite pass as meta-narration.
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


def test_detector_fires_on_the_observed_failure():
    import discord_bot.bot as bot
    # the exact 2026-07-02 shipped sentence
    ans = ("→ You've been spamming the ticker for weeks, but BK's the one "
           "actually posting the receipt.\n\n→ Don't try to claim the move "
           "now just because you're underwater on your own bags.")
    v = bot._outcome_violations(ans, context_text="")
    assert len(v) == 1 and "underwater" in v[0], v
    assert bot._has_unsourced_outcome_claims(ans, [], "") is True
    # 2026-07-07: the "in the hole" / "holding the bag" / "upside down"
    # P&L idioms escaped the lexicon ("you're deep in the hole on this
    # prison play" shipped).
    for idiom in (
        "you're deep in the hole on this prison play.",
        "you're left holding the bag on those GEO calls.",
        "you're upside down on the whole position.",
        "your book's in the toilet.",
    ):
        assert bot._outcome_violations(idiom, "") == [idiom], \
            f"P&L idiom must flag: {idiom!r}"
    # the strip leaves a working clapback
    stripped = bot._strip_sentences(ans, v)
    assert "underwater" not in stripped and "spamming the ticker" in stripped
    _ok("detector: 'underwater on your own bags' flagged; strip keeps the rest")


def test_third_person_member_claims_flag():
    import discord_bot.bot as bot
    names = ["abe", "bk"]
    # the exact 2026-07-09 shipped sentence — third person, invisible to
    # the 2P-only check, and "homelessness" wasn't in the lexicon.
    abe = ("If you want to speedrun homelessness, Abe’s plays are a hell "
           "of a lot faster than waiting for a corporate lawsuit to catch "
           "up to you.")
    v = bot._outcome_violations(abe, "", names)
    assert len(v) == 1, f"the Abe sentence must flag: {v}"
    # pure third-person member P&L claim (no 'you' at all)
    v2 = bot._outcome_violations("abe's book is bleeding out.", "", names)
    assert len(v2) == 1, f"third-person member claim must flag: {v2}"
    # WITHOUT the name list the old behavior is preserved (2P-only)
    assert bot._outcome_violations("abe's book is bleeding out.", "") == []
    # market prose about non-members stays exempt even with names loaded
    assert bot._outcome_violations(
        "the hyperscalers are bleeding while memory rips", "", names) == []
    # company-applicable words are NOT in the lexicon — "you're betting
    # on them not going insolvent" (ALP, 07-09) is legitimate prose
    assert bot._outcome_violations(
        "you're just betting on them not going insolvent before the next "
        "hype cycle.", "", names) == []
    # attribution still exempts a named member
    assert bot._outcome_violations(
        "abe posted the +65% close, he said he's up big.", "", names) == []
    _ok("third-person: Abe sentence + member claims flag; prose/company/"
        "attribution safe")


def test_member_names_wired():
    import inspect as _i
    import discord_bot.bot as bot
    import db as _db
    assert callable(getattr(_db, "known_trade_caller_names", None)), \
        "db.known_trade_caller_names missing"
    src = _i.getsource(bot._answer_with_gemini)
    assert "_oc_names = _known_member_names()" in src, \
        "guard must load the ledger-caller names"
    assert "_ask_tool_trace, user_content, _oc_names" in src, \
        "names must reach _has_unsourced_outcome_claims"
    _ok("wired: ledger-caller names cached + passed to the guard")


def test_detector_respects_sources():
    import discord_bot.bot as bot
    # (a) the member's OWN claim is attributed -> not flagged
    attributed = "you said you're up 250% and then cried about trimming early"
    assert bot._outcome_violations(attributed, "") == [], "attribution exempts"
    # (b) a percentage sourced from injected context (profile gain_pct)
    ctx = "Recent trades: $DASH 175C / -52.83% documented loss"
    sourced = "you're down 52.83% on those DASH calls, own it"
    assert bot._outcome_violations(sourced, ctx) == [], "context-% exempts"
    # ...but the SAME sentence with no context backing is flagged
    assert len(bot._outcome_violations(sourced, "")) == 1, "unsourced % flags"
    # (c) a lookup_trade_log call this turn exempts the whole answer
    trace = [{"tool": "lookup_trade_log", "status": "ok"}]
    assert bot._has_unsourced_outcome_claims(
        "you're underwater on those bags", trace, "") is False
    # (d) grounding does NOT exempt — the web can't source a member's book
    #     (guard takes no grounding param at all; nothing to assert here)
    # (e) third-person market prose never trips it
    assert bot._outcome_violations(
        "the hyperscalers are bleeding while memory rips", "") == []
    _ok("detector: attribution / context-% / trade-tool exempt; 3rd person safe")


def test_outcome_guard_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_has_unsourced_outcome_claims(" in src, "guard not in answer path"
    assert "user_content" in src.split("_has_unsourced_outcome_claims(", 1)[1][:80], \
        "guard must see the injected context for the sourced-% exemption"
    window = src.split("Member-outcome guard", 1)[1][:6000]
    assert "Do NOT add any new facts" in window, "rewrite must not invent"
    assert "_strip_sentences(" in window, "strip fallback missing"
    _ok("outcome guard wired: detect -> no-new-facts rewrite -> strip fallback")


def test_passive_aggressive_lint():
    import discord_bot.bot as bot
    # the observed 07-02 shape
    _, kinds = bot._clean_voice_violations(
        "tried it. maybe focus on your own portfolio instead of my hydration."
    )
    assert "passive-aggressive" in kinds, kinds
    # 2026-07-07: the WHOLE "maybe if you..." redirect-advice family must
    # flag by shape, not by enumerated tail. Every one of these shipped
    # or slipped a narrow regex at some point.
    family = [
        "maybe put that energy into a trade that doesn't end in a "
        "paperhanded exit for once instead of acting like a soap opera "
        "character.",                                     # 'instead of' tail
        "maybe if you spent less time shouting slurs at the capital "
        "grille and more time actually trading, you wouldn't be so "
        "worried about your rank.",                       # 'less time / more time'
        "if you put half the energy into trading that you put into "
        "coping, you'd be fine.",                         # 'half the energy'
        "maybe channel that energy into a real thesis for once.",  # 'channel energy'
        "maybe focus on your own portfolio instead of my hydration.",  # attention-redirect
    ]
    for t in family:
        _, k = bot._clean_voice_violations(t)
        assert "passive-aggressive" in k, f"faux-advice family miss: {t[:50]!r}"
    # other distinct templates
    for t in ("do with that what you will", "if you say so, champ"):
        _, k = bot._clean_voice_violations(t)
        assert "passive-aggressive" in k, f"template not caught: {t!r}"
    # DIRECT jabs (incl. the rewrite's target shape) do NOT trip it
    for clean in (
        "you full ported geo calls and never posted an exit. that's hoping.",
        "your last five trades were paperhanded exits.",
        "you spend all day posting wojaks and zero exits.",  # 'spend' but no advice lead-in
    ):
        _, k2 = bot._clean_voice_violations(clean)
        assert "passive-aggressive" not in k2, f"false positive on direct jab: {clean!r}"
    _ok("passive-aggressive lint: 'maybe if you' family flagged by shape, direct jabs clean")


def test_empty_answer_no_unbound_hitkinds():
    # 2026-07-05 UnboundLocalError: a blank Gemini payload skipped the
    # `if answer:` block, leaving hit_kinds undefined when the register-
    # rewrite gate read it. hit_kinds is now pre-initialized.
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # the pre-init must appear BEFORE the conditional assignment
    pre_init = src.index("hit_kinds: list[str] = []")
    cond_assign = src.index("answer, hit_kinds = _clean_voice_violations")
    gate = src.index("_register_rewrite_kinds = {")
    assert pre_init < cond_assign < gate, \
        "hit_kinds must be initialized before the conditional assign + gate"
    _ok("empty-answer path: hit_kinds pre-initialized (no UnboundLocalError)")


def test_rewrite_trigger_covers_all_kinds():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # 2026-07-09: the trigger set gained "asker-mockery" (FACT-gated).
    # Assert membership of each kind rather than the exact literal so
    # the set can grow without breaking this smoke.
    gate = src.split("_register_rewrite_kinds = {", 1)
    assert len(gate) == 2, "register rewrite gate missing"
    gate_set = gate[1].split("}", 1)[0]
    for kind in ("meta-narration", "passive-aggressive", "asker-mockery"):
        assert f'"{kind}"' in gate_set, f"rewrite must fire on {kind!r}"
    assert "_pa_directive" in src, "faux-advice directive missing from rewrite"
    assert "_am_directive" in src, "asker-mockery directive missing from rewrite"
    _ok("register rewrite: fires on meta-narration, passive-aggressive, "
        "asker-mockery")


if __name__ == "__main__":
    print("=== /ask outcome-guard + register-lint smoke ===")
    test_detector_fires_on_the_observed_failure()
    test_third_person_member_claims_flag()
    test_member_names_wired()
    test_detector_respects_sources()
    test_outcome_guard_wired()
    test_passive_aggressive_lint()
    test_empty_answer_no_unbound_hitkinds()
    test_rewrite_trigger_covers_all_kinds()
    print("\nALL OUTCOME-GUARD SMOKE TESTS PASS")
