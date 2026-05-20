"""One-off cross-attribution audit between profiles and caller logs."""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import db


def main():
    conn = db.get_connection()

    print("=== PROFILE TABLE: bankerkyle row ===")
    row = conn.execute(
        "SELECT user_id, username, display_name FROM user_profiles WHERE username=?",
        ("bankerkyle",),
    ).fetchone()
    if row:
        print(f"  user_id={row[0]} username={row[1]} display={row[2]}")
    else:
        print("  (not found — BK has no profile)")

    print()
    print("=== ANALYST TRADES: caller counts ===")
    rows = conn.execute(
        "SELECT caller, COUNT(*) FROM analyst_trades WHERE is_trade=1 GROUP BY caller"
    ).fetchall()
    for r in rows:
        print(f"  caller={r[0]!r}: {r[1]} rows")
    null_caller = conn.execute(
        "SELECT COUNT(*) FROM analyst_trades WHERE caller IS NULL AND is_trade=1"
    ).fetchone()[0]
    print(f"  caller=NULL: {null_caller} rows")

    print()
    print("=== Caller-block separation ===")
    abe_block = db.format_analyst_trades_for_context(
        hours=168, caller="abe", display="Abe"
    )
    bk_block = db.format_analyst_trades_for_context(
        hours=168, caller="bankerkyle", display="BK"
    )
    bk_only_tickers = ["BABA", "HIMS"]
    abe_only_tickers = ["NOW", "TSLA", "LMT"]
    leaks_into_abe = [t for t in bk_only_tickers if t in abe_block]
    leaks_into_bk = [t for t in abe_only_tickers if t in bk_block]
    print(f"  Abe block starts: {abe_block[:60]!r}")
    print(f"  BK  block starts: {bk_block[:60]!r}")
    print(f"  BK-only tickers (BABA, HIMS) leaking into Abe block: {leaks_into_abe}")
    print(f"  Abe-only tickers (NOW, TSLA, LMT) leaking into BK block: {leaks_into_bk}")
    print()
    print(f"  Abe block length: {len(abe_block)}")
    print(f"  BK  block length: {len(bk_block)}")

    print()
    print("=== get_current_analyst_positions per caller ===")
    abe_pos = db.get_current_analyst_positions(caller="abe")
    bk_pos = db.get_current_analyst_positions(caller="bankerkyle")
    bk_pos_tickers = {p["ticker"] for p in bk_pos}
    abe_pos_tickers = {p["ticker"] for p in abe_pos}
    overlap = bk_pos_tickers & abe_pos_tickers
    print(f"  Abe currently open: {len(abe_pos)} positions, tickers: {sorted(abe_pos_tickers)}")
    print(f"  BK  currently open: {len(bk_pos)} positions, tickers: {sorted(bk_pos_tickers)}")
    print(f"  Tickers both are long: {sorted(overlap)}")
    print(f"  (overlap is expected — BK tails Abe; each row stays caller-tagged)")

    print()
    print("=== compute_caller_win_loss_summary separation ===")
    abe_wl = db.compute_caller_win_loss_summary(days=30, caller="abe")
    bk_wl = db.compute_caller_win_loss_summary(days=30, caller="bankerkyle")
    print(f"  Abe W/L: {abe_wl['wins']}W / {abe_wl['total_losses']}L (decided: {abe_wl['decided']})")
    print(f"  BK  W/L: {bk_wl['wins']}W / {bk_wl['total_losses']}L (decided: {bk_wl['decided']})")
    # BK has 10 opens, 0 closes — so W/L should be 0/0, decided=0
    if bk_wl["decided"] == 0:
        print("  ✓ BK has no decided trades yet (correct — 10 opens, no closes)")

    print()
    print("=== format_user_profiles_for_context separation check ===")
    # Get bankerkyle's user_id
    bk_row = conn.execute("SELECT user_id FROM user_profiles WHERE username='bankerkyle'").fetchone()
    abe_row = conn.execute("SELECT user_id FROM user_profiles WHERE username='abullish_xyz'").fetchone()
    if bk_row and abe_row:
        bk_uid, abe_uid = bk_row[0], abe_row[0]
        bk_profile_block = db.format_user_profiles_for_context([bk_uid])
        # The BK profile shouldn't ever pull from caller logs — it's purely profile_text
        print(f"  BK profile block contains 'analyst_trades'?  {'analyst_trades' in bk_profile_block}")
        print(f"  BK profile block contains BK ticker words (BABA/HIMS/SOFI)?")
        for t in ["BABA", "HIMS", "SOFI"]:
            print(f"    {t}: {t in bk_profile_block}")
        # Profile block start
        print(f"  BK profile block starts: {bk_profile_block[:80]!r}")
    else:
        print("  (couldn't resolve user_ids for the profile separation check)")


if __name__ == "__main__":
    main()
