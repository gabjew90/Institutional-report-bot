"""analyst_log.strike_check: a strike the underlying cannot have is not a
trade (2026-10-05 audit: QQQ 165c with QQQ near 750, SPX 370c, NDX 7685c,
SOXL 1.0c in the ledger)."""
from unittest.mock import patch

from analyst_log import strike_check as SC

PRICES = {"QQQ": 750.0, "SOXL": 160.0, "SPX": 7700.0, "NDX": 27000.0, "RUT": 2500.0,
          "XSP": 770.0, "SPY": 770.0}


def _x(ticker, strike, ctype="call"):
    return {"is_trade_screenshot": True, "ticker": ticker, "strike": strike,
            "contract_type": ctype, "action": "open", "notes": "member batch"}


def _apply(x, prices=PRICES):
    with patch.object(SC, "price_on", side_effect=lambda t, d: prices.get(t)):
        return SC.apply(x, "2026-10-05T18:14:22+00:00")


def test_an_impossible_strike_is_not_a_trade():
    out = _apply(_x("QQQ", 165))
    assert out["is_trade_screenshot"] is False
    assert "165" in out["what_it_appears_to_be"]
    assert _apply(_x("SOXL", 1.0))["is_trade_screenshot"] is False


def test_a_possible_strike_is_kept():
    assert _apply(_x("SOXL", 165))["is_trade_screenshot"] is True
    assert _apply(_x("QQQ", 752, "put"))["is_trade_screenshot"] is True
    # far out-of-the-money lottos stay inside the band
    assert _apply(_x("QQQ", 1000))["is_trade_screenshot"] is True


def test_an_index_strike_moves_to_the_index_it_fits():
    out = _apply(_x("NDX", 7685))
    assert out["is_trade_screenshot"] is True and out["ticker"] == "SPX"
    assert "moved from NDX" in out["notes"]
    # 370 fits no index: rejected, not moved
    assert _apply(_x("SPX", 370))["is_trade_screenshot"] is False


def test_an_index_level_strike_under_an_etf_moves_too():
    out = _apply(_x("SPY", 7685))
    assert out["is_trade_screenshot"] is True and out["ticker"] == "SPX"
    # BK's QQQ 165 fits no index and is still rejected
    assert _apply(_x("QQQ", 165))["is_trade_screenshot"] is False


def test_crypto_and_futures_roots_are_never_priced_from_yahoo():
    """Yahoo's BTC is a trust near $50 and ES is Eversource."""
    with patch.dict("os.environ", {"STRIKE_CHECK_OFFLINE": "0"}), \
         patch("report.market_data.fetch_price_history", side_effect=AssertionError("priced")):
        assert SC.price_on("BTC", "2026-10-05") is None
        assert SC.price_on("ES", "2026-10-05") is None


def test_a_failed_lookup_is_retried_after_a_while():
    calls = []

    def fake(sym, start, end):
        calls.append(sym)
        return None

    SC._cache.clear()
    with patch.dict("os.environ", {"STRIKE_CHECK_OFFLINE": "0"}), \
         patch("report.market_data.fetch_price_history", side_effect=fake):
        assert SC.price_on("QQQ", "2026-10-05") is None
        assert SC.price_on("QQQ", "2026-10-05") is None
        assert len(calls) == 1, "a miss is cached briefly"
        SC._cache[("QQQ", "2026-10-05")] = (None, 0.0)
        assert SC.price_on("QQQ", "2026-10-05") is None
        assert len(calls) == 2, "and retried once it is old"
    SC._cache.clear()


def test_an_unknown_price_keeps_the_trade():
    assert _apply(_x("QQQ", 165), prices={})["is_trade_screenshot"] is True


def test_stock_and_non_trades_are_untouched():
    stock = {"is_trade_screenshot": True, "ticker": "ARM", "strike": None,
             "contract_type": "stock", "action": "open"}
    assert _apply(dict(stock)) == stock
    junk = {"is_trade_screenshot": False}
    assert _apply(dict(junk)) == junk


def test_offline_mode_never_prices():
    with patch.dict("os.environ", {"STRIKE_CHECK_OFFLINE": "1"}):
        assert SC.price_on("QQQ", "2026-10-05") is None


def test_xsp_is_a_tenth_of_spx():
    with patch("report.market_data.fetch_price_history",
               return_value=[{"date": "2026-10-05", "close": 7700.0}]), \
         patch.dict("os.environ", {"STRIKE_CHECK_OFFLINE": "0"}):
        SC._cache.clear()
        assert SC.price_on("XSP", "2026-10-05") == 770.0
        assert SC.price_on("SPXW", "2026-10-05") == 7700.0
    SC._cache.clear()
