"""Smoke test: the dossier's racism rank is the GLOBAL board, with counts.

History. Until 2026-09-26 WHO'S TALKING ranked racism only among the
people active in the conversation, and the bot kept presenting that
ordinal as global standing (2026-06-24: sunny was "#1/1 in this conv"
and was told "you're actually #1"). The fix then was to label the scope
loudly and suppress tiny denominators.

2026-09-26 removed the scope itself: the dossier now carries the global
board rank (db.race_board, race-edged messages over 30 days) with the
count behind it, so there is no conversation-scoped ordinal left to
misread. This smoke pins that: a ranked member shows #N/M with counts,
an unranked member shows zero, and no conversation-scoped wording or
LLM humor score survives.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _profile(name, humor):
    return {
        "display_name": name, "username": name.lower(),
        "racial_humor_score": humor, "slur_count": 19,
        "racism_rationale": "uses race-edged banter",
        "trader_rationale": "mid-pack", "profile_text": f"{name} is a trader.",
        "message_count_at_update": 700,
    }


BOARD = {
    "rows": [{"rank": 7, "user_id": 318, "race_edged": 22, "racial_slurs": 9,
              "messages": 732, "per_100": 3.0}],
    "total": 44, "window_days": 30, "coverage": 1.0,
}


def test_ranked_member_shows_global_rank_with_counts():
    import db
    profiles = {318: _profile("SUNNY", 65), 212: _profile("DarkMark", 15)}
    with patch("db.get_profiles_for_users", return_value=profiles), \
         patch("db.get_global_trader_ranks", return_value=({318: 21, 212: 14}, 59)), \
         patch("db.race_board", return_value=BOARD), \
         patch("db.member_ledger_summary", return_value={}):
        out = db.format_user_profiles_for_context([318, 212])
    assert "racism-rank #7/44 (30d: 22 race-edged of 732 msgs, 9 racial slurs)" in out, out
    assert "racism-rank: unranked (30d: 0 race-edged msgs)" in out, out
    metrics = out.split("\n", 1)[1]  # past the block's own heading
    for stale in ("in this conv", "ACTIVE here", "humor:", "too few active"):
        assert stale not in metrics, f"stale scope/score wording {stale!r}: {out}"
    _ok("dossier: global board rank with counts; unranked shows zero; no conv scope")


if __name__ == "__main__":
    test_ranked_member_shows_global_rank_with_counts()
