"""The weekly dormant sweep writes only the score and the message count
(2026-10-05 audit: it re-stamped updated_at, so stale profiles looked
freshly written, and reset slur_count to 0)."""
from pathlib import Path

import db

UID = 9_400_000_001


def _row():
    return dict(db.get_connection().execute(
        "SELECT profile_text, updated_at, slur_count, trader_score, message_count_at_update "
        "FROM user_profiles WHERE user_id = ?", (UID,)).fetchone())


def test_a_dormant_rescore_touches_only_score_and_count():
    conn = db.get_connection()
    conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (UID,))
    conn.commit()
    db.upsert_user_profile(user_id=UID, username="ksfs", display_name="KsFs",
                           profile_text="**KsFs** old format", message_count_at_update=39,
                           last_seen_message_at="2026-04-29T13:14:24", slur_count=3,
                           trader_score=12)
    conn.execute("UPDATE user_profiles SET updated_at = '2026-04-30 00:00:00' WHERE user_id = ?", (UID,))
    conn.commit()

    db.rescore_dormant_profile(UID, 0)
    r = _row()
    assert r["updated_at"] == "2026-04-30 00:00:00"
    assert r["profile_text"] == "**KsFs** old format" and r["slur_count"] == 3
    assert r["trader_score"] == 0 and r["message_count_at_update"] == 0

    db.rescore_dormant_profile(UID, None)
    assert _row()["trader_score"] == 0, "None keeps the stored score"
    conn.execute("DELETE FROM user_profiles WHERE user_id = ?", (UID,))
    conn.commit()


def test_the_sweep_uses_the_narrow_write():
    src = (Path(__file__).resolve().parents[1] / "scripts" / "backfill_user_profiles.py").read_text(encoding="utf-8")
    sweep = src[src.index("for uid, prior in all_existing_profiles.items():"):src.index("Dormant sweep: rescored")]
    assert "db.rescore_dormant_profile(" in sweep
    assert "upsert_user_profile" not in sweep
