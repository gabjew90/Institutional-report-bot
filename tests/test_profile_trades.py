"""A member's own trade log in their profile (scripts/profile_trades.py,
2026-10-08 audit cases)."""
from datetime import date

import db  # noqa: F401  (loads the facade before db_parts)

from scripts import profile_trades as T


def _row(action, ticker, strike, ct, posted, gain=None, expiry="2026-10-09", status=""):
    return {"action": action, "ticker": ticker, "strike": strike, "contract_type": ct,
            "posted_at": posted, "gain_pct": gain, "expiry": expiry,
            "tracking_mode": "member", "inferred_status": status}


def test_an_open_with_a_later_close_is_not_called_expired():
    rows = [_row("open", "TSLA", 370, "call", "2026-09-30T15:00", expiry="2026-10-02"),
            _row("close", "TSLA", 370, "call", "2026-10-02T15:00", gain=100.0, expiry="2026-10-02")]
    block = T.render_block(rows, today=date(2026, 10, 8))
    assert "CLOSED later" in block and "EXPIRED" not in block


def test_impossible_gains_are_blank():
    rows = [_row("close", "NBIS", 100, "put", "2026-10-06T15:00", gain=-600.0)]
    assert "gain ?" in T.render_block(rows) and "-600" not in T.render_block(rows)
    assert T.shown_gain(-100.0) == -100.0 and T.shown_gain(-600) is None


def test_a_spread_strike_does_not_break_the_block():
    assert "TSLA 450/460" in T.render_block([_row("open", "TSLA", "450/460", "call", "2026-10-06T15:00")])


def test_a_share_close_up_thousands_of_percent_is_an_extraction_error():
    """2026-10-09 audit: an APLD share close stored as +2009% (a dollar
    profit read as a percent) made a 0W/14L record read avg +245%."""
    assert T.shown_gain(2009.0, "stock") is None
    assert T.shown_gain(2009.0, "call") == 2009.0
    assert T.shown_gain(45.0, "stock") == 45.0
    assert T.shown_gain(1500.0, "unclear") is None
    assert T.shown_gain(1500.0, "put") == 1500.0


PROFILE = """**Voice.**
- "x y"

**Recent trades.**
- TSLA 385C [OPEN per log] - [got stopped out, then bought back]
- LITE 980C +169.27%

**Recent personal life.**
- "y z"
"""


def test_recent_trades_are_written_from_the_log_2026_10_30():
    """Run 7 design note 3: the writer's section is discarded. M7 (a gain
    the log lacks), M8 (an outcome on an open trade) and M20 (a logged win
    left out) cannot appear; the other sections are untouched."""
    rows = [_row("open", "TSLA", 385, "call", "2026-10-06T15:00", expiry="2026-11-20"),
            _row("open", "NDX", 25000, "call", "2026-10-07T15:00"),
            _row("close", "NDX", 25000, "call", "2026-10-08T15:00", gain=580.0),
            _row("close", "QQQ", 600, "put", "2026-10-09T15:00", gain=-45.0)]
    text = T.write_recent_trades(PROFILE, rows, today=date(2026, 10, 30))
    assert "stopped out" not in text and "169" not in text
    assert "- NDX 25000C exp 10/09: opened Oct 7, closed Oct 8, +580.0%" in text
    assert "- QQQ 600P exp 10/09: exit posted Oct 9, -45.0%, no entry logged" in text
    assert "- TSLA 385C exp 11/20: opened Oct 6, no exit posted" in text
    assert text.index("NDX") < text.index("TSLA")          # closes first
    assert '**Voice.**\n- "x y"' in text and '**Recent personal life.**\n- "y z"' in text


def test_an_expired_open_and_an_empty_log_2026_10_30():
    rows = [_row("open", "NET", 380, "call", "2026-09-25T15:00", expiry="2026-10-16"),
            _row("open", "MON", 5, "call", "2026-10-01T15:00")]       # M21: a weekday
    text = T.write_recent_trades("**Recent trades.**\n- NET to the moon", rows, today=date(2026, 10, 30))
    assert text == ("**Recent trades.**\n- NET 380C exp 10/16: opened Sep 25, "
                    "expiry passed with no exit posted, outcome unknown")
    assert T.write_recent_trades("**Recent trades.**\n- stuff\n\n**Recent personal life.**\n- a", []) == (
        "**Recent trades.**\n- No trades logged in the last 30 days.\n\n**Recent personal life.**\n- a")


def test_a_reopen_after_a_close_shows_both_2026_10_30():
    rows = [_row("open", "TSLA", 385, "call", "2026-10-01T15:00"),
            _row("close", "TSLA", 385, "call", "2026-10-02T15:00", gain=40.0),
            _row("open", "TSLA", 385, "call", "2026-10-06T15:00")]
    lines = T.section_lines(rows, today=date(2026, 10, 7))
    assert lines == ["- TSLA 385C exp 10/09: opened Oct 1, closed Oct 2, +40.0%",
                     "- TSLA 385C exp 10/09: opened Oct 6, no exit posted"]
