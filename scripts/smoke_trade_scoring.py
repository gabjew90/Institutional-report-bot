"""Smoke: score exited trade-board leans + price-history capability.

2026-07-29 pulse feedback: the TRADE BOARD churns instead of tracking —
leans exit silently as "off board since Jul 27" with no outcome. Long
$BNO (held since Jul 22) ate a 6%+ oil crash and just vanished; Long
$GLD fell off while that same pulse's RECAP noted gold -1.45%. "Off
board" was doing the work "stopped out, here's the damage" should do.

Fix: yfinance daily history (already a dependency) gives the move from
first_seen to now, scored DIRECTION-AWARE (a short that fell is a win).
The same helper also closes the /ask fabrication gap — the model
invented weekly S&P closes for a correlation chart because it had no
history source. Now it has one.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_score_long_loss():
    from report.market_data import score_lean_move
    with patch("report.market_data.price_move_since",
               return_value={"first": 49.32, "last": 46.74, "pct": -5.23}):
        s = score_lean_move("BNO", "long", "2026-07-22")
    # magnitude must be present; the sign lives in the words ("against
    # it"), which reads better than a redundant minus.
    assert s and "5.2%" in s, s
    assert "against" in s.lower() or "lost" in s.lower(), (
        f"a long that fell must read as a loss: {s}"
    )
    _ok("long that fell scores as a loss with the %")


def test_score_short_win():
    from report.market_data import score_lean_move
    with patch("report.market_data.price_move_since",
               return_value={"first": 100.0, "last": 92.0, "pct": -8.0}):
        s = score_lean_move("SOXX", "short", "2026-07-22")
    assert s and "8.0%" in s, s
    assert "worked" in s.lower() or "won" in s.lower(), (
        f"a short that fell must read as a win: {s}"
    )
    _ok("short that fell scores as a win (direction-aware)")


def test_score_none_when_no_data():
    from report.market_data import score_lean_move
    with patch("report.market_data.price_move_since", return_value=None):
        assert score_lean_move("ZZZZ", "long", "2026-07-22") is None
    _ok("no price data -> no score (never fabricates an outcome)")


def test_exit_scoring_is_no_longer_rendered():
    """2026-08-12 (owner decision): the board carries today's calls only,
    so exited leans and their scored outcomes no longer render.
    score_lean_move itself is unchanged and still covered by the tests
    above — the scoring stays available for anything that wants it, it
    just does not appear on the board."""
    from report.pulse_sections import render_trade_board
    rows = [
        {"instrument": "SOXX", "direction": "short",
         "first_seen_date": "2026-07-28", "last_seen_date": "2026-07-28",
         "context_snippet": "Short $SOXX — crowded semis"},
        {"instrument": "BNO", "direction": "long",
         "first_seen_date": "2026-07-22", "last_seen_date": "2026-07-27",
         "context_snippet": "Long $BNO — Hormuz premium reloads"},
    ]
    out = render_trade_board(rows, "2026-07-28",
                             prev_board_date="2026-07-27")
    assert out == "", f"no house lean or exit row should render: {out}"
    _ok("exit rows and their scoring retired from the board")


def test_board_needs_no_scoring_to_render():
    """The board no longer calls score_lean_move at all, so a dead price
    feed cannot affect it. Previously this was a try/except guard."""
    import inspect
    import report.pulse_sections as ps
    # Skip the docstring — it names score_lean_move to record that the
    # scoring still exists and simply no longer renders.
    body = inspect.getsource(ps.render_trade_board).split('"""')[-1]
    assert "score_lean_move" not in body, \
        "board should no longer depend on the price feed"
    _ok("board render has no price-feed dependency left")


if __name__ == "__main__":
    print("=== trade scoring smoke ===")
    test_score_long_loss()
    test_score_short_win()
    test_score_none_when_no_data()
    test_exit_scoring_is_no_longer_rendered()
    test_board_needs_no_scoring_to_render()
    print("\nALL TRADE SCORING SMOKE TESTS PASS")
