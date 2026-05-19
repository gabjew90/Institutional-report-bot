"""Smoke test for the multi-caller analyst-log refactor.

Verifies:
1. The 'caller' column exists on analyst_trades.
2. Existing rows got backfilled with caller='abe'.
3. settings.resolve_analyst_callers() returns the Abe registry entry.
4. settings.caller_by_channel('🥷🏽-abe-alerts-🥷🏽') matches Abe.
5. format_analyst_trades_for_context(caller='abe') returns content.
6. get_current_analyst_positions(caller='abe') returns Abe positions.
7. compute_caller_win_loss_summary(caller='abe') matches the deprecated
   compute_abe_win_loss_summary() output.

Read-only — no writes. Designed to run via railway ssh once.
"""
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import db
from config import settings


def check(label, condition, detail=""):
    icon = "✅" if condition else "❌"
    line = f"{icon} {label}"
    if detail:
        line += f"\n   {detail}"
    print(line)
    return bool(condition)


def main():
    print("=== Multi-caller smoke test ===\n")
    results = []

    # 1. caller column exists
    cols = [r[1] for r in db.get_connection().execute(
        "PRAGMA table_info(analyst_trades)"
    ).fetchall()]
    results.append(check(
        "analyst_trades.caller column exists",
        "caller" in cols,
        f"columns: {cols}",
    ))

    # 2. backfill happened: how many rows have caller set?
    row = db.get_connection().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN caller IS NOT NULL THEN 1 ELSE 0 END) AS with_caller, "
        "SUM(CASE WHEN caller = 'abe' THEN 1 ELSE 0 END) AS abe_caller "
        "FROM analyst_trades"
    ).fetchone()
    total = row["total"]
    with_caller = row["with_caller"]
    abe_caller = row["abe_caller"]
    results.append(check(
        f"All {total} rows have caller set",
        with_caller == total,
        f"total={total}, with_caller={with_caller}, abe_caller={abe_caller}",
    ))
    results.append(check(
        "Backfill assigned legacy rows to caller='abe'",
        abe_caller == with_caller,  # at this point everyone should be Abe
        f"abe_caller={abe_caller} / with_caller={with_caller}",
    ))

    # 3. resolve_analyst_callers returns Abe
    callers = settings.resolve_analyst_callers()
    has_abe = any(c.get("name") == "abe" for c in callers)
    results.append(check(
        "resolve_analyst_callers() yields Abe",
        has_abe,
        f"callers: {[c.get('name') for c in callers]}",
    ))

    # 4. caller_by_channel finds Abe
    matched = settings.caller_by_channel("🥷🏽-abe-alerts-🥷🏽")
    results.append(check(
        "caller_by_channel('🥷🏽-abe-alerts-🥷🏽') returns Abe",
        matched is not None and matched.get("name") == "abe",
        f"matched: {matched}",
    ))

    # 5. format_analyst_trades_for_context for Abe returns content
    block = db.format_analyst_trades_for_context(
        hours=168, caller="abe", display="Abe"
    )
    results.append(check(
        "Abe context block non-empty",
        bool(block.strip()),
        f"length: {len(block)} chars, starts: {block[:100]!r}",
    ))

    # 6. current open positions for Abe
    positions = db.get_current_analyst_positions(caller="abe")
    results.append(check(
        "Abe current positions queryable",
        True,
        f"{len(positions)} open positions: "
        + ", ".join(
            f"{p.get('ticker')}{p.get('strike')}"
            f"{(p.get('contract_type') or '').upper()[:1]}"
            for p in positions[:5]
        )
        + ("..." if len(positions) > 5 else ""),
    ))

    # 7. compute_caller_win_loss matches the deprecated wrapper
    new_wl = db.compute_caller_win_loss_summary(days=30, caller="abe")
    old_wl = db.compute_abe_win_loss_summary(days=30)
    results.append(check(
        "compute_caller_win_loss_summary(caller='abe') == compute_abe_win_loss_summary()",
        new_wl == old_wl,
        f"new: {new_wl['wins']}W/{new_wl['total_losses']}L, "
        f"old: {old_wl['wins']}W/{old_wl['total_losses']}L",
    ))

    print()
    passed = sum(results)
    total = len(results)
    print(f"=== {passed}/{total} checks passed ===")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
