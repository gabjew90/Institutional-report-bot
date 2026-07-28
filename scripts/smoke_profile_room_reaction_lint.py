"""Smoke: profile generator must not invent room reactions.

Context (2026-07-28 ZHawk audit): the generator's input is ONLY the
profiled user's own messages, yet the section schemas demanded
room-reaction framing beats ("the room's running joke", "room
reaction") — structurally forcing invention. Verified fabrications in
ZHawk's live profile: "the room constantly fact-checks his fitness
claims against his Whoop strain scores" (no such messages exist) and
"endless 'is she real' speculation" (one tangential message exists).
Those beats then flow into clapbacks as asserted facts.

Covers:
  - PROFILE_PROMPT carries the room-reaction grounding rule
  - the prompt's schema/example no longer teach room-reaction beats
  - _lint_profile hard-flags room-reaction claims
  - clean self-sourced framing passes the new check
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_prompt_carries_grounding_rule():
    from scripts.backfill_user_profiles import PROFILE_PROMPT as P
    assert "ROOM-REACTION" in P, "room-reaction grounding rule missing"
    assert "only see" in P.lower() or "not in your input" in P.lower(), (
        "rule must explain WHY room reactions are unsourceable"
    )
    _ok("PROFILE_PROMPT carries the room-reaction grounding rule")


def test_prompt_examples_no_longer_teach_room_beats():
    from scripts.backfill_user_profiles import PROFILE_PROMPT as P
    assert "[the room asks weekly" not in P, (
        "the fictional example still teaches an invented room-reaction beat"
    )
    assert "room reaction]" not in P, (
        "the Retarded-takes shape still offers 'room reaction' as a source"
    )
    _ok("prompt examples no longer teach room-reaction beats")


_ROOM_CLAIM_PROFILE = """\
**Test (test, <@1>) — 500 msgs**

**Personality and style.**
Trades big, talks bigger.

**Voice.**
- "test phrase" — [when testing]

**Retarded takes.**
- "test take" — [it was wrong]

**Recent trades.**
- TSLA 400C / closed +10% — [clean win]

**Recent personal life.**
- Runs marathons + [the room constantly fact-checks his fitness claims]
- Dates a model + [leading to endless 'is she real' speculation]
"""

_CLEAN_PROFILE = _ROOM_CLAIM_PROFILE.replace(
    "[the room constantly fact-checks his fitness claims]",
    "[claims sub-3-hour times, has posted zero race results]",
).replace(
    "[leading to endless 'is she real' speculation]",
    "[mentions her dad's Stockholm status in the same breath, every time]",
)


def test_lint_flags_room_reaction_claims():
    from scripts.backfill_user_profiles import _lint_profile
    hard, _soft = _lint_profile(_ROOM_CLAIM_PROFILE, msg_count=500)
    room_hits = [v for v in hard if "room-reaction" in v.lower()
                 or "room reaction" in v.lower()]
    assert room_hits, f"room-reaction claims not flagged; hard={hard}"
    _ok("lint hard-flags invented room-reaction beats")


def test_lint_passes_self_sourced_framing():
    from scripts.backfill_user_profiles import _lint_profile
    hard, _soft = _lint_profile(_CLEAN_PROFILE, msg_count=500)
    room_hits = [v for v in hard if "room-reaction" in v.lower()
                 or "room reaction" in v.lower()]
    assert not room_hits, f"clean framing wrongly flagged: {room_hits}"
    _ok("self-sourced framing beats pass the room-reaction check")


if __name__ == "__main__":
    print("=== profile room-reaction lint smoke ===")
    test_prompt_carries_grounding_rule()
    test_prompt_examples_no_longer_teach_room_beats()
    test_lint_flags_room_reaction_claims()
    test_lint_passes_self_sourced_framing()
    print("\nALL PROFILE ROOM-REACTION LINT SMOKE TESTS PASS")
