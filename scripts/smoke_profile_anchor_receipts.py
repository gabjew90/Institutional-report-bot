"""Smoke: profile bullets carry verbatim anchor receipts (R1).

Context (2026-07-28): three invention classes in profile generation —
invented quotes (mechanically checked), invented room reactions
(prose-rule patched), invented biography (BK "works in a clinical
role" — unchecked, shipped into a clapback, publicly denied by the
member). Instead of a third prose rule, the FORMAT changes: every
bullet in Retarded takes / Recent personal life must embed a verbatim
quoted anchor from the user's own messages. The existing claim-check
verifies quote authenticity; this lint rule verifies presence. A
fabricated bullet can't produce a receipt — all three classes die
through one door, and the long room-reaction paragraph shrinks.

Also covers the vocabulary de-bias: the generator rubric was
saturated with "bag-holder" framing (15 occurrences) that homogenized
every dossier into loss-mockery; the ask-prompt's one worked clapback
exemplar was itself a P&L jab contradicting the personal-color rule.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_UNANCHORED_PROFILE = """\
**Test (test, <@1>) — 500 msgs**

**Personality and style.**
Trades big, talks bigger.

**Voice.**
- "test phrase for the ages" — [when testing]

**Retarded takes.**
- Claims AI will replace all traders by next quarter + [pure vibes, no receipt]

**Recent trades.**
- TSLA 400C / closed +10% — [clean win]

**Recent personal life.**
- Works in a clinical role and recruits discord members + [no quote anywhere]
"""

_ANCHORED_PROFILE = _UNANCHORED_PROFILE.replace(
    'Claims AI will replace all traders by next quarter + [pure vibes, no receipt]',
    '"AI will replace every trader by Q4" + [said it the day after buying NVDA calls]',
).replace(
    'Works in a clinical role and recruits discord members + [no quote anywhere]',
    'Says "the salary I make in the hospital goes to retirement" + [treats the day job as a funding source]',
)


def test_prompt_carries_anchor_rule():
    from scripts.backfill_user_profiles import PROFILE_PROMPT as P
    assert "ANCHOR RECEIPTS" in P, "anchor-receipts rule missing"
    assert "ROOM-REACTION" in P, "room-reaction ban must survive inside it"
    _ok("PROFILE_PROMPT carries the anchor-receipts rule")


def test_lint_flags_unanchored_bullets():
    from scripts.backfill_user_profiles import _lint_profile
    hard, _ = _lint_profile(_UNANCHORED_PROFILE, msg_count=500)
    hits = [v for v in hard if "anchor" in v.lower()]
    assert hits, f"unanchored bullets not flagged; hard={hard}"
    _ok("lint hard-flags bullets without a verbatim anchor quote")


def test_lint_passes_anchored_bullets():
    from scripts.backfill_user_profiles import _lint_profile
    hard, _ = _lint_profile(_ANCHORED_PROFILE, msg_count=500)
    hits = [v for v in hard if "anchor" in v.lower()]
    assert not hits, f"anchored bullets wrongly flagged: {hits}"
    _ok("anchored bullets pass")


def test_rubric_debias():
    from scripts.backfill_user_profiles import PROFILE_PROMPT as P
    n = len(re.findall(r"bag[- ]?hold\w*|bag[- ]?holders?\b", P, re.I))
    assert n <= 3, (
        f"generator rubric still carries {n} bag-holder framings — the "
        f"loss-mockery saturation homogenizes every dossier"
    )
    _ok(f"rubric de-biased ({n} bag-holder mentions, ceiling 3)")


def test_ask_prompt_exemplar_swapped():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    assert "full ported those geo calls" not in S, (
        "the worked clapback exemplar is still a P&L jab — it "
        "contradicts the personal-color-beats-P&L rule it sits above"
    )
    assert "personal color beats P&L" in S, "hierarchy rule must survive"
    _ok("clapback exemplar no longer teaches the P&L register")


if __name__ == "__main__":
    print("=== profile anchor receipts smoke ===")
    test_prompt_carries_anchor_rule()
    test_lint_flags_unanchored_bullets()
    test_lint_passes_anchored_bullets()
    test_rubric_debias()
    test_ask_prompt_exemplar_swapped()
    print("\nALL PROFILE ANCHOR RECEIPTS SMOKE TESTS PASS")
