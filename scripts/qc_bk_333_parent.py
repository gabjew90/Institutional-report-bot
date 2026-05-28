"""Find BK msg 333's parent message in chat_messages + dump gemini_json."""
import sqlite3, os, json
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


# We learned the parent ID from chat_messages: 1509612264632746164
PARENT_ID = 1509612264632746164

hr("A. gemini_json from analyst_trades #333 — what did the model see/decide?")
r = conn.execute("SELECT gemini_json FROM analyst_trades WHERE id = 333").fetchone()
if r and r["gemini_json"]:
    try:
        j = json.loads(r["gemini_json"])
        for k, v in j.items():
            print(f"  {k}: {v!r}")
    except Exception as e:
        print(f"  parse error: {e}")
        print(f"  raw: {r['gemini_json'][:500]}")
else:
    print("  gemini_json is empty/null on #333")

hr(f"B. parent chat_messages row for reply_parent_id={PARENT_ID}")
p = conn.execute(
    "SELECT * FROM chat_messages WHERE discord_message_id = ?", (PARENT_ID,)
).fetchone()
if p is None:
    print(f"  Parent {PARENT_ID} NOT in chat_messages.")
    print("  (Either older than chat_ingestion start, posted elsewhere, or never ingested.)")
else:
    for k in p.keys():
        val = p[k]
        if isinstance(val, str) and len(val) > 400:
            val = val[:400] + "…"
        print(f"  {k}: {val!r}")

hr("C. Was the parent ALSO logged in analyst_trades?")
ap = conn.execute(
    "SELECT * FROM analyst_trades WHERE discord_message_id = ?", (PARENT_ID,)
).fetchone()
if ap is None:
    print(f"  Parent {PARENT_ID} is NOT in analyst_trades.")
else:
    print(f"  Parent IS in analyst_trades:")
    for k in ap.keys():
        val = ap[k]
        if k == "gemini_json" and val:
            try:
                j = json.loads(val)
                print(f"  {k}:")
                for jk, jv in j.items():
                    print(f"      {jk}: {jv!r}")
                continue
            except Exception:
                pass
        if isinstance(val, str) and len(val) > 200:
            val = val[:200] + "…"
        print(f"    {k}: {val!r}")

hr("D. BK's earlier kyle-alerts messages with open @0.80 (40% above = 1.12)")
# +40% gain on close @1.12 => entry @0.80. Find BK opens in last 7d that
# could plausibly match.
rows = conn.execute(
    """SELECT discord_message_id, posted_at, content,
              substr(COALESCE(image_ocr_text,''),1,100) AS ocr
         FROM chat_messages
        WHERE channel_name LIKE '%kyle-alerts%'
          AND author_username = 'bankerkyle'
          AND posted_at > datetime('now','-3 days')
          AND posted_at < '2026-05-28T19:41'
        ORDER BY posted_at DESC
        LIMIT 30"""
).fetchall()
print(f"\nLast 30 BK kyle-alerts posts before 19:41 on 5/28:")
for r in rows:
    c = (r["content"] or "").replace("\n", " ")
    ocr = (r["ocr"] or "").replace("\n", " ")
    print(f"  {r['posted_at'][:16]}  msg={r['discord_message_id']}  content={c[:80]!r}  ocr={ocr[:60]!r}")
