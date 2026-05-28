"""Diagnose why L2 ('CRCL 110c @0.8 SLAMMMMM' from BK at 17:40:07)
is missing from analyst_trades despite being a clean text entry."""
import asyncio
import sqlite3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from config import settings  # noqa: E402


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


L2_MSG_ID = 1509612248593727539
L2_CHANNEL = "💅🏾-kyle-alerts-💅🏾"
L2_CAPTION = "CRCL 110c @0.8 SLAMMMMM"


hr("A. settings.caller_by_channel — does production channel name match config?")
matched = settings.caller_by_channel(L2_CHANNEL)
print(f"  channel: {L2_CHANNEL!r}")
print(f"  caller_by_channel returns: {matched!r}")

hr("B. All analyst caller channel names — what's actually configured?")
for c in settings.resolve_analyst_callers():
    print(f"  channel={c.get('channel')!r:<40s} username={c.get('username')!r:<15s} name={c.get('name')!r}")

hr("C. eager_ocr_channels — is the channel in this set too?")
egr = settings.resolve_chat_eager_ocr_channels()
print(f"  resolve_chat_eager_ocr_channels: {sorted(egr)}")
print(f"  L2 channel in set: {L2_CHANNEL in egr}")

hr("D. analyst_trade_exists check — could be a dedup ghost row?")
print(f"  exists(msg={L2_MSG_ID}, att=0): {db.analyst_trade_exists(L2_MSG_ID, 0)}")

hr("E. ALL rows in analyst_trades referencing this msg_id (any attachment_id)")
conn = db.get_connection()
rows = conn.execute(
    "SELECT id, discord_attachment_id, is_trade, tracking_mode, caller, "
    "       ticker, action, posted_at "
    "FROM analyst_trades WHERE discord_message_id = ?",
    (L2_MSG_ID,),
).fetchall()
if not rows:
    print("  NO ROWS — watcher never wrote anything for this msg.")
else:
    for r in rows:
        print(f"  {dict(r)}")

hr("F. Test the extractor against L2's caption — does Gemini say is_trade=True?")
from analyst_log.ocr import extract_trade_from_caption


async def _test():
    extracted = await extract_trade_from_caption(
        L2_CAPTION, parent_caption=None, caller_name="BK",
    )
    print(f"  extracted: {extracted!r}")
    return extracted


extracted = asyncio.run(_test())

hr("G. How many other BK text entries in kyle-alerts (last 7d) are also MISSING?")
# Find chat_messages from BK in kyle-alerts with non-empty content and
# no analyst_trades row at all.
rows = conn.execute(
    """
    SELECT cm.discord_message_id, cm.posted_at, cm.content
      FROM chat_messages cm
     WHERE cm.channel_name = ?
       AND cm.author_username = 'bankerkyle'
       AND cm.content IS NOT NULL
       AND length(cm.content) > 0
       AND cm.posted_at > datetime('now','-7 days')
       AND NOT EXISTS (
           SELECT 1 FROM analyst_trades at
            WHERE at.discord_message_id = cm.discord_message_id
       )
     ORDER BY cm.posted_at DESC
     LIMIT 30
    """,
    (L2_CHANNEL,),
).fetchall()
print(f"  Found {len(rows)} BK kyle-alerts messages with content but no analyst_trades row (last 7d):")
for r in rows:
    c = (r["content"] or "").replace("\n", " ")
    print(f"    {r['posted_at'][:16]}  msg={r['discord_message_id']}  content={c[:80]!r}")
