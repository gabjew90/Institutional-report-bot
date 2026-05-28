"""Drill-down QC: caller-column values, null-ticker row content,
viewing actions, DeeP FRieD's close-only cluster, Abe/abugs-bunny dup."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 78 + f"\n  {s}\n" + "=" * 78)


hr("A. caller column distribution (14d)")
rows = conn.execute(
    """SELECT COALESCE(caller,'(null)') AS c, tracking_mode, COUNT(*) AS n
       FROM analyst_trades WHERE posted_at > datetime('now','-14 days')
       GROUP BY c, tracking_mode ORDER BY n DESC"""
).fetchall()
for r in rows:
    print(f"  caller={r['c']:<20s}  mode={r['tracking_mode'] or '?':<8s}  {r['n']}")

hr("B. 10 null-ticker rows — OCR? attachment? text-only chat?")
rows = conn.execute(
    """SELECT at.id, at.posted_at, at.tracking_mode, at.author, at.discord_attachment_id,
              substr(COALESCE(at.caption,''),1,100) AS cap,
              CASE WHEN cm.image_ocr_text IS NULL THEN 0 ELSE 1 END AS has_ocr,
              length(cm.image_ocr_text) AS ocr_len,
              cm.channel_name
         FROM analyst_trades at
         LEFT JOIN chat_messages cm ON cm.discord_message_id = at.discord_message_id
        WHERE at.posted_at > datetime('now','-14 days')
          AND (at.ticker IS NULL OR at.ticker = '')
        ORDER BY at.id DESC LIMIT 10"""
).fetchall()
for r in rows:
    cap = (r['cap'] or '').replace('\n', ' ')
    print(
        f"  #{r['id']:>4d}  {r['posted_at'][:16]}  {r['tracking_mode'] or '?':<6s}  "
        f"{(r['author'] or '?')[:14]:<14s} ch={(r['channel_name'] or '?')[:18]:<18s} "
        f"ocr={r['has_ocr']}({r['ocr_len'] or 0:>4d}) att={r['discord_attachment_id']}  | {cap}"
    )

hr("C. action='viewing' samples (5)")
rows = conn.execute(
    """SELECT id, posted_at, author, ticker,
              substr(COALESCE(caption,''),1,80) AS cap
         FROM analyst_trades WHERE action = 'viewing'
         ORDER BY id DESC LIMIT 5"""
).fetchall()
for r in rows:
    print(f"  #{r['id']}  {r['posted_at'][:16]}  {(r['author'] or '?')[:15]:<15s} t={r['ticker'] or '-'}   | {r['cap']}")

hr("D. DeeP FRieD DΞFi — 14d full row list (why 5 losses?)")
rows = conn.execute(
    """SELECT id, posted_at, action, ticker, contract_type, strike, expiry, gain_pct,
              substr(COALESCE(caption,''),1,60) AS cap
         FROM analyst_trades WHERE author = 'DeeP FRieD DΞFi'
           AND posted_at > datetime('now','-14 days')
         ORDER BY id DESC"""
).fetchall()
for r in rows:
    print(
        f"  #{r['id']} {r['posted_at'][:16]} {(r['action'] or '?'):>6s} "
        f"{r['ticker'] or '-':<6s} {r['contract_type'] or '-':<1s} "
        f"K={str(r['strike'] or '-'):<6s} exp={r['expiry'] or '-':<10s} gain={r['gain_pct']}  | {r['cap']}"
    )

hr("E. Abe / abugs bunny — confirm same author_id")
rows = conn.execute(
    """SELECT author, author_id, tracking_mode, caller, COUNT(*) AS n
         FROM analyst_trades
        WHERE author_id = 1192771108332650496
          AND posted_at > datetime('now','-14 days')
        GROUP BY author, author_id, tracking_mode, caller
        ORDER BY n DESC"""
).fetchall()
for r in rows:
    print(f"  author={r['author']!r:<20s} mode={r['tracking_mode']:<8s} caller={r['caller']}  n={r['n']}")

hr("F. eager-OCR channel names — actual production values")
rows = conn.execute(
    """SELECT channel_name, COUNT(*) AS n,
              SUM(CASE WHEN image_ocr_text IS NOT NULL THEN 1 ELSE 0 END) AS ocr_n
         FROM chat_messages
        WHERE posted_at > datetime('now','-14 days')
        GROUP BY channel_name
        HAVING ocr_n > 0
        ORDER BY ocr_n DESC LIMIT 20"""
).fetchall()
print(f"  {'channel':<30s} {'msgs':>6s} {'with-OCR':>9s}")
for r in rows:
    print(f"  {r['channel_name']:<30s} {r['n']:>6d} {r['ocr_n']:>9d}")
