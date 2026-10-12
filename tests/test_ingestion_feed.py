"""discord_bot/ingestion_feed.py had no test or smoke reference."""
import sys

from discord_bot import ingestion_feed as F


def _with_channel(value, fn):
    import config
    orig = config.settings.discord_ingest_feed_channel_id
    config.settings.discord_ingest_feed_channel_id = value
    try:
        return fn()
    finally:
        config.settings.discord_ingest_feed_channel_id = orig


def test_feed_disabled_when_channel_blank():
    assert _with_channel("", F.feed_enabled) is False
    assert _with_channel("", F._channel_id) is None


def test_channel_id_parses_and_strips():
    assert _with_channel(" 123456789012345678 ", F._channel_id) == 123456789012345678


def test_garbage_channel_id_is_none_not_a_crash():
    assert _with_channel("not-a-snowflake", F._channel_id) is None


def test_uploaded_label_accepts_both_timestamp_seams():
    """SQLite writes a space, isoformat writes a T; both must parse."""
    a = F._fmt_uploaded("2026-09-01T14:30:00")
    b = F._fmt_uploaded("2026-09-01 14:30:00")
    assert a == b
    assert a == "" or a.startswith("uploaded ")


def test_uploaded_label_never_raises():
    assert F._fmt_uploaded(None) == ""
    assert F._fmt_uploaded("garbage") == ""


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


def test_a_late_claude_lane_analysis_is_still_posted_2026_10_12():
    """The feed walked pdf_file_id upward. With the Claude lane, a HIGH
    note's analysis lands up to two hours after later MEDIUM notes, so the
    cursor passed its id first and it was never posted. The cursor is now
    the analysis row, and each PDF is posted once, at its first analysis."""
    import db

    def _pdf(name):
        return db.insert_pdf_file(f"/Current/x/{name}", name, f"/tmp/{name}")

    def _an(pid, prio, model):
        return db.insert_analysis(pid, None, '{"key_insights": ["x"]}', prio, 1, 1, 0, 0,
                                  model, 0.0)

    conn = db.get_connection()
    old = _pdf("old-already-posted.pdf")
    _an(old, "high", "gemini")
    conn.execute("UPDATE ingest_feed_state SET last_announced_pdf_file_id = ?, "
                 "last_announced_analysis_id = NULL WHERE id = 1", (old,))
    conn.commit()
    high = _pdf("gs-high-note.pdf")          # handed to the Claude lane
    medium = _pdf("rbc-medium-note.pdf")
    _an(medium, "medium", "gemini")          # Gemini finishes first

    cur = db.get_ingest_feed_cursor()        # starts past the old cursor
    row = db.get_next_pdf_to_announce(cur)
    assert row["pf_id"] == medium
    db.set_ingest_feed_cursor(row["analysis_id"], row["pf_id"])

    _an(high, "high", "claude-sonnet-5")     # the lane's record lands later
    row = db.get_next_pdf_to_announce(db.get_ingest_feed_cursor())
    assert row and row["pf_id"] == high, "the late HIGH note must still be posted"
    db.set_ingest_feed_cursor(row["analysis_id"], row["pf_id"])

    _an(medium, "medium", "gemini")          # a /reanalyze is not a new post
    assert db.get_next_pdf_to_announce(db.get_ingest_feed_cursor()) is None
