"""How many messages does KsFs actually have in chat_messages?
The leaderboard shows msgs=39 (cached from May 20 refresh) but
the actual current count drives whether he gets processed."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row

r = c.execute(
    "SELECT user_id, username, display_name, trader_score, "
    "       message_count_at_update, updated_at "
    "  FROM user_profiles WHERE LOWER(display_name) LIKE '%ksfs%'"
).fetchone()
print(f"KsFs user_id: {r['user_id']}")
print(f"  username:                {r['username']}")
print(f"  display_name:            {r['display_name']}")
print(f"  trader_score:            {r['trader_score']}")
print(f"  message_count_at_update: {r['message_count_at_update']}")
print(f"  updated_at:              {r['updated_at']}")

uid = r["user_id"]

tot = c.execute(
    "SELECT COUNT(*) AS n, MIN(posted_at) AS earl, MAX(posted_at) AS lat "
    "  FROM chat_messages WHERE author_id = ?",
    (uid,),
).fetchone()
print(f"\nchat_messages by author_id={uid}:")
print(f"  total:    {tot['n']}")
print(f"  earliest: {tot['earl']}")
print(f"  latest:   {tot['lat']}")

n30 = c.execute(
    "SELECT COUNT(*) AS n FROM chat_messages "
    "WHERE author_id = ? AND posted_at > datetime('now','-30 days')",
    (uid,),
).fetchone()
n14 = c.execute(
    "SELECT COUNT(*) AS n FROM chat_messages "
    "WHERE author_id = ? AND posted_at > datetime('now','-14 days')",
    (uid,),
).fetchone()
n7 = c.execute(
    "SELECT COUNT(*) AS n FROM chat_messages "
    "WHERE author_id = ? AND posted_at > datetime('now','-7 days')",
    (uid,),
).fetchone()
print(f"  last 30d: {n30['n']}")
print(f"  last 14d: {n14['n']}")
print(f"  last 7d:  {n7['n']}")
print()

# What's the chat_messages retention?
print("=== chat_retention check ===")
ret = c.execute("SELECT MIN(posted_at) FROM chat_messages").fetchone()
print(f"  earliest chat_messages overall: {ret[0]}")
print()

# Check the cron purge config — chat_retention_days
# (settings.chat_retention_days — looked at earlier, default unset = no purge)
