"""Smoke: the TRADE BOARD carries today's calls and nothing else.

2026-08-12 owner decision. The board had become a status ledger: a
"held since <date>" line for every live position, "off board since"
exit rows with move scoring, FLIP and RE-ENTRY labels, and a six-line
legend defining all of it reprinted verbatim every pulse. That day's
board was eight rows of repeats and exits, zero new calls, under a
legend longer than the content.

New contract: one list, no legend, no status prefixes. The leans this
pulse is initiating plus the desks' high-conviction single-name calls,
merged. Every line is a call being made today so no line needs a label
saying so.

This file replaces the rendering assertions in smoke_board_dropped,
smoke_thesis_flip and smoke_trade_board_churn. The DETECTION those files
cover (lineage, thesis flips, reversal counts, prev_seen_date) is
untouched and still unit-tested there — it just no longer renders.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _row(inst, first, last, prev=None, ctx=None, direction="long"):
    return {"instrument": inst, "direction": direction,
            "first_seen_date": first, "last_seen_date": last,
            "prev_seen_date": prev,
            "context_snippet": ctx or f"Long ${inst} · thesis"}


HC = [{"source": "Deutsche Bank", "ticker": "DASH", "action": "reiterate",
       "pt": "$260", "rating": "Buy",
       "rationale": "Strong execution and accelerating flywheel."}]


def _board(rows, hc=None, today="2026-08-12", prev="2026-08-11"):
    from report.pulse_sections import render_trade_board
    return render_trade_board(board_rows=rows, today=today, hc_calls=hc,
                              prev_board_date=prev)


def test_only_todays_calls_render():
    md = _board([
        _row("MU", "2026-08-12", "2026-08-12", ctx="Long $MU · new today"),
        _row("BNO", "2026-08-06", "2026-08-12", prev="2026-08-11",
             ctx="Long $BNO · carried from Aug 6"),
    ])
    if "$MU" not in md:
        _fail(f"today's new call is missing:\n{md}")
    if "$BNO" in md:
        _fail(f"a carried lean still renders:\n{md}")
    _ok("only leans first flagged today render")


def test_no_status_labels():
    md = _board([_row("MU", "2026-08-12", "2026-08-12")])
    for label in ("NEW", "FLIP", "RE-ENTRY", "held since", "off board"):
        if label in md:
            _fail(f"status label {label!r} still renders:\n{md}")
    _ok("no NEW / FLIP / RE-ENTRY / held / off-board labels")


def test_no_legend():
    md = _board([_row("MU", "2026-08-12", "2026-08-12")], hc=HC)
    for phrase in ("first flagged today", "reverses a view",
                   "Leans this pulse is making",
                   "is scored where price data exists"):
        if phrase in md:
            _fail(f"legend text survives: {phrase!r}")
    _ok("the repeated legend is gone")


def test_desk_calls_merge_into_the_same_list():
    md = _board([_row("MU", "2026-08-12", "2026-08-12")], hc=HC)
    if "High-conviction single-name calls" in md:
        _fail(f"desk calls still render under their own header:\n{md}")
    bullets = [l for l in md.splitlines() if l.startswith("- ")]
    if len(bullets) != 2:
        _fail(f"expected one lean + one desk call in one list, got "
              f"{len(bullets)}:\n{md}")
    if "$MU" not in bullets[0] or "$DASH" not in bullets[1]:
        _fail(f"lean and desk call are not in one list:\n{md}")
    _ok("desk calls merge into the same list as the leans")


def test_rating_legend_survives():
    """Short, conditional, and it exists because OW/UW once shipped with
    no decode (2026-07-15). Not the boilerplate that was cut."""
    md = _board([], hc=HC)
    if "PT = price target" not in md:
        _fail(f"rating decode was lost:\n{md}")
    _ok("the conditional rating decode still renders")


def test_no_new_calls_renders_nothing():
    """A day with only carried positions has made no calls. Say nothing
    rather than reprint yesterday."""
    md = _board([_row("BNO", "2026-08-06", "2026-08-12", prev="2026-08-11")])
    if md.strip():
        _fail(f"board rendered with no new calls:\n{md}")
    _ok("a board with no new calls renders empty")


def test_desk_calls_alone_still_render():
    md = _board([], hc=HC)
    if "$DASH" not in md or "TRADE BOARD" not in md:
        _fail(f"desk calls alone did not render:\n{md}")
    _ok("desk calls render even with no leans of our own")


def test_tracking_is_unchanged():
    """The rewrite is presentation-only. Lineage must still be recorded
    so re-enabling any label family is a rendering change."""
    import sqlite3
    import db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbmod._init_schema(conn)
    dbmod._conn = conn
    lean = [{"instrument": "MU", "direction": "long", "context": "Long $MU"}]
    dbmod.upsert_pulse_leans("2026-08-07", lean)
    dbmod.upsert_pulse_leans("2026-08-12", lean)
    r = conn.execute(
        "SELECT first_seen_date, prev_seen_date, last_seen_date "
        "FROM pulse_leans WHERE instrument='MU'").fetchone()
    if (r["first_seen_date"], r["prev_seen_date"], r["last_seen_date"]) != (
            "2026-08-07", "2026-08-07", "2026-08-12"):
        _fail(f"lineage tracking changed: {dict(r)}")
    _ok("pulse_leans still records full lineage behind the scenes")


if __name__ == "__main__":
    test_only_todays_calls_render()
    test_no_status_labels()
    test_no_legend()
    test_desk_calls_merge_into_the_same_list()
    test_rating_legend_survives()
    test_no_new_calls_renders_nothing()
    test_desk_calls_alone_still_render()
    test_tracking_is_unchanged()
    print("\nAll new-calls-only board smoke tests passed.")
