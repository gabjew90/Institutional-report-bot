"""Walk back from BK #333 to see how far up the reply chain has actual
context. Also dump raw column values so we can tell embed-image vs
empty-message vs attachment-but-no-OCR."""
import sqlite3, os, json
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


def dump(row, label):
    print(f"\n--- {label} ---")
    if row is None:
        print("  (not in chat_messages)")
        return
    keys_to_show = [
        "discord_message_id", "posted_at", "author_username", "content",
        "has_attachments", "attachment_urls", "embed_texts",
        "reply_parent_id", "image_ocr_text", "image_ocr_status",
    ]
    for k in keys_to_show:
        v = row[k]
        if isinstance(v, str) and len(v) > 300:
            v = v[:300] + "…"
        print(f"  {k}: {v!r}")


# Start at #333's chat_messages row
hr("Reply-chain walk starting from BK msg #333")
start_msg = conn.execute(
    "SELECT * FROM chat_messages WHERE discord_message_id = ?",
    (1509642875028246730,),
).fetchone()
dump(start_msg, "L0  BK 'Sold @1.12, +40%' (msg 333 in analyst_trades)")

# Walk parents
current = start_msg
hop = 1
while current and hop <= 5:
    parent_id = current["reply_parent_id"]
    if not parent_id:
        print(f"\n(hop {hop}: no parent — chain ends)")
        break
    parent = conn.execute(
        "SELECT * FROM chat_messages WHERE discord_message_id = ?",
        (parent_id,),
    ).fetchone()
    dump(parent, f"L{hop}  parent id={parent_id}")
    if parent is None:
        break
    # Also: any analyst_trades row for this parent?
    at = conn.execute(
        "SELECT id, action, ticker, contract_type, strike, expiry, gain_pct, price, "
        "       substr(COALESCE(caption,''),1,80) AS cap, is_trade "
        "FROM analyst_trades WHERE discord_message_id = ?",
        (parent_id,),
    ).fetchone()
    if at:
        print(f"  analyst_trades #{at['id']}: is_trade={at['is_trade']} "
              f"action={at['action']!r} ticker={at['ticker']!r} "
              f"strike={at['strike']} exp={at['expiry']} cap={at['cap']!r}")
    else:
        print("  analyst_trades: no row")
    current = parent
    hop += 1
