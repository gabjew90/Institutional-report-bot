"""Why is Tied still #4? Show his profile state, any pipeline_events
referencing him, and the last 5 refresh events overall."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 88 + f"\n  {s}\n" + "=" * 88)


hr("A. Tied's user_profiles row right now")
r = conn.execute(
    """SELECT user_id, username, display_name, trader_score,
              racial_humor_score, message_count_at_update,
              length(profile_text) AS prof_len,
              updated_at, last_seen_message_at
         FROM user_profiles
        WHERE LOWER(display_name) LIKE '%tied%'
           OR LOWER(username) LIKE '%tied%'"""
).fetchall()
for x in r:
    print(f"  user_id:                  {x['user_id']}")
    print(f"  username:                 {x['username']}")
    print(f"  display_name:             {x['display_name']}")
    print(f"  trader_score:             {x['trader_score']}")
    print(f"  racial_humor_score:       {x['racial_humor_score']}")
    print(f"  message_count_at_update:  {x['message_count_at_update']}")
    print(f"  profile_text length:      {x['prof_len']} chars")
    print(f"  last_updated:             {x['updated_at']}")
    print(f"  last_seen_message_at:     {x['last_seen_message_at']}")

if not r:
    print("  NO PROFILE FOUND for 'tied'")
    raise SystemExit(0)

uid = int(r[0]["user_id"])

hr(f"B. pipeline_events referencing user_id={uid} (last 50, newest first)")
try:
    # First check the actual schema:
    cols = [c["name"] for c in conn.execute("PRAGMA table_info(pipeline_events)").fetchall()]
    print(f"  pipeline_events columns: {cols}")
    sub_col = "event_subtype" if "event_subtype" in cols else "sub_type"
    rows = conn.execute(
        f"""SELECT created_at, event_type, {sub_col} AS sub_type,
                  substr(payload, 1, 250) AS data_preview
             FROM pipeline_events
            WHERE payload LIKE ?
            ORDER BY created_at DESC
            LIMIT 50""",
        (f"%{uid}%",),
    ).fetchall()
    if not rows:
        print("  No events referencing this user_id.")
    for x in rows:
        d = (x["data_preview"] or "").replace("\n", " ")
        print(f"  {x['created_at']}  {x['event_type']}/{x['sub_type']}  | {d}")
except Exception as e:
    print(f"  Query error: {e}")

hr("C. Last 10 profile_user_failure events overall (any user)")
try:
    rows = conn.execute(
        f"""SELECT created_at, {sub_col} AS sub_type, substr(payload, 1, 280) AS d
             FROM pipeline_events
            WHERE event_type = 'profile_user_failure'
            ORDER BY created_at DESC
            LIMIT 20"""
    ).fetchall()
    for x in rows:
        d = (x["d"] or "").replace("\n", " ")
        print(f"  {x['created_at']}  {x['sub_type']}  | {d}")
except Exception as e:
    print(f"  Query error: {e}")

hr("D. How many messages does Tied have in chat_messages right now?")
# Match by user_id
rows = conn.execute(
    "SELECT COUNT(*) AS n, MIN(posted_at) AS earliest, MAX(posted_at) AS latest "
    "FROM chat_messages WHERE author_id = ?",
    (uid,),
).fetchone()
print(f"  chat_messages count:  {rows['n']}")
print(f"  earliest:             {rows['earliest']}")
print(f"  latest:               {rows['latest']}")

# Also check within profile_window_days (likely 30)
from datetime import datetime, timedelta
cutoff_30 = (datetime.utcnow() - timedelta(days=30)).isoformat()
rows30 = conn.execute(
    "SELECT COUNT(*) AS n FROM chat_messages "
    "WHERE author_id = ? AND posted_at > ?",
    (uid, cutoff_30),
).fetchone()
print(f"  in last 30d:          {rows30['n']}")
