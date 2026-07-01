"""QC the analyst_log pipeline: coverage, extraction quality, mode mix,
recent activity, point totals. Run via:
    railway ssh '/opt/venv/bin/python scripts/qc_analyst_log.py'
"""
import sqlite3
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(label: str):
    print()
    print("=" * 78)
    print(f"  {label}")
    print("=" * 78)


# --- 1. Overall row counts -------------------------------------------------
hr("1. analyst_trades — total rows + mode breakdown")
total = conn.execute("SELECT COUNT(*) AS n FROM analyst_trades").fetchone()["n"]
print(f"Total rows: {total}")

mode_rows = conn.execute(
    "SELECT COALESCE(tracking_mode,'(null)') AS m, COUNT(*) AS n "
    "FROM analyst_trades GROUP BY m ORDER BY n DESC"
).fetchall()
for r in mode_rows:
    print(f"  mode={r['m']:>10s}  n={r['n']}")

# --- 2. Last 24h / 7d / 14d activity --------------------------------------
hr("2. Recent activity windows")
for label, days in [("last 24h", 1), ("last 7d", 7), ("last 14d", 14)]:
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM analyst_trades "
        "WHERE posted_at > datetime('now', ?)",
        (f"-{days} days",),
    ).fetchone()["n"]
    by_mode = conn.execute(
        "SELECT COALESCE(tracking_mode,'(null)') AS m, COUNT(*) AS n "
        "FROM analyst_trades WHERE posted_at > datetime('now', ?) "
        "GROUP BY m ORDER BY n DESC",
        (f"-{days} days",),
    ).fetchall()
    print(f"\n{label}: {n} rows total")
    for r in by_mode:
        print(f"    {r['m']:>10s}  {r['n']}")

# --- 3. Top members by row count (14d window) ------------------------------
hr("3. Top authors (last 14d) — caller and member combined")
rows = conn.execute(
    """
    SELECT author, author_id, tracking_mode, COUNT(*) AS n,
           SUM(CASE WHEN action='close' THEN 1 ELSE 0 END) AS closes,
           SUM(CASE WHEN action='open' THEN 1 ELSE 0 END) AS opens,
           SUM(CASE WHEN action='trim' THEN 1 ELSE 0 END) AS trims
      FROM analyst_trades
     WHERE posted_at > datetime('now','-14 days')
     GROUP BY author, author_id, tracking_mode
     ORDER BY n DESC
     LIMIT 20
    """
).fetchall()
print(f"{'author':<25s} {'mode':<8s} {'rows':>5s} {'open':>5s} {'trim':>5s} {'close':>6s}  author_id")
for r in rows:
    print(
        f"  {(r['author'] or '?')[:22]:<22s}  {str(r['tracking_mode'])[:6]:<6s}  "
        f"{r['n']:>4d}  {r['opens'] or 0:>4d}  {r['trims'] or 0:>4d}  "
        f"{r['closes'] or 0:>5d}   {r['author_id']}"
    )

# --- 4. Extraction quality — null/blank tickers, weird strikes ------------
hr("4. Extraction quality red flags (14d)")
null_ticker = conn.execute(
    "SELECT COUNT(*) AS n FROM analyst_trades "
    "WHERE posted_at > datetime('now','-14 days') "
    "AND (ticker IS NULL OR ticker='')"
).fetchone()["n"]
null_action = conn.execute(
    "SELECT COUNT(*) AS n FROM analyst_trades "
    "WHERE posted_at > datetime('now','-14 days') "
    "AND (action IS NULL OR action='')"
).fetchone()["n"]
print(f"null ticker:  {null_ticker}")
print(f"null action:  {null_action}")

# Suspicious tickers — non-equity strings, too long, lowercase
suspicious = conn.execute(
    """
    SELECT ticker, COUNT(*) AS n FROM analyst_trades
     WHERE posted_at > datetime('now','-14 days')
       AND ticker IS NOT NULL
       AND (length(ticker) > 6 OR ticker GLOB '*[a-z]*' OR ticker GLOB '*[^A-Z0-9./-]*')
     GROUP BY ticker ORDER BY n DESC LIMIT 15
    """
).fetchall()
if suspicious:
    print("\nsuspicious ticker strings:")
    for r in suspicious:
        print(f"  {r['ticker']!r:>20s}  x{r['n']}")
else:
    print("\nsuspicious tickers: none")

# Action values
actions = conn.execute(
    "SELECT COALESCE(action,'(null)') AS a, COUNT(*) AS n FROM analyst_trades "
    "WHERE posted_at > datetime('now','-14 days') GROUP BY a ORDER BY n DESC"
).fetchall()
print("\naction values (14d):")
for r in actions:
    print(f"  {r['a']:>12s}  {r['n']}")

