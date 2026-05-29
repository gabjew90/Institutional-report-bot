"""Compare kloh vs Grand Nagus Yeezy trader rationale + show racism scores."""
import sqlite3, os
DB_PATH = os.environ.get("DB_PATH", "/data/reports.db")
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row


def show(username_pat):
    r = conn.execute(
        """SELECT display_name, username, trader_score, trader_rationale,
                  racial_humor_score, slur_count, racism_rationale,
                  message_count_at_update
             FROM user_profiles
            WHERE LOWER(username) LIKE LOWER(?)
               OR LOWER(display_name) LIKE LOWER(?)""",
        (f"%{username_pat}%", f"%{username_pat}%"),
    ).fetchone()
    if r is None:
        print(f"  {username_pat}: NOT FOUND")
        return
    print(f"\n=== {r['display_name']} ({r['username']}) — msgs={r['message_count_at_update']} ===")
    print(f"  trader_score:        {r['trader_score']}")
    print(f"  racial_humor_score:  {r['racial_humor_score']}/100")
    print(f"  slur_count:          {r['slur_count']}")
    print()
    print(f"  TRADER RATIONALE:")
    rat = r['trader_rationale'] or ""
    for ln in rat.split('. '):
        if ln.strip():
            print(f"    {ln.strip()}.")
    print()
    print(f"  RACISM RATIONALE:")
    rat = r['racism_rationale'] or ""
    for ln in rat.split('. '):
        if ln.strip():
            print(f"    {ln.strip()}.")


print("=" * 76)
print("KLOH")
print("=" * 76)
show("kloh")
print("\n" + "=" * 76)
print("GRAND NAGUS YEEZY")
print("=" * 76)
show("nagus")
