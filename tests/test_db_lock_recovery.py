"""One lost lock must not freeze a connection (2026-11-02).

The sequence is the production one: a write loses the lock, a read follows on
the same connection, another process commits, and the next write fails at once
with nobody holding a lock. Runs against real SQLite, two connections, WAL.
"""
import asyncio
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db
from discord_bot import ingestion_feed as feed


@pytest.fixture
def pair(tmp_path):
    path = str(tmp_path / "t.db")
    other = sqlite3.connect(path, timeout=0.1)
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("CREATE TABLE t (x)")
    other.execute("INSERT INTO t VALUES (1)")
    other.commit()
    ours = sqlite3.connect(path, timeout=0.1, factory=db._Connection)
    with patch.object(db, "_report_db_lock") as report:
        yield ours, other, report
    ours.close()
    other.close()


def _lose_the_lock(ours, other):
    other.execute("INSERT INTO t VALUES (2)")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ours.execute("UPDATE t SET x = 9")
    other.commit()


def test_the_bug_this_guards_plain_sqlite_stays_frozen(tmp_path):
    """Without the class: the production failure, so the test below means something."""
    path = str(tmp_path / "p.db")
    other = sqlite3.connect(path, timeout=0.1)
    other.execute("PRAGMA journal_mode=WAL")
    other.execute("CREATE TABLE t (x)")
    other.commit()
    plain = sqlite3.connect(path, timeout=0.1)
    _lose_the_lock(plain, other)
    plain.execute("SELECT COUNT(*) FROM t").fetchone()
    other.execute("INSERT INTO t VALUES (3)")
    other.commit()
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        plain.execute("UPDATE t SET x = 9")
    assert plain.in_transaction
    plain.close()
    other.close()


def test_a_lost_lock_ends_the_transaction_and_is_reported(pair):
    ours, other, report = pair
    _lose_the_lock(ours, other)
    assert not ours.in_transaction
    report.assert_called_once_with("UPDATE t SET x = 9")


def test_writes_and_reads_recover_after_a_lost_lock(pair):
    ours, other, _ = pair
    _lose_the_lock(ours, other)
    assert ours.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
    other.execute("INSERT INTO t VALUES (3)")
    other.commit()
    assert ours.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3     # not frozen
    ours.execute("UPDATE t SET x = 9")                                   # not locked
    ours.commit()
    assert other.execute("SELECT COUNT(*) FROM t WHERE x = 9").fetchone()[0] == 3


def test_executemany_recovers_too(pair):
    ours, other, report = pair
    other.execute("INSERT INTO t VALUES (2)")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        ours.executemany("INSERT INTO t VALUES (?)", [(5,), (6,)])
    assert not ours.in_transaction and report.called
    other.rollback()


def test_another_error_leaves_the_transaction_alone(pair):
    ours, _, report = pair
    ours.execute("INSERT INTO t VALUES (7)")
    with pytest.raises(sqlite3.OperationalError):
        ours.execute("SELECT nope FROM t")
    assert ours.in_transaction and not report.called
    ours.commit()
    assert ours.execute("SELECT COUNT(*) FROM t WHERE x = 7").fetchone()[0] == 1


def test_the_live_connection_uses_the_class():
    assert isinstance(db.get_connection(), db._Connection)


def test_a_slow_commit_and_statement_are_logged_with_their_thread(pair, caplog):
    ours, _, _ = pair
    with patch.object(db, "_SLOW_DB_S", 0.0), caplog.at_level("WARNING", logger=db.log.name):
        ours.execute("INSERT INTO t VALUES (8)")
        ours.commit()
    text = caplog.text
    assert "INSERT INTO t VALUES" in text and "commit took" in text and "MainThread" in text


def test_a_quick_commit_says_nothing(pair, caplog):
    ours, _, _ = pair
    with caplog.at_level("WARNING", logger=db.log.name):
        ours.execute("INSERT INTO t VALUES (8)")
        ours.commit()
    assert "took" not in caplog.text


def test_the_pause_watch_starts_once_per_process():
    with patch.object(db, "_pause_watch_started", False), patch.object(db.threading, "Thread") as thread:
        db._start_pause_watch()
        db._start_pause_watch()
    assert thread.call_count == 1 and thread.call_args.kwargs["daemon"] is True


# --- the ingestion feed


def _tick(cursor_results):
    """Two ticks on one note, the cursor save behaving as given."""
    channel = MagicMock(send=AsyncMock())
    bot = MagicMock(get_channel=MagicMock(return_value=channel))
    row = {"pf_id": 42, "analysis_id": 99, "dropbox_path": "/x.pdf", "file_name": "x.pdf"}
    feed._posted_not_saved = None
    with patch.object(feed, "feed_enabled", return_value=True), \
            patch.object(feed, "_channel_id", return_value=1), \
            patch.object(feed, "_build_embed", return_value=MagicMock()), \
            patch.object(feed.db, "get_ingest_feed_cursor", return_value=0), \
            patch.object(feed.db, "get_next_pdf_to_announce", return_value=row), \
            patch.object(feed.db, "set_ingest_feed_cursor", side_effect=cursor_results) as save, \
            patch("dropbox_client.watcher.get_temporary_link", return_value="http://x"):
        for _ in cursor_results:
            asyncio.run(feed.announce_next_pending(bot))
    feed._posted_not_saved = None
    return channel.send.await_count, save.call_count


def test_the_feed_does_not_post_again_when_only_the_cursor_failed():
    locked = sqlite3.OperationalError("database is locked")
    posts, saves = _tick([locked, locked, locked, None])
    assert posts == 1 and saves == 4


def test_a_normal_tick_posts_and_saves():
    assert _tick([None]) == (1, 1)
