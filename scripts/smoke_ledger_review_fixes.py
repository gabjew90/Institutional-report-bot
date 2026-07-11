"""Smoke: trade-ledger / chat-log / rankings review fixes (2026-07-11).

Three defects from the data review:

1. CALLER-BACKFILL BUG — the boot migration stamped caller='abe' on
   every NULL-caller row with no tracking_mode filter, so all 432
   member-mode rows (39 different authors, caller=NULL by design) were
   relabeled as abe's on every boot. Now scoped to caller-mode rows,
   with a repair sweep restoring member rows to NULL.
2. OUTCOME-GUARD REACH — known_trade_caller_names read DISTINCT caller
   only (3 names post-bug) while 39 members had ledger rows. Now unions
   member authors' usernames + display names via chat_messages.
3. OCR CHANNEL GAPS — abe-alerts (47 att msgs/30d) and kyle-alerts (7)
   had ZERO chat-store OCR (missing from eager list), and the main room
   (879 att msgs/30d — where members actually post P&L screenshots)
   fed nothing to the member ledger. All three added to
   chat_eager_ocr_channels.
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


def test_backfill_scoped_and_repaired():
    import db as _db
    src = inspect.getsource(_db)
    assert "WHERE caller IS NULL AND tracking_mode = 'caller'" in src, \
        "abe backfill must be scoped to caller-mode rows"
    assert "SET caller = NULL" in src and \
        "tracking_mode = 'member' AND caller IS NOT NULL" in src, \
        "member-row repair sweep missing"
    _ok("caller backfill: scoped to caller mode + member repair sweep")


def test_guard_name_set_includes_member_authors():
    import db as _db
    src = inspect.getsource(_db.known_trade_caller_names)
    assert "JOIN chat_messages" in src and "author_display" in src, \
        "guard name set must union member authors' names"
    assert "DISTINCT caller" in src, "caller names must stay included"
    _ok("outcome-guard names: callers + member authors (username + display)")


def test_eager_ocr_channels_cover_the_gaps():
    from config import settings
    chans = settings.resolve_chat_eager_ocr_channels()
    for ch in ("🥷🏽-abe-alerts-🥷🏽", "💅🏾-kyle-alerts-💅🏾",
               "💬-stonks-yapping-💬"):
        assert ch in chans, f"eager OCR missing channel: {ch}"
    # the original set stays intact
    assert "💲-gain-loss-porn-💲" in chans
    _ok("eager OCR: abe/kyle alerts + main room covered; originals intact")


if __name__ == "__main__":
    print("=== ledger/chat/rankings review-fixes smoke ===")
    test_backfill_scoped_and_repaired()
    test_guard_name_set_includes_member_authors()
    test_eager_ocr_channels_cover_the_gaps()
    print("\nALL LEDGER-REVIEW SMOKE TESTS PASS")
