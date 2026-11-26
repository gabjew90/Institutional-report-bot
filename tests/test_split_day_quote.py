"""A quote on the day of a stock split (report/market_data.py, 2026-11-26).

2026-11-20: "SNDK $87.88 pre-market, -88.4% from the last close". $755.16
was the close before an 8-for-1 split and the move was about -7%."""
from datetime import datetime
from zoneinfo import ZoneInfo

from discord_bot import price_block
from report import market_data as M


def test_the_unsplit_previous_close_is_restated():
    hit = M.split_base(87.88, 755.16, 94.395)
    assert hit == (94.395, "8-for-1 split")
    q = {"symbol": "SNDK", "price": 87.88, "prev_close": 755.16, "regular_session_close": 755.16,
         "change_pct": -88.36, "extended_hours_change_pct": -88.36, "source": "yahoo_extended_hours",
         "data_freshness": "live_extended_hours"}
    M.restate_quote(q, *hit)
    assert q["prev_close"] == 94.395 and q["regular_session_close"] == 94.395
    assert round(q["change_pct"], 1) == -6.9 and round(q["extended_hours_change_pct"], 1) == -6.9
    assert q["split_adjusted"] == "8-for-1 split" and "755.16" in q["split_note"]
    assert price_block.quote_line(q, "PRE-MARKET") == (
        "→ **SNDK** **$87.88** pre-market, -6.9% from the last close. "
        "The last close is restated for the 8-for-1 split.")


def test_a_real_crash_or_an_ordinary_move_is_left_alone():
    # an ordinary session: never checked
    assert M.split_base(98.0, 100.0, 12.5) is None
    # a real -55%: the history agrees with the feed's previous close
    assert M.split_base(45.0, 100.0, 100.0) is None
    assert M.split_base(45.0, 100.0, 99.2) is None
    # the history is off by a split factor and the move is still not ordinary on it
    assert M.split_base(45.0, 100.0, 12.5) is None
    # no history
    assert M.split_base(45.0, 100.0, None) is None
    q = {"symbol": "X", "price": 98.0, "change_pct": -2.0, "data_freshness": "live_regular_session"}
    assert price_block.quote_line(q) == "→ **X** at **$98.00**, -2.0% today."


def test_reverse_and_fractional_splits_are_named():
    assert M.split_label(8.0) == "8-for-1 split" and M.split_label(1.5) == "3-for-2 split"
    assert M.split_label(0.1) == "1-for-10 reverse split" and M.split_label(0.05) == "1-for-20 reverse split"
    assert M.split_label(6.37) == "stock split"
    # a 1-for-10 reverse split: the feed's previous close is a tenth of the restated one
    hit = M.split_base(21.0, 2.0, 20.0)
    assert hit == (20.0, "1-for-10 reverse split")
    q = {"symbol": "RVS", "price": 21.0, "prev_close": 2.0, "change_pct": 950.0, "source": "finnhub"}
    M.restate_quote(q, *hit)
    assert round(q["change_pct"], 1) == 5.0


def test_after_hours_the_sessions_move_and_the_late_move_are_both_restated():
    # split effective today: today's close 95 is post-split, yesterday's 755.16 is not
    q = {"symbol": "SNDK", "price": 96.0, "prev_close": 755.16, "regular_session_close": 95.0,
         "change_pct": -87.4, "change_incl_after_hours_pct": -87.3, "extended_hours_change_pct": 1.05,
         "source": "yahoo_extended_hours"}
    M.restate_quote(q, *M.split_base(96.0, 755.16, 94.395))
    assert q["regular_session_close"] == 95.0                       # today's close was already post-split
    assert round(q["change_pct"], 1) == 0.6 and round(q["change_incl_after_hours_pct"], 1) == 1.7
    assert round(q["extended_hours_change_pct"], 2) == 1.05


def test_the_restated_close_is_the_last_bar_before_today(monkeypatch):
    bars = [{"date": "2026-11-18", "close": 93.1}, {"date": "2026-11-19", "close": 94.395},
            {"date": "2026-11-20", "close": 88.0}]
    monkeypatch.setattr(M, "fetch_price_history", lambda sym, start, **kw: bars)
    now = datetime(2026, 11, 20, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    assert M.restated_last_close("SNDK", now) == 94.395
    monkeypatch.setattr(M, "fetch_price_history", lambda sym, start, **kw: None)
    assert M.restated_last_close("SNDK", now) is None
