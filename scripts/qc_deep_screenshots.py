"""Look at DeeP FRieD DΞFi's close-only screenshot rows in detail —
what did Gemini extract, what's in the caption, what's in OCR, and
what channel was each posted in. Goal: figure out why all 11 are
scoring as losses when the user says they're wins."""
import sqlite3, os, json
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 88 + f"\n  {s}\n" + "=" * 88)


hr("All DeeP rows in last 14d")
rows = conn.execute(
    """SELECT at.id, at.posted_at, at.action, at.ticker,
              at.contract_type, at.strike, at.expiry,
              at.gain_pct, at.price, at.tracking_mode,
              at.image_url, at.caption,
              at.gemini_json,
              cm.channel_name, cm.image_ocr_text
         FROM analyst_trades at
    LEFT JOIN chat_messages cm
           ON cm.discord_message_id = at.discord_message_id
        WHERE at.author = 'DeeP FRieD DΞFi'
          AND at.posted_at > datetime('now','-14 days')
          AND at.is_trade = 1
        ORDER BY at.posted_at DESC"""
).fetchall()

print(f"Total trade rows: {len(rows)}\n")
for r in rows:
    cap = (r["caption"] or "").replace("\n", " ")[:60]
    ch = (r["channel_name"] or "?")[:25]
    print(
        f"#{r['id']}  {r['posted_at'][:16]}  ch={ch:<25s}  "
        f"{(r['action'] or '?')[:5]:<5s} {(r['ticker'] or '-'):<6s} "
        f"K={str(r['strike'] or '-'):<7s} gain={str(r['gain_pct'] or '-'):<6s} "
        f"price={r['price']!s:<6s} | {cap}"
    )

hr("Gemini JSON for first 3 close-only rows (what the model actually decided)")
close_rows = [r for r in rows if (r["action"] or "").lower() == "close"][:3]
for r in close_rows:
    print(f"\n--- #{r['id']} ({r['ticker']} {r['strike']}c exp={r['expiry']}) ---")
    print(f"  image_url: {(r['image_url'] or '<none>')[:80]}")
    print(f"  caption:   {r['caption']!r}")
    print(f"  image_ocr_text (from chat_messages):")
    ocr = (r["image_ocr_text"] or "<none>").replace("\n", "  |  ")
    print(f"    {ocr[:400]}")
    print(f"  gemini_json:")
    try:
        j = json.loads(r["gemini_json"] or "{}")
        for k, v in j.items():
            vv = repr(v)[:140]
            print(f"    {k}: {vv}")
    except Exception as e:
        print(f"    (parse error: {e})")
        print(f"    raw: {(r['gemini_json'] or '')[:300]}")
