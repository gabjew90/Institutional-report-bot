"""Per-thread SQLite connections (2026-09-01 review, P1).

Before: one connection, every thread, no lock. Transactions are
per-connection, so thread B's commit committed thread A's half-written
rows. These tests pin the new model: the main thread keeps the legacy
module-level connection, every other thread gets its own, writes from
many threads all land, and the legacy `db._conn = None` reset still
works for the scripts that use it.
"""
import sys
import threading

import db


def test_main_thread_keeps_the_module_connection():
    c1 = db.get_connection()
    assert c1 is db._conn
    assert db.get_connection() is c1


def test_worker_threads_get_their_own_connection():
    main = db.get_connection()
    seen = {}

    def worker(i):
        seen[i] = db.get_connection()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert all(c is not main for c in seen.values())
    assert len({id(c) for c in seen.values()}) == 3


def test_concurrent_writers_lose_nothing():
    """The failure this fixes was silent: interleaved implicit
    transactions. 8 threads x 25 inserts through the real helpers must
    yield exactly 200 rows, each committed by the thread that wrote it."""
    conn = db.get_connection()
    conn.execute("CREATE TABLE IF NOT EXISTS _thr_probe (t INTEGER, i INTEGER)")
    conn.execute("DELETE FROM _thr_probe"); conn.commit()
    errors = []

    def writer(t):
        try:
            c = db.get_connection()
            for i in range(25):
                c.execute("INSERT INTO _thr_probe VALUES (?, ?)", (t, i))
                c.commit()
        except Exception as e:  # pragma: no cover
            errors.append(repr(e))

    ts = [threading.Thread(target=writer, args=(t,)) for t in range(8)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert not errors, errors
    n = db.get_connection().execute("SELECT COUNT(*) FROM _thr_probe").fetchone()[0]
    assert n == 200, n


def test_legacy_reset_reopens_on_the_main_thread():
    old = db.get_connection()
    db._conn = None
    new = db.get_connection()
    assert new is not old
    assert new.execute("SELECT 1").fetchone()[0] == 1


def test_reset_connections_forgets_every_thread():
    db.get_connection()
    def w(): db.get_connection()
    t = threading.Thread(target=w); t.start(); t.join()
    db.reset_connections()
    assert db._conn is None
    assert db.get_connection() is not None


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
