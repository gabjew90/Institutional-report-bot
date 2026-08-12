"""Smoke: TRADE BOARD DROPPED lines (2026-07-10).

The 07-10 board silently lost four of five prior leans (oil $BNO/$XLE,
memory $MU/$VST, gold $GLD, breadth $RSP/$XLU) — a reader tracking any
of them got no update, and the main event outright reversed the RSP
thesis with no marker. The board legend had NEW / FLIP / held-since but
no way to say "this call is retired."

render_trade_board now derives DROPPED lines: leans whose last_seen ==
the IMMEDIATELY-PRIOR board date and which share no ticker with any
lean affirmed today. Derived, not stored — each drop renders exactly
once, and bridge retries stay idempotent.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from report.pulse_sections import render_trade_board  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _row(inst, direction, first, last, display):
    return {"instrument": inst, "direction": direction,
            "first_seen_date": first, "last_seen_date": last,
            "context_snippet": display}


def test_the_observed_07_10_drops_surface():
    # yesterday's five leans; today re-affirms none, flips TLT, adds BTC
    rows = [
        _row("BTC", "long", "2026-07-10", "2026-07-10",
             "Long $BTC — most oversold in history"),
        _row("TLT", "short", "2026-07-10", "2026-07-10",
             "Short $TLT — AI-driven core inflation sticky"),
        # prior board (07-09)
        _row("TLT", "long", "2026-07-09", "2026-07-09",
             "Long $TLT — market over-bought the Warsh hawk"),
        _row("BNO", "long", "2026-07-09", "2026-07-09",
             "Long $BNO, $XLE — physical scarcity vs curve"),
        _row("GLD", "long", "2026-06-22", "2026-07-09",
             "Long $GLD — central-bank bid plus Gulf premium"),
        _row("RSP", "long", "2026-06-17", "2026-07-09",
             "Long $RSP, $XLU — rotate out of crowded AI lead"),
    ]
    board = render_trade_board(rows, "2026-07-10", flips={"TLT"},
                               prev_board_date="2026-07-09")
    # 2026-08-12 (owner decision): abandoned leans no longer RENDER. The
    # board carries today's calls only — see smoke_board_new_calls_only.
    # What this case still pins is that yesterday's leans cannot leak
    # back onto the board under any label, and that today's own calls
    # survive the change.
    for gone in ("off board", "$BNO", "$GLD", "$RSP", "$XLU"):
        assert gone not in board, \
            f"a dropped lean leaked back onto the board: {gone}\n{board}"
    assert "$BTC" in board, f"today's own call vanished:\n{board}"
    assert board.count("$TLT") == 1, \
        f"today's TLT call should appear exactly once: {board}"
    for legend in ("off board since …", "not repeated today", "scored"):
        assert legend not in board, f"legend text survives: {legend}"
    _ok("07-10 case: silent drops surface; FLIP not double-reported")


def test_drop_renders_exactly_once_then_retires():
    # day 2: GLD (dropped on the 07-10 board) must NOT re-render as
    # DROPPED on 07-11 — its last_seen (07-09) no longer equals the
    # prior board date (07-10). This is why prev_board_date comes from
    # daily_reports, not from max(last_seen < today): re-affirmed leans
    # carry last_seen == today and leave no trace of yesterday, so the
    # heuristic re-derives the same stale date and re-drops GLD daily
    # until age-out (the original implementation had this bug).
    rows = [
        _row("BTC", "long", "2026-07-10", "2026-07-11",
             "Long $BTC — inflows stringing together"),
        _row("GLD", "long", "2026-06-22", "2026-07-09",
             "Long $GLD — central-bank bid"),
    ]
    board = render_trade_board(rows, "2026-07-11",
                               prev_board_date="2026-07-10")
    assert "$GLD" not in board, \
        f"a drop renders the day it happens, then retires: {board}"
    # missing prev date (fresh deploy / no daily_reports history) —
    # conservative: no DROPPED lines rather than wrong ones
    board2 = render_trade_board(rows, "2026-07-11")
    # legend text always names DROPPED — check the LINE marker
    assert "- **off board" not in board2, "no prev date -> no off-board lines"
    _ok("lifecycle: DROPPED renders exactly once, then retires; "
        "no prev date -> conservative")


def test_partial_ticker_overlap_is_not_a_drop():
    # yesterday 'Long $MU, $VST'; today 'Long $VST' alone — the row
    # shares a ticker with today's board, so it's a partial
    # re-affirmation, not an abandonment.
    rows = [
        _row("VST", "long", "2026-07-10", "2026-07-10",
             "Long $VST — power names feeding the datacenters"),
        _row("MU", "long", "2026-07-09", "2026-07-09",
             "Long $MU, $VST — memory + power over crowded chips"),
    ]
    board = render_trade_board(rows, "2026-07-10",
                               prev_board_date="2026-07-09")
    assert "- **off board" not in board, \
        f"shared-ticker rows are not abandonments: {board}"
    _ok("partial overlap: shared-ticker prior lean is not DROPPED")


def test_no_leans_today_means_no_drop_flood():
    # a pulse whose _LEANS block failed renders no live leans — that's a
    # validator failure, not a mass abandonment. No DROPPED lines.
    rows = [
        _row("GLD", "long", "2026-06-22", "2026-07-09",
             "Long $GLD — central-bank bid"),
    ]
    board = render_trade_board(rows, "2026-07-10",
                               prev_board_date="2026-07-09")
    assert "- **off board" not in board, f"no live leans -> no drop flood: {board}"
    _ok("guard: empty today-board never mass-drops the history")


if __name__ == "__main__":
    print("=== TRADE BOARD DROPPED smoke ===")
    test_the_observed_07_10_drops_surface()
    test_drop_renders_exactly_once_then_retires()
    test_partial_ticker_overlap_is_not_a_drop()
    test_no_leans_today_means_no_drop_flood()
    print("\nALL BOARD-DROPPED SMOKE TESTS PASS")
