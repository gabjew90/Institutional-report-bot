"""Quick lookup for 'ilonsta' or similar — user_profiles + chat_messages."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row

# Try a few variants
for pat in ("ilon", "lonsta", "ilonsta"):
    print(f"\n=== user_profiles LIKE %{pat}% ===")
    rows = c.execute(
        "SELECT user_id, username, display_name, trader_score, "
        "       message_count_at_update, updated_at "
        "  FROM user_profiles "
        " WHERE LOWER(username) LIKE ? OR LOWER(display_name) LIKE ?",
        (f"%{pat}%", f"%{pat}%"),
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  uid={r['user_id']}  username={r['username']!r:<25s}  "
              f"display={r['display_name']!r:<25s}  score={r['trader_score']}  "
              f"msgs={r['message_count_at_update']}  updated={r['updated_at']}")

    print(f"--- chat_messages LIKE %{pat}% ---")
    rows = c.execute(
        "SELECT DISTINCT author_id, author_username, author_display, "
        "       COUNT(*) AS n, MIN(posted_at) AS earl, MAX(posted_at) AS lat "
        "  FROM chat_messages "
        " WHERE LOWER(author_username) LIKE ? OR LOWER(author_display) LIKE ? "
        " GROUP BY author_id",
        (f"%{pat}%", f"%{pat}%"),
    ).fetchall()
    if not rows:
        print("  (none)")
    for r in rows:
        print(f"  uid={r['author_id']}  username={r['author_username']!r:<22s}  "
              f"display={r['author_display']!r:<22s}  "
              f"msgs={r['n']}  range={(r['earl'] or '?')[:10]}..{(r['lat'] or '?')[:10]}")
