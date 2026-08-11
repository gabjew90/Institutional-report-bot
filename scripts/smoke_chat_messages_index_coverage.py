"""Smoke: the hot chat_messages lookups must not full-scan the table.

2026-08-10 latency review. After fixing the 172.9s known_trade_caller_names
join, EXPLAIN QUERY PLAN was run over every SQL literal in db.py against
the production database (189,864 chat_messages). Two index gaps remained,
both on the /ask event loop:

  1. LOWER(author_username) = LOWER(?) — a function on the column makes
     idx_chat_messages_username_ts unusable. Five call sites fell back to
     a full scan + temp B-tree. 0.052s each.
  2. bare posted_at range filters — posted_at is only ever a trailing
     column in the composite indexes, so a window query scanned. 0.035s.

Neither was catastrophic on its own, but they run on the event loop, they
compound across a tool-calling turn, and they get worse as the table grows
— which is exactly how the 172.9s query got there unnoticed.

These assertions are on the PLAN, not on wall-clock, so they hold on a
seeded test database where every query is fast regardless.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn():
    import db as dbmod
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    dbmod._init_schema(c)
    for mid in range(1, 40):
        c.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, author_display, "
            "content, posted_at) VALUES (?,?,?,?,?,?,?,?)",
            (mid, 9, "stonks", 100 + (mid % 5), f"user{mid % 5}",
             f"U{mid % 5}", "gold is ripping", f"2026-08-{(mid % 28) + 1:02d}"))
    c.commit()
    return c


HOT = {
    "resolve_username_to_user_id":
        ("SELECT author_id FROM chat_messages WHERE LOWER(author_username) "
         "= LOWER(?) ORDER BY posted_at DESC LIMIT 1", ("user1",)),
    "get_recent_user_messages":
        ("SELECT content FROM chat_messages WHERE LOWER(author_username) = "
         "LOWER(?) ORDER BY posted_at DESC LIMIT 50", ("user1",)),
    "find_user_messages_matching":
        ("SELECT content FROM chat_messages WHERE LOWER(author_username) = "
         "LOWER(?) AND content LIKE ? COLLATE NOCASE ORDER BY posted_at "
         "DESC LIMIT 25", ("user1", "%gold%")),
    "pending_protected_lookup":
        ("SELECT author_id FROM chat_messages WHERE LOWER(author_username) "
         "= ? AND author_id IS NOT NULL LIMIT 1", ("user1",)),
    "search_chat_messages_for_ask(window)":
        ("SELECT content FROM chat_messages WHERE posted_at >= ? AND "
         "posted_at <= ? ORDER BY posted_at DESC LIMIT 200",
         ("2026-08-01", "2026-08-10")),
    "load_chat_messages_for_profiles":
        ("SELECT content FROM chat_messages WHERE posted_at >= ? ORDER BY "
         "posted_at ASC", ("2026-08-01",)),
}


def test_no_hot_lookup_scans_chat_messages():
    c = _conn()
    bad = []
    for name, (sql, p) in HOT.items():
        plan = " | ".join(str(r[-1]) for r in
                          c.execute("EXPLAIN QUERY PLAN " + sql, p))
        if "SCAN chat_messages" in plan:
            bad.append(f"{name}: {plan}")
    if bad:
        _fail("full scan of chat_messages on the event loop:\n  "
              + "\n  ".join(bad))
    _ok(f"all {len(HOT)} hot lookups use an index, none scan the table")


def test_both_indexes_are_declared():
    """They were created directly on production during the review; the
    schema must own them or a fresh database loses them."""
    c = _conn()
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='chat_messages'")}
    for idx in ("idx_chat_messages_lower_username",
                "idx_chat_messages_posted_at"):
        if idx not in names:
            _fail(f"{idx} is not declared in _init_schema — production has "
                  f"it but a fresh database would not")
    _ok("both review indexes are declared in _init_schema")


def test_lower_form_is_preserved():
    """The expression index only matches LOWER(author_username). If a call
    site is 'simplified' to bare equality it silently scans again."""
    c = _conn()
    bare = ("SELECT author_id FROM chat_messages WHERE author_username = ? "
            "ORDER BY posted_at DESC LIMIT 1")
    plan = " | ".join(str(r[-1]) for r in
                      c.execute("EXPLAIN QUERY PLAN " + bare, ("user1",)))
    if "idx_chat_messages_lower_username" in plan:
        _fail("bare equality unexpectedly matched the expression index — "
              "this test's premise is wrong, re-derive it")
    _ok("expression index matches LOWER(col) only, as documented")


if __name__ == '__main__':
    test_no_hot_lookup_scans_chat_messages()
    test_both_indexes_are_declared()
    test_lower_form_is_preserved()
    print("\nAll chat_messages index-coverage smoke tests passed.")
