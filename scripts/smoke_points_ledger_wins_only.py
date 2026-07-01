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


def test_default_window_is_21_days():
    # 2026-07-01: window widened 14 -> 21 with recency banding, so a
    # documented edge no longer evaporates at the two-week cliff.
    c = _conn_with_trade_history([])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["window_days"] == 21, (
        f"expected default window_days=21, got {result['window_days']}"
    )
    _ok("default window is 21 days")


def test_recency_banding_older_win_half_credit():
    """A win documented 10 days ago scores 1 pt (half credit), not 2 —
    and not 0 (the old 14d-cliff killed it entirely at day 15)."""
    old_open = (_NOW - timedelta(days=12)).strftime("%Y-%m-%dT%H:%M:%S")
    old_close = (_NOW - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S")
    c = _conn_with_trade_history([
        (old_open, "open", "MRVL", None, "🕰️-member-alerts-🕰️"),
        (old_close, "trim", "MRVL", 58.0, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["entries_won"] == 1, result
    assert result["wins_older"] == 1 and result["wins_recent"] == 0, result
    assert result["points"] == 1, (
        f"a 10-day-old documented win must score 1 (half credit), "
        f"got {result['points']}"
    )
    _ok("banding: 10-day-old winning trim = +1 (half credit, not 0)")


def test_recency_banding_mixed_total():
    """Recent win (2) + older win (1) = 3 total."""
    old_close = (_NOW - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%S")
    c = _conn_with_trade_history([
        (_T0, "close", "NVDA", 25.0, "💲-gain-loss-porn-💲"),
        (old_close, "close", "CSCO", 60.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["wins_recent"] == 1 and result["wins_older"] == 1, result
    assert result["points"] == 3, (
        f"recent(2) + older(1) must total 3, got {result['points']}"
    )
    _ok("banding: recent win 2 + 15-day-old win 1 = 3 pts")


def test_ghost_age_decoupled_from_window():
    """An entry 16 days old with no close must GHOST (fixed 14d rule),
    not sit pending just because the scoring window widened to 21d."""
    stale_open = (_NOW - timedelta(days=16)).strftime("%Y-%m-%dT%H:%M:%S")
    c = _conn_with_trade_history([
        (stale_open, "open", "GEO", None, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["entries_ghosted"] == 1, result
    assert result["entries_pending"] == 0, (
        "a 16-day-old closeless entry must ghost at the fixed 14d mark"
    )
    assert result["points"] == 0, result
    _ok("ghost age fixed at 14d — decoupled from the 21d scoring window")


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
    print("=== wins-only banded ledger smoke ===")
    test_default_window_is_21_days()
    test_recency_banding_older_win_half_credit()
    test_recency_banding_mixed_total()
    test_ghost_age_decoupled_from_window()
    test_entry_plus_winning_close_scores_2()
    test_entry_plus_losing_close_scores_0()
    test_screenshot_win_scores_2()
    test_screenshot_loss_scores_0()
    test_ghost_scores_0()
    print("\nALL POINTS-LEDGER SMOKE TESTS PASS")
