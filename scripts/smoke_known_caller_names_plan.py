"""Smoke: known_trade_caller_names must not scan the whole message table.

2026-08-10, reported as "responses take minimum 60 seconds now whereas
before they were like 15 seconds max".

The /ask outcome guard calls _known_member_names() on every answer. It
caches for 600s, so on a cache miss it ran db.known_trade_caller_names()
synchronously ON THE DISCORD EVENT LOOP. That function joined
analyst_trades to chat_messages on author_id, and SQLite inverted the
join — SCAN cm USING INDEX idx_chat_messages_username_ts, then a probe
into analyst_trades per row, plus a temp B-tree for DISTINCT. Against
189,855 chat_messages and 49,320 analyst_trades that measured **172.9s
in production**. The gateway heartbeat logged "blocked for more than 130
seconds" and every interaction queued behind it stalled.

Driving from the small side (ledger author_ids, then an indexed probe
into chat_messages) returns the identical 114 rows in 0.146s — 1183x.

This test seeds the real schema and asserts both halves: the result is
unchanged, and the plan never scans chat_messages.
"""

import inspect
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _seeded_conn():
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    # Two ledger members: one tracked by caller name, one by author_id.
    for mid, aid, caller in ((101, 111, "Abe"), (102, 222, None)):
        conn.execute(
            "INSERT INTO analyst_trades (discord_message_id, "
            "discord_attachment_id, author, author_id, posted_at, caller, "
            "is_trade) VALUES (?,?,?,?,?,?,1)",
            (mid, mid, "x", aid, "2026-08-10T00:00:00", caller))
    for mid, aid, uname, disp in (
        (1, 111, "abe.trades", "Abe"),
        (2, 222, "bankerkyle", "BK"),
        (3, 333, "noledger", "Nobody"),   # chats but has no ledger rows
    ):
        conn.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, author_display, "
            "content, posted_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, 9, "stonks", aid, uname, disp, "hi",
             "2026-08-10T00:00:00"))
    conn.commit()
    return conn


def test_result_is_unchanged():
    import db as dbmod
    conn = _seeded_conn()
    prev = dbmod._conn
    dbmod._conn = conn
    try:
        names = dbmod.known_trade_caller_names()
    finally:
        dbmod._conn = prev
    for expected in ("abe", "abe.trades", "bankerkyle", "bk"):
        if expected not in names:
            _fail(f"{expected!r} missing from {names} — the rewrite lost a "
                  f"name the outcome guard needs to protect")
    if "noledger" in names or "nobody" in names:
        _fail(f"a member with no ledger rows leaked in: {names}")
    _ok(f"caller-name union unchanged ({len(names)} names, ledger-only)")


def test_plan_never_scans_chat_messages():
    """The defect was a plan, not a result — assert on the plan."""
    import db as dbmod
    conn = _seeded_conn()
    sql = (
        "SELECT DISTINCT author_username, author_display "
        "FROM chat_messages WHERE author_id IN ("
        "  SELECT author_id FROM analyst_trades "
        "  WHERE author_id IS NOT NULL)"
    )
    plan = " | ".join(
        str(r[-1]) for r in conn.execute("EXPLAIN QUERY PLAN " + sql)
    )
    if "SCAN chat_messages" in plan:
        _fail(f"plan scans the message table: {plan}")
    if "idx_chat_messages_author_ts" not in plan:
        _fail(f"plan does not use the author_id index: {plan}")
    _ok("plan probes chat_messages by author_id, never scans it")


def test_source_does_not_reintroduce_the_join():
    """The inverted-join form is the trap. Fail if it comes back."""
    import db as dbmod
    src = inspect.getsource(dbmod.known_trade_caller_names)
    body = src.split('"""')[-1]  # skip the docstring, which names the bug
    if "JOIN chat_messages" in body:
        _fail("known_trade_caller_names joins chat_messages again — SQLite "
              "inverts this join and scans the whole message table (172.9s "
              "in production, on the Discord event loop)")
    if "author_id IN (" not in body:
        _fail("the small-side subquery form is gone")
    _ok("source keeps the subquery form, no JOIN against chat_messages")


if __name__ == '__main__':
    test_result_is_unchanged()
    test_plan_never_scans_chat_messages()
    test_source_does_not_reintroduce_the_join()
    print("\nAll known-caller-names plan smoke tests passed.")
