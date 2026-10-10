"""A member's own trade log in their profile (scripts/profile_trades.py,
2026-10-08 audit cases)."""
from datetime import date

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


PROFILE = """**Voice.**
- "x"

**Recent trades.**
- TSLA 385C (exp 10-09) [OPEN per log — no exit posted] — [added to his position while calling for a rocket move, only to get stopped out shortly after]
- AAOI 120C (exp 10-09) [EXIT — closed +525.00%] — [a clean, documented win that he timed perfectly]
- MU 1025P exp 10-07 — [Closed at a realized loss after he tripled down on a bearish thesis]
- NVDA 200C exp 10-09 — [a loss he will never admit]
- SPCX 155C exp 10-09 — [cashed out a massive triple-digit return]
- Brazil — [scaled out for profit]

**Recent personal life.**
- "y"
"""


def test_invented_outcomes_on_open_trades_are_replaced_with_the_log():
    rows = [_row("open", "TSLA", 385, "call", "2026-10-06T15:00"),
            _row("close", "AAOI", 120, "call", "2026-10-06T16:00", gain=525.0),
            _row("open", "MU", 1025, "put", "2026-10-07T14:00"),
            _row("close", "NVDA", 200, "call", "2026-10-07T15:00", gain=80.0),
            _row("close", "SPCX", 155, "call", "2026-10-07T15:30", gain=300.0)]
    text, fixed = T.ground_recent_trades(PROFILE, rows)
    assert "stopped out" not in text and "realized loss" not in text
    assert "TSLA 385C (exp 10-09) [OPEN per log — no exit posted] — [no exit posted in the log]" in text
    assert "MU 1025P exp 10-07 — [no exit posted in the log]" in text
    # a loss claimed on a winning close takes the log's number
    assert "NVDA 200C exp 10-09 — [closed +80.00% per the log]" in text
    # supported outcomes stay, and so does a line the log cannot check
    assert "a clean, documented win" in text and "cashed out a massive" in text
    assert "Brazil — [scaled out for profit]" in text
    assert len(fixed) == 3
    assert text.endswith('**Recent personal life.**\n- "y"\n')


def test_no_log_rows_leaves_the_profile_alone():
    assert T.ground_recent_trades(PROFILE, []) == (PROFILE, [])


def test_a_bullet_that_starts_with_a_verb_is_checked():
    rows = [_row("open", "TSLA", 385, "call", "2026-10-06T15:00")]
    text, fixed = T.ground_recent_trades(
        "**Recent trades.**\n- Closed TSLA 385C for a loss — [got stopped out]\n", rows)
    assert fixed and "[no exit posted in the log]" in text


def test_an_earlier_round_trip_does_not_license_a_reopen():
    rows = [_row("open", "TSLA", 385, "call", "2026-10-01T15:00"),
            _row("close", "TSLA", 385, "call", "2026-10-02T15:00", gain=40.0),
            _row("open", "TSLA", 385, "call", "2026-10-06T15:00")]
    text, fixed = T.ground_recent_trades(
        "**Recent trades.**\n- TSLA 385C — [stopped out again]\n", rows)
    assert fixed and "[no exit posted in the log]" in text


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
