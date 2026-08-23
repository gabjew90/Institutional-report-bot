"""Smoke: the 2026-08-23 memory/cost fixes.

  1. Catchup watermark seals rescans: find_oldest_chat_gap ignores gaps
     before since_iso; set/get round-trips; sealing stops the permanent
     overnight-gap rescan that ran 26x/day storing 0 rows.
  2. memtrim is total: returns an int on every platform, never raises.
  3. Jobs registered: malloc_trim (15 min) + weekly VACUUM, and the
     catchup calls trim + writes the watermark only on scan_ok.
  4. vacuum_db returns page counts (in-memory DB).
"""

import sqlite3
import sys
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE chat_messages (
               channel_id INTEGER, posted_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE chat_catchup_watermark (
               channel_id INTEGER PRIMARY KEY,
               scanned_through TEXT NOT NULL,
               updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    )
    return conn


def test_watermark_seals_gap_scan():
    import db as db_mod
    from datetime import datetime, timedelta, timezone
    conn = _conn()
    now = datetime.now(timezone.utc)
    # messages: one 20 days ago, one 10 days ago -> a huge "gap" between
    old = (now - timedelta(days=20)).isoformat()
    mid = (now - timedelta(days=10)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    conn.executemany(
        "INSERT INTO chat_messages VALUES (1, ?)",
        [(old,), (mid,), (recent,)],
    )
    with patch.object(db_mod, "get_connection", return_value=conn):
        # without a watermark the detector finds the old gap
        g = db_mod.find_oldest_chat_gap(1, days=30, gap_minutes=60)
        assert g == old, (g, old)
        # sealed past the mid message -> the 10d gap disappears; only
        # the mid->recent gap (also >60min) remains
        g2 = db_mod.find_oldest_chat_gap(1, days=30, gap_minutes=60,
                                         since_iso=mid)
        assert g2 == mid, (g2, mid)
        # sealed past everything -> no gap at all
        g3 = db_mod.find_oldest_chat_gap(1, days=30, gap_minutes=60,
                                         since_iso=recent)
        assert g3 is None, g3
        # watermark round-trip
        db_mod.set_catchup_watermark(1, recent)
        assert db_mod.get_catchup_watermark(1) == recent
        db_mod.set_catchup_watermark(1, mid)  # upsert
        assert db_mod.get_catchup_watermark(1) == mid
    _ok("watermark: seals gap scan, round-trips, upserts")


def test_memtrim_total():
    import memtrim
    rc = memtrim.trim()
    assert isinstance(rc, int) and rc in (-1, 0, 1), rc
    memtrim.trim_and_log("smoke")  # must never raise
    _ok(f"memtrim: total on this platform (rc={rc})")


def test_vacuum_returns_counts():
    import db as db_mod
    conn = _conn()
    with patch.object(db_mod, "get_connection", return_value=conn):
        res = db_mod.vacuum_db()
    assert "pages_before" in res and "pages_after" in res, res
    _ok("vacuum_db: runs and reports page counts")


def test_wiring():
    import inspect
    import scheduler.jobs as jobs
    src = inspect.getsource(jobs.setup_scheduler)
    assert 'id="malloc_trim"' in src, "trim job not registered"
    assert 'id="db_vacuum"' in src, "vacuum job not registered"
    import chat_ingestion.watcher as w
    wsrc = inspect.getsource(w.run_chat_catchup)
    assert "get_catchup_watermark" in wsrc, "catchup must read watermark"
    assert "set_catchup_watermark" in wsrc, "catchup must seal on success"
    assert "if scan_ok" in wsrc, "watermark only on full successful walk"
    assert "memtrim" in wsrc, "catchup must trim after the walk"
    _ok("wiring: jobs registered; catchup reads/seals watermark + trims")


if __name__ == "__main__":
    print("=== memory/cost fixes smoke ===")
    test_watermark_seals_gap_scan()
    test_memtrim_total()
    test_vacuum_returns_counts()
    test_wiring()
    print("\nALL MEMORY-COST-FIXES SMOKE TESTS PASS")