# --- 5. Recent 30 rows for eyeball check ----------------------------------
hr("5. Most recent 30 rows — eyeball QC")
rows = conn.execute(
    """
    SELECT id, posted_at, tracking_mode, author, ticker, action,
           contract_type, strike, expiry, gain_pct, price,
           substr(COALESCE(caption,''),1,80) AS cap
      FROM analyst_trades
     ORDER BY id DESC LIMIT 30
    """
).fetchall()
for r in rows:
    cap = (r["cap"] or "").replace("\n", " ")
    print(
        f"#{r['id']:>5d}  {r['posted_at'][:16]}  {(r['tracking_mode'] or '?'):<6s}  "
        f"{(r['author'] or '?')[:15]:<15s}  "
        f"{(r['action'] or '?')[:5]:<5s} "
        f"{(r['ticker'] or '?'):<6s} "
        f"{(r['contract_type'] or '-')[:1]:<1s} "
        f"{str(r['strike'] or '-')[:6]:<6s} "
        f"exp={(r['expiry'] or '-')[:10]:<10s} "
        f"g={str(r['gain_pct'] or '-')[:6]:<6s} "
        f"| {cap}"
    )

# --- 6. Mode coverage — how much eager-OCR traffic became trades ---------
hr("6. Member-mode coverage: chat_messages → analyst_trades ratio")
# Eager-OCR alert channels (read from settings would be nicer but we hard-code
# the ones we know exist; this is a sanity check, not a config-truth view)
candidate_channels = [
    "member-alerts", "gain-loss-porn", "kloh-alerts", "zhawk-thawghts",
    "spot-bag", "0dte-lotto", "crypto-alerts",
]
for ch in candidate_channels:
    chat_n = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_messages "
        "WHERE channel_name = ? AND posted_at > datetime('now','-14 days')",
        (ch,),
    ).fetchone()["n"]
    chat_ocr = conn.execute(
        "SELECT COUNT(*) AS n FROM chat_messages "
        "WHERE channel_name = ? AND posted_at > datetime('now','-14 days') "
        "AND image_ocr_text IS NOT NULL",
        (ch,),
    ).fetchone()["n"]
    # Joined: how many of those msg_ids produced an analyst_trades row?
    trades_n = conn.execute(
        """
        SELECT COUNT(*) AS n FROM analyst_trades at
         JOIN chat_messages cm ON cm.discord_message_id = at.discord_message_id
        WHERE cm.channel_name = ?
          AND at.posted_at > datetime('now','-14 days')
        """,
        (ch,),
    ).fetchone()["n"]
    print(
        f"  {ch:<20s}  msgs={chat_n:>5d}  with-OCR={chat_ocr:>4d}  "
        f"→ trades={trades_n:>3d}  ratio={(trades_n / max(chat_ocr, 1)) * 100:>5.1f}%"
    )

# --- 7. Caller-mode sanity — BK/Abe are still firing? ---------------------
hr("7. Caller-mode coverage — BK/Abe last 14d")
for caller in ["BK", "Abe", "kloh", "zhawk"]:
    rows = conn.execute(
        """
        SELECT COUNT(*) AS n, MAX(posted_at) AS latest
          FROM analyst_trades
         WHERE caller = ?
           AND tracking_mode = 'caller'
           AND posted_at > datetime('now','-14 days')
        """,
        (caller,),
    ).fetchone()
    print(f"  {caller:<8s}  n={rows['n']:>3d}  last={rows['latest']}")

# --- 8. Points ledger — top 10 by 14d total -------------------------------
hr("8. Top 10 by 14d receipt points (db.compute_member_points)")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db as _db  # noqa: E402

# Get distinct authors active in 14d
authors = conn.execute(
    """
    SELECT DISTINCT author_id, author
      FROM analyst_trades
     WHERE posted_at > datetime('now','-14 days')
       AND author_id IS NOT NULL
    """
).fetchall()
print(f"distinct active authors (14d): {len(authors)}")

scored = []
for a in authors:
    try:
        L = _db.compute_member_points(int(a["author_id"]), days=21)
        if L.get("points", 0) > 0:
            scored.append((a["author"], int(a["author_id"]), L))
    except Exception as e:
        print(f"  ERR uid={a['author_id']}: {e}")

scored.sort(key=lambda t: -t[2]["points"])
print(f"\n{'author':<20s} {'pts':>4s}  W   L   G   PW  PL  ENT  CLS  detail")
for name, uid, L in scored[:10]:
    print(
        f"  {(name or '?')[:18]:<18s}  {L['points']:>4d}  "
        f"{L['entries_won']:>2d}  {L['entries_lost']:>2d}  {L['entries_ghosted']:>2d}  "
        f"{L['screenshot_wins']:>2d}  {L['screenshot_losses']:>2d}  "
        f"{L['entries_pending']:>3d}  {L.get('entries_closed_count','?'):>3}"
    )
