"""Smoke: roast-recycle guard + reply-trigger refinement (2026-07-10).

Room feedback after three bot roasts of ZHawk in two minutes: "Omni wiz
doesn't know you or how to insult you at all. pathetic." Two causes:

1. RECYCLING — all three roasts remixed the same four profile hooks
   (GEO bags / no-exit-posted / LARPing / casino). The prompt-level
   anti-recycling block is advisory and got ignored. Now a code-level
   check: >= 4 distinctive hooks shared with a single prior answer to
   the SAME asker forces one rewrite with those hooks banned.
   BANTER-gated — factual answers legitimately repeat facts.

2. TRIGGER SCOPE — the middle roast fired on ZHawk's reply-to-bot that
   tagged @abe. A one-day stand-down for tagged replies was tried and
   REVERSED by user decision (2026-07-11): the bot fires on every reply
   to its own messages, three-way exchanges included — freshness is the
   recycle guard's job, not the trigger's.
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


# The actual shipped roasts (2026-07-10 22:21 and 22:23 UTC).
_ROAST_1 = (
    "You’re calling me slow while you’re still holding those GEO entries "
    "with no exit in sight, pretending your play-money account is a "
    "professional risk-management strategy. You treat this chat like a "
    "rigged casino because you’re just another wageslave LARPing as a "
    "contrarian. Your 'simple and silly' 10k reset system is just a "
    "slow-motion grind to zero."
)
_ROAST_3 = (
    "Keep pivoting to Abe all you want, but your ORCL 290s and GEO "
    "positions are still sitting there with no exits posted. You spend "
    "all year LARPing as a high-roller in Switzerland just to circle "
    "back to micro-cap lottos. If you’re so much smarter than the rest "
    "of the room, try posting a single winning trade instead of just "
    "crying about the casino."
)


def test_detector_fires_on_the_shipped_remix():
    import discord_bot.bot as bot
    shared = bot._recycled_roast_hooks(_ROAST_3, [_ROAST_1])
    assert len(shared) >= bot._RECYCLE_HOOK_MIN, \
        f"the shipped remix must trip the guard: {shared}"
    assert "geo" in [h.lower() for h in shared] or "GEO" in shared, shared
    assert any("larp" in h.lower() for h in shared), shared
    _ok(f"detector: shipped ZHawk remix trips ({len(shared)} shared hooks: "
        f"{', '.join(shared[:6])})")


def test_fresh_roast_passes():
    import discord_bot.bot as bot
    # a roast of the same person built from DIFFERENT profile material
    fresh = (
        "You flew to Switzerland to study their immigration policy and "
        "came home to reset your Robinhood to 10k again. Your close "
        "friend the Raytheon director can't hedge your personality."
    )
    shared = bot._recycled_roast_hooks(fresh, [_ROAST_1])
    assert len(shared) < bot._RECYCLE_HOOK_MIN, \
        f"fresh material must pass: {shared}"
    # and an unrelated factual answer never trips
    factual = "ASML reports Wednesday July 15 before the open."
    assert bot._recycled_roast_hooks(factual, [_ROAST_1]) == [] or \
        len(bot._recycled_roast_hooks(factual, [_ROAST_1])) < \
        bot._RECYCLE_HOOK_MIN
    _ok("detector: fresh-material roast + factual answers pass")


def test_per_prior_answer_not_union():
    import discord_bot.bot as bot
    # two prior answers each sharing 2 hooks must NOT sum to a trip —
    # the comparison is against a SINGLE prior roast (a remix of one),
    # not the union of everything ever said.
    prior_a = "Your GEO bags are a museum exhibit and the casino owns you."
    prior_b = "You LARP as a contrarian while your ORCL entries rot."
    cur = ("GEO down again, the casino always wins, keep LARPing with "
           "those ORCL entries.")
    union_side = (
        set(bot._extract_roast_hooks(cur)) & bot._extract_roast_hooks(prior_a)
    ) | (
        set(bot._extract_roast_hooks(cur)) & bot._extract_roast_hooks(prior_b)
    )
    best = bot._recycled_roast_hooks(cur, [prior_a, prior_b])
    assert len(best) <= len(union_side), (best, union_side)
    assert len(best) == max(
        len(set(bot._extract_roast_hooks(cur))
            & bot._extract_roast_hooks(p)) for p in (prior_a, prior_b)
    ), "must compare per prior answer, not the union"
    _ok("detector: per-prior-answer comparison (not union inflation)")


def test_guard_wired_banter_gated():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # 2026-07-30: this pinned the two terms as ADJACENT text, which broke
    # when `not _analysis_extra` was inserted between them. Check the
    # gate is in the condition, not how it's spelled.
    _cond_at = src.find("_prior_bot_answer_texts):")
    assert _cond_at != -1, "recycle guard condition not found"
    _cond = src[max(0, _cond_at - 320):_cond_at]
    assert "not _route_is_factual" in _cond, \
        "recycle guard must be BANTER-gated"
    assert "not _analysis_extra" in _cond, \
        "recycle guard must not rewrite analysis answers as roasts"
    # window widened 2026-07-17: SUBJECT MATERIAL + fidelity check grew
    # the section
    win = src.split("Roast-recycle guard", 1)[1][:8000]
    assert "_recycled_roast_hooks(answer, _prior_bot_answer_texts)" in win
    assert '"roast-recycle"' in win, "meta stamp missing"
    assert "BANNED" in win, "rewrite must ban the recycled hooks"
    # ("new facts" straddles a source line break — use a contiguous piece)
    assert "Do NOT invent new" in win, "no-new-facts rule missing"
    # accept-check re-runs the detector on the rewrite
    assert "_recycled_roast_hooks(" in win.split("ORIGINAL", 1)[1], \
        "rewrite acceptance must re-check recycling"
    _ok("wired: BANTER-gated detect -> banned-hooks rewrite -> re-check")


def test_reply_trigger_fires_even_when_tagging_others():
    # User decision 2026-07-11 (reversing a one-day stand-down): a reply
    # to the bot's message engages the bot even when it @-tags another
    # user — the bot holds its own in three-way exchanges. Freshness is
    # the recycle guard's job, not the trigger's.
    import discord_bot.bot as bot
    src = inspect.getsource(bot)
    win = src.split("_is_reply_to_bot = bool(", 1)[1][:2200]
    assert "if _is_reply_to_bot and message.mentions:" not in win, \
        "the tagged-reply stand-down was reverted — must not return"
    assert "user decision 2026-07-11" in win, \
        "the reversal must be documented at the trigger site"
    _ok("trigger: reply-to-bot fires even when the reply tags others")


if __name__ == "__main__":
    print("=== /ask roast-recycle + reply-trigger smoke ===")
    test_detector_fires_on_the_shipped_remix()
    test_fresh_roast_passes()
    test_per_prior_answer_not_union()
    test_guard_wired_banter_gated()
    test_reply_trigger_fires_even_when_tagging_others()
    print("\nALL ROAST-RECYCLE SMOKE TESTS PASS")
