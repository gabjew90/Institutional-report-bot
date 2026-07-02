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
    # the strip leaves a working clapback
    stripped = bot._strip_sentences(ans, v)
    assert "underwater" not in stripped and "spamming the ticker" in stripped
    _ok("detector: 'underwater on your own bags' flagged; strip keeps the rest")


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
    # other banned templates
    for t in ("do with that what you will",
              "if you say so, champ",
              "if you put half the energy into trading"):
        _, k = bot._clean_voice_violations(t)
        assert "passive-aggressive" in k, f"template not caught: {t!r}"
    # a DIRECT jab does not trip it
    _, k2 = bot._clean_voice_violations(
        "you full ported geo calls and never posted an exit. that's hoping."
    )
    assert "passive-aggressive" not in k2, k2
    _ok("passive-aggressive lint: templates flagged, direct jabs clean")


def test_rewrite_trigger_covers_both_kinds():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert '{"meta-narration", "passive-aggressive"}' in src, \
        "register rewrite must fire on both kinds"
    assert "_pa_directive" in src, "faux-advice directive missing from rewrite"
    _ok("register rewrite: fires on meta-narration AND passive-aggressive")


if __name__ == "__main__":
    print("=== /ask outcome-guard + register-lint smoke ===")
    test_detector_fires_on_the_observed_failure()
    test_detector_respects_sources()
    test_outcome_guard_wired()
    test_passive_aggressive_lint()
    test_rewrite_trigger_covers_both_kinds()
    print("\nALL OUTCOME-GUARD SMOKE TESTS PASS")
