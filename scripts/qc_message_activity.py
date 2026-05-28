"""Show the activity distribution across profiled users so we can pick
a sensible 'low activity' threshold for the trader-score penalty."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


hr("A. user_profiles columns — what activity signals are stored?")
for c in conn.execute("PRAGMA table_info(user_profiles)").fetchall():
    print(f"  {c['name']:<35s} {c['type']}")

hr("B. Trader-score distribution alongside message_count")
# message_count_at_update is the column in user_profiles
rows = conn.execute(
    """SELECT display_name, username, trader_score,
              message_count_at_update AS msg_count
         FROM user_profiles
        WHERE trader_score IS NOT NULL
        ORDER BY trader_score DESC"""
).fetchall()
print(f"  {len(rows)} ranked traders\n")
print(f"  {'rank':<5s} {'name':<25s} {'score':>5s} {'msgs':>6s}")
for i, r in enumerate(rows):
    print(f"  #{i+1:<3d}  {(r['display_name'] or r['username'] or '?')[:22]:<22s}  "
          f"{r['trader_score']:>5d}  {r['msg_count'] or 0:>6d}")

hr("C. message_count buckets (where are the cliffs?)")
buckets = [
    ("very low (<50)",   "msg_count < 50"),
    ("low (50-200)",     "msg_count BETWEEN 50 AND 199"),
    ("medium (200-1000)","msg_count BETWEEN 200 AND 999"),
    ("high (1000-5000)", "msg_count BETWEEN 1000 AND 4999"),
    ("very high (5000+)","msg_count >= 5000"),
]
for label, where in buckets:
    n = conn.execute(
        f"SELECT COUNT(*) FROM user_profiles "
        f"WHERE trader_score IS NOT NULL "
        f"  AND message_count_at_update IS NOT NULL "
        f"  AND {where.replace('msg_count','message_count_at_update')}"
    ).fetchone()[0]
    print(f"  {label:<20s}  n={n}")

hr("D. Are low-activity users ranked above active ones? (the bug)")
# Find low-activity users (<200 msgs) ranked HIGHER than mid-activity (200-1000)
rows = conn.execute(
    """SELECT display_name, username, trader_score,
              message_count_at_update AS msg_count
         FROM user_profiles
        WHERE trader_score IS NOT NULL
          AND message_count_at_update < 200
        ORDER BY trader_score DESC"""
).fetchall()
print(f"  {len(rows)} users with <200 msgs but a trader_score:")
for r in rows[:15]:
    print(f"    {(r['display_name'] or r['username'] or '?')[:22]:<22s}  "
          f"score={r['trader_score']:>3d}  msgs={r['msg_count']:>4d}")
