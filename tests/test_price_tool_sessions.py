"""lookup_market_price: the pre-market move and the Yahoo fallback.

2026-10-18 audit A14: before the open the move was measured from the close
two sessions back (QQQ "-3.2% pre-market" at $719.99 when it was -1.23% from
the 10/14 close of $728.95). A9: SpaceX (SPCX) had no Finnhub quote and was
called "not a recognized US stock".
"""
import asyncio
from datetime import datetime
from unittest.mock import patch

from discord_bot import ask_tools as T
from report import market_data as MD


def _run(symbols, session, yahoo, finnhub=None):
    with patch.object(MD, "_session_label", return_value=(session, "")), \
         patch.object(MD, "_fetch_yahoo_extended_hours", side_effect=lambda s: yahoo.get(s)), \
         patch.object(MD, "_fetch_finnhub_quote", side_effect=lambda s: (finnhub or {}).get(s)), \
         patch.object(T, "_crypto_quote", return_value=None):
        return asyncio.run(T._execute_market_price({"symbols": symbols}))


def _quote(res, sym):
    return next(q for q in res["quotes"] if q["symbol"] == sym)


def test_pre_market_move_is_from_the_latest_close():
    yahoo = {"QQQ": {"last_price": 719.99, "last_session": "pre",
                     "regular_close": 728.95, "prev_close": 743.57}}
    q = _quote(_run(["QQQ"], "PRE-MARKET", yahoo), "QQQ")
    assert round(q["change_pct"], 2) == -1.23
    assert q["prev_close"] == 728.95


def test_after_hours_change_is_the_session_and_the_full_move_is_separate():
    """2026-10-26 audit A26: change_pct after hours is the regular session's
    own move; the move including the after-hours print has its own field."""
    yahoo = {"GTLB": {"last_price": 90.0, "last_session": "post",
                      "regular_close": 100.0, "prev_close": 95.0}}
    q = _quote(_run(["GTLB"], "AFTER-HOURS", yahoo), "GTLB")
    assert round(q["change_pct"], 2) == round((100 - 95) / 95 * 100, 2)
    assert round(q["change_incl_after_hours_pct"], 2) == round((90 - 95) / 95 * 100, 2)
    assert round(q["extended_hours_change_pct"], 1) == -10.0


def test_a_listing_finnhub_lacks_is_priced_from_yahoo():
    yahoo = {"SPCX": {"last_price": 146.03, "last_session": "regular",
                      "regular_close": 146.03, "prev_close": 140.0}}
    q = _quote(_run(["SPCX"], "OPEN", yahoo), "SPCX")
    assert q["price"] == 146.03 and q["source"] == "yahoo" and "error" not in q


def test_no_quote_anywhere_is_still_an_error():
    q = _quote(_run(["ZZZZ"], "OPEN", {}), "ZZZZ")
    assert "error" in q


def test_volume_against_the_average_mid_session():
    """2026-10-18 audit A15: a volume question had no volume figure."""
    from zoneinfo import ZoneInfo
    bars = [{"date": f"2026-09-{d:02d}", "volume": 50_000_000} for d in range(1, 21)]
    bars.append({"date": "2026-10-13", "volume": 30_000_000})
    now = datetime(2026, 10, 13, 11, 6, tzinfo=ZoneInfo("America/New_York"))
    with patch.object(MD, "fetch_price_history", return_value=bars):
        v = MD.volume_vs_average("SPY", now=now)
    assert v["avg_20d"] == 50_000_000 and v["pct_of_avg"] == 60
    assert v["session_minutes_elapsed"] == 96 and "so far today" in v["note"]


def test_with_volume_adds_it_to_each_quote():
    yahoo = {"SPY": {"last_price": 600.0, "last_session": "regular",
                     "regular_close": 600.0, "prev_close": 598.0}}
    with patch.object(MD, "volume_vs_average", return_value={"volume": 1, "avg_20d": 2}):
        with patch.object(MD, "_session_label", return_value=("OPEN", "")), \
             patch.object(MD, "_fetch_yahoo_extended_hours", side_effect=lambda s: yahoo.get(s)), \
             patch.object(MD, "_fetch_finnhub_quote", return_value={"price": 600.0, "change_pct": 0.3, "prev_close": 598.0}), \
             patch.object(T, "_crypto_quote", return_value=None):
            res = asyncio.run(T._execute_market_price({"symbols": ["SPY"], "with_volume": True}))
    assert res["quotes"][0]["volume"] == {"volume": 1, "avg_20d": 2}
