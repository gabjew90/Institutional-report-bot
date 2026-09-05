"""db.get_room_positions (2026-09-04): the crowding view over the member
trade ledger, with the two ledger traps baked in — is_trade=1 only, and
members counted by author_id, never by display name."""
import sys

import db

_T = "ZZCROWD"   # a ticker nothing real uses


def _seed(rows):
    conn = db.get_connection()
    for author_id, author, action, is_trade in rows:
        conn.execute(
            "INSERT INTO analyst_trades (discord_message_id, discord_attachment_id, author, "
            "posted_at, is_trade, ticker, action, author_id, tracking_mode, extraction_source) "
            "VALUES (?, 0, ?, datetime('now','-1 day'), ?, ?, ?, ?, 'member', 'test')",
            (hash((author_id, author, action)) % 10**12, author, is_trade, _T, action, author_id))
    conn.commit()


def _cleanup():
    conn = db.get_connection()
    conn.execute("DELETE FROM analyst_trades WHERE ticker = ?", (_T,))
    conn.commit()


def test_members_are_distinct_author_ids_and_only_real_trades_count():
    _cleanup()
    try:
        _seed([
            (901, "BK", "open", 1),
            (901, "M&AK", "add", 1),        # same person, renamed: still one member
            (902, "abe", "open", 1),
            (903, "sv", "open", 1),
            (903, "sv", "close", 1),        # one of the three has posted an exit
            (904, "noise", "open", 0),      # extractor judged it not a trade
            (905, "late", "close", 1),      # closing only: got OUT, was never "in" this window
            (906, "trim", "trim", 1),       # a size change is neither entry nor exit
        ])
        res = db.get_room_positions(days=14, min_members=2)
        row = next(p for p in res["positions"] if p["ticker"] == _T)
        assert row["members_entered"] == 3, row      # 901 (twice, renamed), 902, 903
        assert row["members_exited"] == 2, row       # 903 and 905
        assert row["entries"] == 4 and row["exits"] == 2, row
        assert row["members_entered_not_exited"] == 2, row   # 901 and 902
        assert "ENTRY-BIASED" in res["note"]
        assert res["window_days"] == 14
    finally:
        _cleanup()


def test_min_members_filters_singletons_and_days_is_clamped():
    _cleanup()
    try:
        _seed([(905, "solo", "open", 1)])
        res = db.get_room_positions(days=14, min_members=2)
        assert all(p["ticker"] != _T for p in res["positions"])
        res1 = db.get_room_positions(days=14, min_members=1)
        assert any(p["ticker"] == _T and p["members_entered"] == 1 for p in res1["positions"])
        assert db.get_room_positions(days=0)["window_days"] == 1
        assert db.get_room_positions(days=400)["window_days"] == 90
    finally:
        _cleanup()


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
