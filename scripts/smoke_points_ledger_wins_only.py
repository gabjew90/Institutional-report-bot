"""Smoke test for the new wins-only +2 points ledger."""

import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import db

# Anchor test trades to "now" so they stay inside the lookback window as
# real-world today advances. The prior hardcoded 2026-06-01/02 dates
# rotted out of the 14-day window on 2026-06-16, decomposing the
# entry+close pair into a close-only screenshot win and failing the
# suite — a pure test-staleness bug, unrelated to scoring logic.
_NOW = datetime.utcnow()
_T0 = (_NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")  # "open"
_T1 = (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")  # "close"


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn_with_trade_history(events: list[tuple]):
    """events = list of (posted_at, action, ticker, gain_pct, channel)"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db._init_schema(c)
    db._migrate_drop_unique_constraints(c)
    db._migrate_add_extraction_source(c)
    for i, (posted_at, action, ticker, gain_pct, channel) in enumerate(events, 1):
        c.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, posted_at, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1000 + i, 1, channel, 100, "u", posted_at, "x"),
        )
        c.execute(
            "INSERT INTO analyst_trades (discord_message_id, "
            "discord_attachment_id, author, author_id, posted_at, "
            "ticker, action, gain_pct, is_trade, tracking_mode, "
            "extraction_source) "
            "VALUES (?, ?, 'u', 100, ?, ?, ?, ?, 1, 'member', 'image')",
            (1000 + i, i, posted_at, ticker, action, gain_pct),
        )
    return c


def test_default_window_is_7_days():
    c = _conn_with_trade_history([])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["window_days"] == 7, (
        f"expected default window_days=7, got {result['window_days']}"
    )
    _ok("default window is 7 days")


def test_entry_plus_winning_close_scores_2():
    today = _T0
    later = _T1
    c = _conn_with_trade_history([
        (today, "open", "AAPL", None, "💲-gain-loss-porn-💲"),
        (later, "close", "AAPL", 50.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)  # wide window
    assert result["entries_won"] == 1, result
    assert result["points"] == 2, f"expected 2 pts for 1 win, got {result['points']}"
    _ok("entry+winning_close = +2 (not +5)")


def test_entry_plus_losing_close_scores_0():
    today = _T0
    later = _T1
    c = _conn_with_trade_history([
        (today, "open", "TSLA", None, "🕰️-member-alerts-🕰️"),
        (later, "close", "TSLA", -30.0, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["entries_lost"] == 1, result
    assert result["points"] == 0, (
        f"expected 0 pts for 1 documented loss, got {result['points']}"
    )
    _ok("entry+losing_close = 0 (loss earns no points)")


def test_screenshot_win_scores_2():
    today = _T0
    c = _conn_with_trade_history([
        (today, "close", "NVDA", 25.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["screenshot_wins"] == 1, result
    assert result["points"] == 2, result
    _ok("standalone screenshot win = +2")


def test_screenshot_loss_scores_0():
    today = _T0
    c = _conn_with_trade_history([
        (today, "close", "META", -10.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["screenshot_losses"] == 1, result
    assert result["points"] == 0, result
    _ok("standalone screenshot loss = 0")


def test_ghost_scores_0():
    """Old policy: ghost = +2 for members. New policy: 0."""
    # Old open (well past the 14d window so it ghosts).
    old = "2026-04-01T12:00:00"
    c = _conn_with_trade_history([
        (old, "open", "LMT", None, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=60)  # widen to capture row
    # Ghost should be counted but contribute 0 points
    assert result["points"] == 0, result
    _ok("aged-out entry (ghost) = 0 pts under new policy")


if __name__ == "__main__":
    print("=== wins-only +2 ledger smoke ===")
    test_default_window_is_7_days()
    test_entry_plus_winning_close_scores_2()
    test_entry_plus_losing_close_scores_0()
    test_screenshot_win_scores_2()
    test_screenshot_loss_scores_0()
    test_ghost_scores_0()
    print("\nALL POINTS-LEDGER SMOKE TESTS PASS")
