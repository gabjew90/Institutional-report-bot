"""Current leaderboard + 14-day points ledger + projected new score
once profiles refresh with the activity-ramp fix."""
import sqlite3, os, sys
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import db as _db  # noqa

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def hr(s):
    print("\n" + "=" * 88 + f"\n  {s}\n" + "=" * 88)


ACTIVITY_FULL_CREDIT_MSGS = 500


def project(chatter, msgs, receipts):
    """Apply the new formula."""
    clipped = max(0, min(50, chatter))
    mult = min(1.0, msgs / ACTIVITY_FULL_CREDIT_MSGS)
    scaled = round(clipped * mult)
    return max(0, min(100, scaled + receipts))


hr("Current ranks + 14-day ledger + projection under activity ramp")

# Pull all ranked traders with msg_count
rows = conn.execute(
    """SELECT user_id, display_name, username, trader_score,
              message_count_at_update AS msgs
         FROM user_profiles
        WHERE trader_score IS NOT NULL
        ORDER BY trader_score DESC, user_id ASC"""
).fetchall()

# Compute live receipts for each user and project new score.
print(f"{'#':>3s} {'name':<22s} {'now':>4s} {'msgs':>5s} | "
      f"{'rec':>4s} {'W/L/G':>6s} {'PW/PL':>5s} {'mult':>5s} {'new':>4s}  {'Δ':>4s}")
print("-" * 88)
projected = []
for i, r in enumerate(rows):
    uid = int(r["user_id"])
    try:
        L = _db.compute_member_points(uid, days=21)
    except Exception:
        L = {"points": 0, "entries_won": 0, "entries_lost": 0,
             "entries_ghosted": 0, "screenshot_wins": 0,
             "screenshot_losses": 0}
    receipts = int(L.get("points") or 0)
    msgs = r["msgs"] or 0
    prev = r["trader_score"]
    # Inferred chatter is current - receipts (rough — ignores any
    # earlier capping). For ranking the projection it doesn't matter
    # if we under-estimate; we'd still keep relative order.
    inferred_chatter = max(0, prev - receipts)
    new = project(inferred_chatter, msgs, receipts)
    mult = min(1.0, msgs / ACTIVITY_FULL_CREDIT_MSGS)
    wlg = (
        f"{L['entries_won']}/{L['entries_lost']}/{L['entries_ghosted']}"
    )
    pwpl = f"{L['screenshot_wins']}/{L['screenshot_losses']}"
    projected.append({
        "name": r["display_name"] or r["username"] or "?",
        "prev": prev, "new": new, "msgs": msgs, "receipts": receipts,
        "wlg": wlg, "pwpl": pwpl, "mult": mult,
    })
    name = (r["display_name"] or r["username"] or "?")[:20]
    delta = new - prev
    delta_str = f"{delta:+d}" if delta else "  ·"
    print(f"#{i+1:<3d} {name:<22s} {prev:>4d}  {msgs:>5d} | "
          f"{receipts:>4d}  {wlg:>6s}  {pwpl:>5s}  {mult:>4.2f}  {new:>4d}  {delta_str:>4s}")

hr("Sorted by PROJECTED new score (post-refresh order)")
projected.sort(key=lambda p: -p["new"])
print(f"{'#':>3s} {'name':<22s} {'new':>4s} {'prev':>4s} {'msgs':>5s} {'rec':>4s}")
print("-" * 60)
for i, p in enumerate(projected[:25]):
    delta = p["new"] - p["prev"]
    delta_str = f" ({delta:+d})" if delta else ""
    print(f"#{i+1:<3d} {p['name'][:20]:<22s} {p['new']:>4d}  {p['prev']:>4d}  "
          f"{p['msgs']:>5d}  {p['receipts']:>4d}{delta_str}")
