"""Drill into analyst_trades #333 — BK's 'Sold @1.12, +40%' with no
ticker. Was it actually a reply? What did parent_caption see?"""
import sqlite3, os, json
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


hr("A. analyst_trades #333 full row")
r = conn.execute("SELECT * FROM analyst_trades WHERE id = 333").fetchone()
if r is None:
    print("Row 333 not found.")
    raise SystemExit(0)
for k in r.keys():
    val = r[k]
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
    print(f"  {k}: {val!r}")

msg_id = r["discord_message_id"]
posted_at = r["posted_at"]

hr(f"B. chat_messages row for the same msg_id ({msg_id})")
cm = conn.execute(
    "SELECT * FROM chat_messages WHERE discord_message_id = ?", (msg_id,)
).fetchone()
if cm is None:
    print("Not found in chat_messages.")
else:
    for k in cm.keys():
        val = cm[k]
        if isinstance(val, str) and len(val) > 300:
            val = val[:300] + "…"
        print(f"  {k}: {val!r}")

hr("C. Reply-chain check — what fields does chat_messages have?")
cols = conn.execute("PRAGMA table_info(chat_messages)").fetchall()
for c in cols:
    print(f"  {c['name']}  {c['type']}")

hr("D. BK's nearby messages in #kyle-alerts on 2026-05-28 (±30 min around posted_at)")
# posted_at = '2026-05-28T19:41' — find rows in a ±30-min window
from datetime import datetime, timedelta
try:
    pa = datetime.fromisoformat(posted_at.replace("Z", ""))
except Exception:
    pa = None
if pa:
    lo = (pa - timedelta(minutes=45)).isoformat()
    hi = (pa + timedelta(minutes=5)).isoformat()
    rows = conn.execute(
        """SELECT discord_message_id, posted_at, author_username, content,
                  substr(COALESCE(image_ocr_text,''),1,120) AS ocr
             FROM chat_messages
            WHERE channel_name LIKE '%kyle-alerts%'
              AND posted_at BETWEEN ? AND ?
            ORDER BY posted_at ASC""",
        (lo, hi),
    ).fetchall()
    print(f"\nBK's recent kyle-alerts posts in window [{lo[:16]} … {hi[:16]}]:")
    for x in rows:
        c = (x["content"] or "").replace("\n", " ")
        ocr = (x["ocr"] or "").replace("\n", " ")
        tag = " *** THE TARGET ***" if x["discord_message_id"] == msg_id else ""
        print(f"  {x['posted_at'][:16]}  msg={x['discord_message_id']}  uname={x['author_username']:<15s}  "
              f"content={c[:80]!r}  ocr={ocr[:60]!r}{tag}")

hr("E. Anything in BK's analyst_trades around the same time")
if pa:
    lo = (pa - timedelta(minutes=45)).isoformat()
    hi = (pa + timedelta(minutes=5)).isoformat()
    rows = conn.execute(
        """SELECT id, posted_at, action, ticker, contract_type, strike, expiry,
                  gain_pct, price, substr(COALESCE(caption,''),1,80) AS cap
             FROM analyst_trades
            WHERE author = 'BK' OR caller = 'bankerkyle'
              AND posted_at BETWEEN ? AND ?
            ORDER BY posted_at ASC""",
        (lo, hi),
    ).fetchall()
    for x in rows:
        tag = " *** THE TARGET ***" if x["id"] == 333 else ""
        cap = (x["cap"] or "").replace("\n", " ")
        print(f"  #{x['id']}  {x['posted_at'][:16]}  {x['action'] or '?':<6s} "
              f"{x['ticker'] or '-':<6s} {x['contract_type'] or '-':<5s} "
              f"K={x['strike']} exp={x['expiry']} gain={x['gain_pct']} price={x['price']} | {cap}{tag}")
