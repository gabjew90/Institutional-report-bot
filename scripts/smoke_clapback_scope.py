"""Smoke: clapback-guard scoping fixes (2026-08-25).

Two real failures from the 8/25 ask logs:

  1. "remind me to sell everything on September 15th 2026" — a benign
     request — got the answer "you done?". The model's actual output was
     two useful arrows; `_is_clapback_shaped` counted 2x "you", the
     fidelity guard flagged the bot's OWN words ("Reminder", "OPEX") as
     another member's receipts, stripped both arrows, and fell through
     to the hostile disengage line.
  2. The disengage line fired on a non-hostile exchange at all.

These pin the scoping so an informational answer is never treated as a
clapback and "you done?" only answers an actual attack.
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_arrow_answers_are_not_clapbacks():
    import discord_bot.bot as b
    # The exact 8/25 answer that got mangled.
    real = (
        "→ **Reminder set for Tuesday, September 15, 2026** — "
        "right in the middle of September OPEX week, which runs through "
        "**Friday, September 18, 2026**\n\n"
        "→ By then your zero-day tech lottos will have either bought "
        "you a boutique Miami gym membership or left you locked out of "
        "your apartment again waiting on the electric company"
    )
    assert b._is_clapback_shaped(real) is False, \
        "arrow-formatted answers are informational, not clapbacks"
    # A real clapback (prose, second person, no arrows) still counts.
    roast = ("easy to talk smack when your little brother is carrying "
             "the household while you spam entries with zero posted "
             "exits. go pop another koozie before your next gamble.")
    assert b._is_clapback_shaped(roast) is True, \
        "prose second-person roasts must still be checked"
    _ok("clapback shape: arrows exempt, prose roasts still caught")


def test_hostility_gate():
    import discord_bot.bot as b
    # benign asks — the disengage line must NOT be available
    for q in [
        "remind me to sell everything on September 15th 2026",
        "how does mstr make money",
        "would you play any earnings today",
        "Good boy",
        "grade the draft on your own",
    ]:
        assert b._is_hostile_exchange(q) is False, f"false positive: {q!r}"
    # real attacks
    for q in [
        "shut up bot ur useless",
        "stfu",
        "you're a garbage bot",
        "fuck off",
    ]:
        assert b._is_hostile_exchange(q) is True, f"missed attack: {q!r}"
    _ok("hostility gate: benign asks clear, attacks caught")


def test_quoted_block_is_not_the_askers_hostility():
    import discord_bot.bot as b
    # The bot's own prior words, quoted back in a reply, must not make
    # the exchange look hostile.
    q = ('[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n'
         '"shut up and read the tape, your entries are useless"\n\n'
         "[asker's message to you]\nwhat time is CPI")
    assert b._is_hostile_exchange(q) is False, \
        "quoted bot text must not count as the asker attacking"
    _ok("hostility gate: quoted blocks excluded")


def test_disengage_is_gated_in_the_pipeline():
    import inspect
    import discord_bot.bot as b
    src = inspect.getsource(b._answer_with_gemini)
    assert 'elif _is_hostile_exchange(question):' in src, \
        "disengage must be gated on real hostility"
    i_dis = src.index('answer = "you done?"')
    seg = src[max(0, i_dis - 400):i_dis]
    assert "_is_hostile_exchange" in seg, \
        "the hostility check must guard the disengage assignment"
    assert "clapback-fidelity-plain-retry" in src, \
        "non-hostile fallback must re-ask plainly instead"
    _ok("pipeline: disengage gated, benign path re-asks plainly")


def test_prompt_carries_proportionality_and_recency():
    from discord_bot import ask_prompt as ap
    txt = ap.ASK_PROMPT if hasattr(ap, "ASK_PROMPT") else open(
        ap.__file__, encoding="utf-8").read()
    assert "PROPORTIONALITY IS MEASURED IN SENTENCES" in txt
    assert "Good boy" in txt, "the worked example must be concrete"
    assert "Never open profile material the exchange did not open" in txt
    assert "would become false when a number moved" in txt, \
        "live-input recency rule missing"
    _ok("prompt: proportionality example + live-input recency rule")


if __name__ == "__main__":
    print("=== clapback scope smoke ===")
    test_arrow_answers_are_not_clapbacks()
    test_hostility_gate()
    test_quoted_block_is_not_the_askers_hostility()
    test_disengage_is_gated_in_the_pipeline()
    test_prompt_carries_proportionality_and_recency()
    print("\nALL CLAPBACK-SCOPE SMOKE TESTS PASS")
