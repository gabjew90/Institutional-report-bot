"""Smoke: the two profile-refresh gap fixes (2026-07-29).

Gap 1 — thin 30-day windows: members with healthy lifetime volume but
<60 in-window messages (arcticaces: 7, itsasandbox: 2, ilonsta: 0)
give the generator nothing to anchor bullets with; generations come
back empty or lint-fail and old text is kept forever. Fix: adaptive
window — thin users get their material re-loaded over 180 days and
run cold-start.

Gap 2 — burst-blind quote verification: Discord users type one
thought across consecutive rapid messages; the model stitches the
burst into one quote; the verifier substring-matched SINGLE messages
only, so genuinely-said stitched quotes registered as hallucinations
(EFDHD hard-failed with 423 in-window messages; kloh's flagged
"escort client" quote spans three messages). Fix: on a first-pass
miss, re-check against whitespace-normalized concatenations of
same-author messages within a 180s gap.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_BURST_ROWS = [
    {"content": "Had an escort client making big $$$",
     "posted_at": "2026-07-20T18:00:05"},
    {"content": "Like 300+ a year",
     "posted_at": "2026-07-20T18:00:31"},
    {"content": "Owns her own place + rents another apt for clients",
     "posted_at": "2026-07-20T18:01:02"},
]

_SPREAD_ROWS = [
    {"content": "Had an escort client making big $$$",
     "posted_at": "2026-07-20T18:00:05"},
    {"content": "Like 300+ a year",
     "posted_at": "2026-07-20T18:20:00"},
    {"content": "Owns her own place + rents another apt for clients",
     "posted_at": "2026-07-20T18:40:00"},
]

_STITCHED = ("Had an escort client making big $$$ Like 300+ a year. "
             "Owns her own place + rents another apt for clients")

_PROFILE_WITH_QUOTE = (
    '**T (t, <@1>) — 100 msgs**\n\n**Voice.**\n- "'
    + _STITCHED + '" — [context]\n'
)


def _run_claims(rows):
    import scripts.backfill_user_profiles as bf

    def fake_match(username, needle, *, limit=10):
        if any(c in needle for c in ("%", "_")) and needle.strip("%") == "":
            return rows  # the fetch-all call
        low = needle.lower()
        return [r for r in rows if low in r["content"].lower()][:limit]

    with patch.object(bf.db, "find_user_messages_matching", fake_match):
        return bf._verify_profile_claims(_PROFILE_WITH_QUOTE, None, "t")


def test_burst_quote_verifies():
    cc = _run_claims(_BURST_ROWS)
    assert cc["unverified_count"] == 0, (
        f"stitched burst quote must verify: {cc['unverified_quotes']}"
    )
    _ok("quote stitched across a 3-message burst verifies")


def test_spread_messages_stay_unverified():
    cc = _run_claims(_SPREAD_ROWS)
    assert cc["unverified_count"] == 1, (
        f"messages 20 min apart are not a burst — must stay "
        f"unverified: {cc}"
    )
    _ok("same text spread over 40 minutes stays unverified")


def test_adaptive_window_wired():
    import inspect
    import scripts.backfill_user_profiles as bf
    src = inspect.getsource(bf)
    assert "_ADAPTIVE_WINDOW_DAYS" in src, "adaptive window constant missing"
    assert "_ADAPTIVE_MIN_MSGS" in src, "thin-user threshold missing"
    # thin users must run cold-start (a widened window can't be an
    # incremental "only new messages" diff against the prior profile)
    seg = src.split("_ADAPTIVE_MIN_MSGS =", 1)[1][:2500]
    assert "cold_uids.add" in seg, (
        "widened users must be marked cold-start"
    )
    _ok("adaptive window wired: thin users widen + run cold-start")


if __name__ == "__main__":
    print("=== profile refresh gaps smoke ===")
    test_burst_quote_verifies()
    test_spread_messages_stay_unverified()
    test_adaptive_window_wired()
    print("\nALL PROFILE REFRESH GAPS SMOKE TESTS PASS")
