"""Smoke: P&L-monotone roast guard + personal-color hierarchy
(2026-07-10 user feedback).

"your roasts need to target more personal stuff than trading money
losses cuz it's just lame and repetitive"

Three layers:
  1. Prompt: the clapback section now carries a binding material
     hierarchy — personal color (Recent personal life, Retarded takes,
     Personality, live chat) beats trading-loss jabs.
  2. Code floor: a BANTER roast that is ALL trading-loss vocabulary
     (>=3 P&L hooks incl. bare tickers) and shares ZERO hooks with the
     dossier's personal-color sections gets one rewrite pointed at the
     personal material.
  3. The roast-recycle rewrite directive now leads with personal color.
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


_PROFILE = """WHO'S TALKING (background on people active in this conversation):
- **2pale** (<@1>) — blah:
**2pale (2pale, <@1>) — 2431 msgs**

**Personality and style.**
High-energy chatterbox allergic to receipts, plotting meetups with the regulars.

**Voice.**
- "futures shit easy" — [flex]

**Retarded takes.**
- "get to studying" — [posted a list of ethnic slurs]

**Recent trades.**
- $SOXL long — [recurring pain]

**Recent personal life.**
- [Admitted to being 'out of baha blast'] + [degenerate diet]
- [Lives in 'bumfuck Idaho'] + [remote-location jokes]
- [Claims 'almost a quarter mil in sealed pokemon'] + [cardboard portfolio]
- [Working on a 'fairness opinion for some shitco deal'] + [day-job mockery]
"""


def test_pure_pnl_roast_trips():
    import discord_bot.bot as bot
    monotone = (
        "Your SOXL bags are bleeding, you never post an exit, and your "
        "account is one more entry away from zero. Post a receipt or "
        "keep donating to the casino."
    )
    assert bot._roast_is_pnl_monotone(monotone, _PROFILE) is True, \
        "all-P&L roast with zero personal color must trip"
    _ok("detector: pure trading-loss roast trips")


def test_personal_color_roast_passes():
    import discord_bot.bot as bot
    personal = (
        "You've got a quarter mil in sealed Pokemon and you're writing "
        "fairness opinions for shitcos from bumfuck Idaho. The baha "
        "blast ran out and so did the cope. Your SOXL entries are the "
        "least embarrassing thing about you."
    )
    assert bot._roast_is_pnl_monotone(personal, _PROFILE) is False, \
        "roast anchored in personal color must pass"
    # a factual market answer never trips (few/no P&L-hook density on
    # personal pool is irrelevant — caller gates on BANTER anyway, but
    # the detector itself shouldn't fire on plain prose)
    factual = "ASML reports Wednesday before the open, consensus $7.98."
    assert bot._roast_is_pnl_monotone(factual, _PROFILE) is False
    _ok("detector: personal-color roast + factual prose pass")


def test_personal_pool_extraction():
    import discord_bot.bot as bot
    pool = bot._personal_color_hooks(_PROFILE)
    for hook in ("pokemon", "idaho", "fairnes", "baha"):
        assert any(hook in h.lower() for h in pool), \
            f"personal pool missing {hook!r}: {sorted(pool)[:20]}"
    # trades section is NOT personal color
    assert not any("soxl" == h.lower() for h in pool), \
        "Recent trades must not feed the personal pool"
    _ok("personal pool: life/takes/personality in, trades out")


def test_guard_wired_banter_gated():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_roast_is_pnl_monotone(answer, profiles_block)" in src, \
        "monotone guard not wired"
    win = src.split("P&L-monotone guard", 1)[1][:4200]
    # 2026-07-30: this pinned "not _route_is_factual and profiles_block"
    # as adjacent text and broke when `not _analysis_extra` was inserted
    # between them. Check the condition contains the gate, not its exact
    # spelling.
    _cond_at = src.find("_roast_is_pnl_monotone(answer, profiles_block)):")
    assert _cond_at != -1, "monotone guard condition not found"
    _cond = src[max(0, _cond_at - 320):_cond_at]
    assert "not _route_is_factual" in _cond, "guard must be BANTER-gated"
    assert "not _analysis_extra" in _cond, \
        "guard must not rewrite analysis answers as roasts"
    assert '"pnl-monotone"' in win, "meta stamp missing"
    assert "PERSONAL color" in win, "rewrite must point at personal color"
    assert "_roast_is_pnl_monotone(_pm_answer, profiles_block)" in win, \
        "rewrite acceptance must re-check monotone"
    _ok("wired: BANTER-gated detect -> personal-color rewrite -> re-check")


def test_prompt_hierarchy_present():
    import discord_bot.bot as bot
    ins = bot._ASK_SYSTEM_INSTRUCTION
    assert "Material hierarchy" in ins, "hierarchy rule missing from prompt"
    assert "personal color beats P&L" in ins
    # 2026-07-30: was "the roast's only note". The rule was scoped to
    # Type 3 clapbacks, so Type 2 banter had no material hierarchy at
    # all and kept reaching for 0DTE/blown-account lines. Widened to
    # "jab" + "binds on EVERY type"; the wording moved with it.
    assert "never as the jab's only note" in ins
    assert "binds on EVERY type" in ins, (
        "the hierarchy must not be scoped to clapbacks — banter is "
        "where the recycled P&L material actually shipped"
    )
    _ok("prompt: material hierarchy (personal beats P&L) present")


def test_injection_reorders_personal_above_trades():
    # The tendency-level fix: stored profiles put Recent trades ABOVE
    # Recent personal life, priming money-loss roasts. At injection the
    # personal color must come first.
    import db as _db
    reordered = _db._reorder_profile_for_roast_attention(_PROFILE)
    i_pl = reordered.index("**Recent personal life.**")
    i_tr = reordered.index("**Recent trades.**")
    assert i_pl < i_tr, "personal life must precede trades at injection"
    # content preserved
    for frag in ("quarter mil in sealed pokemon", "$SOXL long",
                 "futures shit easy", "get to studying"):
        assert frag in reordered, f"reorder must not lose content: {frag!r}"
    # missing-section profiles pass through untouched
    thin = "**Personality and style.**\nJust vibes.\n"
    assert _db._reorder_profile_for_roast_attention(thin) == thin
    # wired into the formatter
    import inspect as _i
    src = _i.getsource(_db.format_user_profiles_for_context)
    assert "_reorder_profile_for_roast_attention(" in src, \
        "formatter must apply the reorder"
    _ok("injection reorder: personal life above trades, content intact")


def test_recycle_directive_leads_personal():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # window widened 2026-07-17: the recycle rewrite gained SUBJECT
    # MATERIAL + the novel-content fidelity check, growing the section
    rr = src.split("Roast-recycle guard", 1)[1][:8000]
    assert "PERSONAL color first" in rr, \
        "recycle rewrite must lead with personal material"
    assert "recent trades" not in rr.split("PERSONAL color first", 1)[1][:400], \
        "recycle rewrite must not suggest 'recent trades' as material"
    _ok("recycle directive: personal color first, trades demoted")


if __name__ == "__main__":
    print("=== /ask P&L-monotone + personal-color smoke ===")
    test_pure_pnl_roast_trips()
    test_personal_color_roast_passes()
    test_personal_pool_extraction()
    test_guard_wired_banter_gated()
    test_prompt_hierarchy_present()
    test_injection_reorders_personal_above_trades()
    test_recycle_directive_leads_personal()
    print("\nALL P&L-MONOTONE SMOKE TESTS PASS")
