"""Smoke: a lean that was dropped and re-added is not called "held since".

2026-08-12 board shipped two false continuity claims:

  held since Aug 7   Long $MU      — off the board Aug 10 AND Aug 11
  held since Aug 10  $QQQ puts     — off the board Aug 11, where it was
                                     scored "flat (-0.3%) since flagged"
                                     on its way out

"held since X" tells a follower the call ran unbroken from X. Both had
been dropped and picked back up. The renderer decided NEW purely on
`first_seen_date == today`, and the upsert stamps `last_seen_date` to
today on re-appearance, so nothing recorded the gap.

prev_seen_date preserves the last_seen_date from BEFORE today's upsert,
which is what makes a gap visible.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _fresh():
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    dbmod._migrate_add_lean_prev_seen(conn)
    dbmod._conn = conn
    return dbmod, conn


def test_migration_is_idempotent():
    dbmod, conn = _fresh()
    dbmod._migrate_add_lean_prev_seen(conn)
    dbmod._migrate_add_lean_prev_seen(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pulse_leans)")}
    if "prev_seen_date" not in cols:
        _fail("prev_seen_date column missing after migration")
    _ok("prev_seen_date migration is idempotent")


def test_upsert_records_the_previous_sighting():
    dbmod, conn = _fresh()
    lean = [{"instrument": "MU", "direction": "long", "context": "Long $MU"}]
    dbmod.upsert_pulse_leans("2026-08-07", lean)
    dbmod.upsert_pulse_leans("2026-08-12", lean)   # gap: 10th and 11th
    r = conn.execute(
        "SELECT first_seen_date, prev_seen_date, last_seen_date "
        "FROM pulse_leans WHERE instrument='MU'").fetchone()
    if r["first_seen_date"] != "2026-08-07":
        _fail(f"first_seen moved: {r['first_seen_date']}")
    if r["prev_seen_date"] != "2026-08-07":
        _fail(f"prev_seen should be the prior sighting, got "
              f"{r['prev_seen_date']}")
    if r["last_seen_date"] != "2026-08-12":
        _fail("last_seen was not refreshed")
    _ok("upsert preserves the prior sighting before overwriting it")


def _render(rows, today, prev_board_date):
    """Return only the LEAN BULLETS. The legend defines every label, so
    asserting against the whole block matches the glossary rather than
    the rendered call."""
    from report.pulse_sections import render_trade_board
    md = render_trade_board(
        board_rows=rows, today=today, prev_board_date=prev_board_date)
    return "\n".join(
        ln for ln in md.splitlines()
        if ln.startswith("- **") and "first flagged today" not in ln)


def _row(inst, first, prev, last, direction="long"):
    return {"instrument": inst, "direction": direction,
            "first_seen_date": first, "prev_seen_date": prev,
            "last_seen_date": last, "context_snippet": f"Long ${inst}"}


def test_the_real_mu_case_renders_as_reentry():
    """$MU: first Aug 7, absent Aug 10 and Aug 11, back Aug 12."""
    md = _render([_row("MU", "2026-08-07", "2026-08-07", "2026-08-12")],
                 "2026-08-12", "2026-08-11")
    if "held since" in md:
        _fail(f"still claims an unbroken hold:\n{md}")
    if "RE-ENTRY" not in md:
        _fail(f"no RE-ENTRY label:\n{md}")
    _ok("the real $MU case renders as RE-ENTRY, not held since")


def test_continuous_hold_still_says_held():
    """$BNO was on yesterday's board and today's — a genuine hold."""
    md = _render([_row("BNO", "2026-08-06", "2026-08-11", "2026-08-12")],
                 "2026-08-12", "2026-08-11")
    if "RE-ENTRY" in md:
        _fail(f"a genuine continuous hold was demoted to RE-ENTRY:\n{md}")
    if "held since" not in md:
        _fail(f"continuous hold lost its label:\n{md}")
    _ok("an unbroken hold still renders as held since")


def test_new_lean_is_unaffected():
    md = _render([_row("XYZ", "2026-08-12", None, "2026-08-12")],
                 "2026-08-12", "2026-08-11")
    if "**NEW**" not in md:
        _fail(f"a first-flagged lean lost its NEW label:\n{md}")
    if "RE-ENTRY" in md:
        _fail("a NEW lean was labelled RE-ENTRY")
    _ok("a first-flagged lean is still NEW")


def test_legacy_null_prev_seen_falls_back_to_held():
    """Rows written before the column existed must not invent re-entries."""
    md = _render([_row("GLD", "2026-07-23", None, "2026-08-12")],
                 "2026-08-12", "2026-08-11")
    if "RE-ENTRY" in md:
        _fail("a NULL prev_seen_date was read as a gap")
    if "held since" not in md:
        _fail("legacy row lost its held label")
    _ok("NULL prev_seen_date falls back to held, never invents a gap")


def test_legend_documents_reentry():
    from report.pulse_sections import render_trade_board
    md = render_trade_board(
        board_rows=[_row("MU", "2026-08-07", "2026-08-07", "2026-08-12")],
        today="2026-08-12", prev_board_date="2026-08-11")
    if "**RE-ENTRY**" not in md:
        _fail("legend does not define RE-ENTRY")
    if "unbroken" not in md:
        _fail("legend still describes 'held since' loosely enough to cover "
              "a re-entry")
    _ok("legend defines RE-ENTRY and tightens 'held since'")


if __name__ == "__main__":
    test_migration_is_idempotent()
    test_upsert_records_the_previous_sighting()
    test_the_real_mu_case_renders_as_reentry()
    test_continuous_hold_still_says_held()
    test_new_lean_is_unaffected()
    test_legacy_null_prev_seen_falls_back_to_held()
    test_legend_documents_reentry()
    print("\nAll board re-entry smoke tests passed.")
