"""Show L2's full chat_messages row including channel_name."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
c = sqlite3.connect(DB_PATH)
c.row_factory = sqlite3.Row
r = c.execute(
    "SELECT * FROM chat_messages WHERE discord_message_id = ?",
    (1509612248593727539,),
).fetchone()
if r:
    for k in r.keys():
        print(f"  {k}: {r[k]!r}")
else:
    print("NOT IN chat_messages")

print("\n--- Other BK messages around 17:40 on 5/28 across ANY channel ---")
rows = c.execute(
    """SELECT discord_message_id, channel_name, posted_at, content, has_attachments
         FROM chat_messages
        WHERE author_username = 'bankerkyle'
          AND posted_at BETWEEN '2026-05-28T17:35' AND '2026-05-28T17:45'
        ORDER BY posted_at ASC"""
).fetchall()
for x in rows:
    co = (x["content"] or "")[:60].replace("\n", " ")
    print(f"  {x['posted_at'][:19]}  ch={x['channel_name']!r:<30s}  has_att={x['has_attachments']}  content={co!r}")
