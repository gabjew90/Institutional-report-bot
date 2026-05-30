"""Compare which thin users got rescored vs which still stuck."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row

print("=== Thin-user profile state ===")
print(f"{'name':<14s} {'score':>5s} {'msgs':>5s} {'prof_len':>8s}  {'updated':<20s}")
for name in ("Tied", "KsFs", "Soysoo", "Rafa", "kravesauce", "Quackers",
             "PenguinTooFar", "Cpig", "TheMelvin"):
    r = c.execute(
        "SELECT display_name, trader_score, message_count_at_update, "
        "       updated_at, length(profile_text) AS pl "
        "  FROM user_profiles WHERE LOWER(display_name) LIKE ?",
        (f"%{name.lower()}%",),
    ).fetchone()
    if r:
        print(f"  {r['display_name']:<14s} {r['trader_score']:>3d}   "
              f"{r['message_count_at_update']:>4d}  {r['pl']:>5d}    "
              f"{r['updated_at']}")
    else:
        print(f"  {name:<14s}  NOT FOUND")

print()
print("=== Last 25 profile_user_failure events (any user) ===")
rows = c.execute(
    "SELECT created_at, status, substr(payload, 1, 200) AS p "
    "  FROM pipeline_events "
    " WHERE event_type = 'profile_user_failure' "
    " ORDER BY created_at DESC LIMIT 25"
).fetchall()
for r in rows:
    p = (r["p"] or "").replace("\n", " ")
    print(f"  {r['created_at']}  {r['status']:<22s}  | {p}")

print()
print("=== Most recent profile_refresh batches ===")
rows = c.execute(
    "SELECT created_at, status, substr(payload, 1, 200) AS p "
    "  FROM pipeline_events "
    " WHERE event_type = 'profile_refresh' "
    " ORDER BY created_at DESC LIMIT 8"
).fetchall()
for r in rows:
    p = (r["p"] or "").replace("\n", " ")
    print(f"  {r['created_at']}  {r['status']:<12s}  | {p}")
