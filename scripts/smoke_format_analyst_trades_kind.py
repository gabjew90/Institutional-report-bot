"""Smoke test for the `kind` parameter on format_analyst_trades_for_context.

Validates:
  1. kind="all" (default) preserves today's behavior
  2. kind="recent" returns ONLY the RECENT TRADES sub-block
  3. kind="open" returns ONLY the CURRENTLY OPEN POSITIONS sub-block
  4. kind="tally" returns ONLY the W/L TALLY sub-block
  5. kind="invalid" raises ValueError
  6. kind="all" with no recent rows returns "" (legacy quirk)
"""

import sys
from unittest.mock import patch

from db import format_analyst_trades_for_context


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Stub the three data-source functions so the test is hermetic.
FAKE_RECENT_ROWS = [
    {
        "posted_at": "2026-06-01T15:00", "action": "open", "ticker": "TSLA",
        "contract_type": "call", "strike": 445.0, "expiry": "2026-06-05",
        "price": 3.55, "gain_pct": None, "inferred_status": None,
    },
]
FAKE_POSITIONS = [
    {
        "ticker": "META", "contract_type": "call", "strike": 640.0,
        "expiry": "2026-06-05", "entry_price": 6.40,
    },
]
FAKE_WL = {"days": 30, "wins": 42, "losses_documented": 3,
           "losses_silent_expiry": 26, "total_losses": 29, "flat": 0,
           "decided": 71, "win_rate_pct": 59.2, "avg_win_pct": 104.9,
           "avg_loss_pct": -22.4, "win_trades": [], "silent_expiry_trades": []}
EMPTY_WL = {"days": 30, "wins": 0, "losses_documented": 0,
            "losses_silent_expiry": 0, "total_losses": 0, "flat": 0,
            "decided": 0, "win_rate_pct": 0.0, "avg_win_pct": None,
            "avg_loss_pct": None, "win_trades": [], "silent_expiry_trades": []}


def test_kind_all_returns_all_three_blocks():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="all")
    assert "ABE'S RECENT TRADES" in out, "missing RECENT block in kind=all"
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, "missing OPEN block in kind=all"
    assert "ABE'S W/L TALLY" in out, "missing TALLY block in kind=all"
    _ok("kind='all' returns all three sub-blocks")


def test_kind_recent_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="recent")
    assert "ABE'S RECENT TRADES" in out, "missing RECENT block in kind=recent"
    assert "ABE'S CURRENTLY OPEN POSITIONS" not in out, "kind=recent should not include OPEN"
    assert "ABE'S W/L TALLY" not in out, "kind=recent should not include TALLY"
    _ok("kind='recent' returns only RECENT block")


def test_kind_open_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="open")
    assert "ABE'S RECENT TRADES" not in out, "kind=open should not include RECENT"
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, "missing OPEN block in kind=open"
    assert "ABE'S W/L TALLY" not in out, "kind=open should not include TALLY"
    _ok("kind='open' returns only OPEN block")


def test_kind_tally_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="tally")
    assert "ABE'S RECENT TRADES" not in out, "kind=tally should not include RECENT"
    assert "ABE'S CURRENTLY OPEN POSITIONS" not in out, "kind=tally should not include OPEN"
    assert "ABE'S W/L TALLY" in out, "missing TALLY block in kind=tally"
    _ok("kind='tally' returns only TALLY block")


def test_kind_invalid_raises():
    try:
        format_analyst_trades_for_context(caller="abe", kind="bogus")
    except ValueError as e:
        assert "kind" in str(e).lower()
        _ok("kind='bogus' raises ValueError")
        return
    _fail("kind='bogus' should have raised ValueError")


def test_kind_all_no_recent_returns_empty():
    """Legacy quirk: kind='all' with no recent rows returns '' even if positions exist."""
    with (
        patch("db.get_recent_analyst_trades", return_value=[]),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="all")
    assert out == "", f"expected '', got {out!r}"
    _ok("kind='all' with no recent rows returns '' (legacy quirk preserved)")


def test_kind_open_no_recent_still_emits_open():
    """kind='open' alone does NOT skip on empty recent — that quirk is kind='all' only."""
    with (
        patch("db.get_recent_analyst_trades", return_value=[]),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=EMPTY_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="open")
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, (
        "kind='open' alone should emit the OPEN block even without recent rows"
    )
    _ok("kind='open' alone emits even when no recent rows")


if __name__ == "__main__":
    print("=== format_analyst_trades_for_context kind smoke ===")
    test_kind_all_returns_all_three_blocks()
    test_kind_recent_only()
    test_kind_open_only()
    test_kind_tally_only()
    test_kind_invalid_raises()
    test_kind_all_no_recent_returns_empty()
    test_kind_open_no_recent_still_emits_open()
    print("\nALL KIND-PARAMETER SMOKE TESTS PASS")
