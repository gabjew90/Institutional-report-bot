"""Find what pipeline_events fired for Tied (uid=442801500008480789),
plus any recent profile-related events to see if the refresh is even
running."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row

UID = 442801500008480789

print("=== Events referencing Tied user_id ===")
rows = c.execute(
    "SELECT created_at, event_type, status, substr(payload, 1, 320) AS p "
    "  FROM pipeline_events "
    " WHERE payload LIKE ? "
    " ORDER BY created_at DESC LIMIT 30",
    (f"%{UID}%",),
).fetchall()
if not rows:
    print("  (none)")
for r in rows:
    p = (r["p"] or "").replace("\n", " ")
    print(f"  {r['created_at']}  {r['event_type']}/{r['status']}  | {p}")

print()
print("=== Last 10 profile_user_failure events (any user) ===")
rows = c.execute(
    "SELECT created_at, status, substr(payload, 1, 260) AS p "
    "  FROM pipeline_events "
    " WHERE event_type = 'profile_user_failure' "
    " ORDER BY created_at DESC LIMIT 10"
).fetchall()
if not rows:
    print("  (none)")
for r in rows:
    p = (r["p"] or "").replace("\n", " ")
    print(f"  {r['created_at']}  {r['status']}  | {p}")

print()
print("=== Last 5 profile-refresh batch events ===")
rows = c.execute(
    "SELECT created_at, event_type, status, substr(payload, 1, 220) AS p "
    "  FROM pipeline_events "
    " WHERE event_type LIKE 'profile%' "
    " ORDER BY created_at DESC LIMIT 15"
).fetchall()
if not rows:
    print("  (none)")
for r in rows:
    p = (r["p"] or "").replace("\n", " ")
    print(f"  {r['created_at']}  {r['event_type']}/{r['status']}  | {p}")

print()
print("=== Did ANY user_profiles row get updated recently? ===")
rows = c.execute(
    """SELECT display_name, username, trader_score, updated_at, message_count_at_update
         FROM user_profiles
        ORDER BY updated_at DESC LIMIT 10"""
).fetchall()
for r in rows:
    print(f"  {r['updated_at']}  {r['display_name']!r:<25s} score={r['trader_score']} msgs={r['message_count_at_update']}")
