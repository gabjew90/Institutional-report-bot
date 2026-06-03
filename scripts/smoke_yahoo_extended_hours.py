"""Smoke test for the Yahoo extended-hours quote path.

Covers:
  - _fetch_yahoo_extended_hours: parses minute bars, classifies the
    last bar's session, returns the proper shape
  - _execute_market_price: prefers Yahoo on AFTER-HOURS / PRE-MARKET
    when Yahoo returns a matching-session bar; falls back to Finnhub
    when Yahoo errors or returns a regular-session bar
  - per-symbol data_freshness tags
  - stock_quote_data_caveat is DROPPED when every stock has live AH
    data, and NARROWED when only some symbols fell back to Finnhub
"""

import asyncio
import sys
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Yahoo response shape: minute timestamps + close array + trading
# periods classifying each bar.
def _make_yahoo_response(closes, timestamps, periods):
    return {
        "chart": {
            "result": [{
                "meta": {
                    "regularMarketPrice": 100.0,
                    "previousClose": 95.0,
                    "tradingPeriods": periods,
                },
                "timestamp": timestamps,
                "indicators": {
                    "quote": [{"close": closes}],
                },
            }]
        }
    }


def test_yahoo_helper_classifies_post_session():
    """A bar after the regular session end should be classified as 'post'."""
    from report.market_data import _fetch_yahoo_extended_hours
    # Trading periods: regular 1000-1300, post 1300-1500
    periods = {
        "pre":     [[{"start": 800,  "end": 1000}]],
        "regular": [[{"start": 1000, "end": 1300}]],
        "post":    [[{"start": 1300, "end": 1500}]],
    }
    # Bars: one in regular at 1200, one in post at 1450
    timestamps = [1200, 1450]
    closes = [99.0, 97.5]

    fake = _make_yahoo_response(closes, timestamps, periods)

    def fake_urlopen(req, timeout=4.0):
        class R:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner):
                import json as _json
                return _json.dumps(fake).encode()
        return R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _fetch_yahoo_extended_hours("TSLA")
    assert out is not None, "expected a dict"
    assert out["last_session"] == "post", out
    assert out["last_price"] == 97.5, out
    assert out["regular_close"] == 100.0
    _ok("yahoo helper: classifies post-market bar correctly")


def test_yahoo_helper_walks_past_null_bars():
    """Null closes at the tail (no print in that minute) should be
    skipped to find the most recent real print."""
    from report.market_data import _fetch_yahoo_extended_hours
    periods = {
        "pre":     [[{"start": 800,  "end": 1000}]],
        "regular": [[{"start": 1000, "end": 1300}]],
        "post":    [[{"start": 1300, "end": 1500}]],
    }
    timestamps = [1200, 1400, 1450, 1490]
    closes = [99.0, 97.5, None, None]  # trailing nulls

    fake = _make_yahoo_response(closes, timestamps, periods)

    def fake_urlopen(req, timeout=4.0):
        class R:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def read(self_inner):
                import json as _json
                return _json.dumps(fake).encode()
        return R()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _fetch_yahoo_extended_hours("TSLA")
    assert out["last_price"] == 97.5, out
    assert out["last_session"] == "post", out
    _ok("yahoo helper: skips trailing null bars to find latest real print")


def test_yahoo_helper_returns_none_on_fetch_failure():
    """Yahoo 401/timeout/etc. should yield None so caller falls back."""
    from report.market_data import _fetch_yahoo_extended_hours

    def fake_urlopen(req, timeout=4.0):
        raise RuntimeError("simulated 429")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        out = _fetch_yahoo_extended_hours("TSLA")
    assert out is None
    _ok("yahoo helper: returns None on fetch failure")


def test_executor_prefers_yahoo_on_after_hours():
    """During AFTER-HOURS, the executor must call Yahoo first and use
    its last_price (not Finnhub's regular close) when session matches."""
    import discord_bot.bot as bot_mod

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED — after-hours.")

    fake_yahoo = {
        "last_price": 42.50,
        "last_timestamp": 9999,
        "last_session": "post",
        "regular_close": 45.00,
        "prev_close": 44.00,
        "source": "yahoo",
    }

    finnhub_called = []

    def fake_finnhub(sym):
        finnhub_called.append(sym)
        return {"price": 45.00, "prev_close": 44.00, "change_pct": 2.27}

    with (
        patch("report.market_data._session_label", side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=fake_yahoo),
        patch("report.market_data._fetch_finnhub_quote", side_effect=fake_finnhub),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["GTLB"]}))

    assert finnhub_called == [], (
        f"Finnhub should NOT have been called when Yahoo returned matching session, "
        f"but was called for: {finnhub_called}"
    )
    quote = result["quotes"][0]
    assert quote["source"] == "yahoo_extended_hours"
    assert quote["price"] == 42.50, quote
    assert quote["data_freshness"] == "live_extended_hours"
    assert quote.get("regular_session_close") == 45.00
    assert quote.get("extended_hours_change_pct") is not None
    _ok("executor: uses Yahoo AH price when session matches (no Finnhub fallback)")


def test_executor_falls_back_when_yahoo_session_mismatches():
    """If Yahoo returns a 'regular' bar during AFTER-HOURS (no AH trades
    happened, or stock is illiquid), the executor must NOT use it — that
    would just give us the same regular close Finnhub returns, without
    the proper caveat. Fall back to Finnhub + keep the caveat."""
    import discord_bot.bot as bot_mod

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED — after-hours.")

    fake_yahoo = {
        "last_price": 45.00,
        "last_timestamp": 9999,
        "last_session": "regular",  # ← mismatch: no AH print available
        "regular_close": 45.00,
        "prev_close": 44.00,
        "source": "yahoo",
    }

    finnhub_calls = []

    def fake_finnhub(sym):
        finnhub_calls.append(sym)
        return {"price": 45.00, "prev_close": 44.00, "change_pct": 2.27}

    with (
        patch("report.market_data._session_label", side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=fake_yahoo),
        patch("report.market_data._fetch_finnhub_quote", side_effect=fake_finnhub),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["ILLIQ"]}))

    assert finnhub_calls == ["ILLIQ"], (
        "executor should fall back to Finnhub when Yahoo's last bar is regular-session"
    )
    quote = result["quotes"][0]
    assert quote["source"] == "finnhub"
    assert quote["data_freshness"] == "regular_session_close"
    _ok("executor: falls back to Finnhub when Yahoo has no AH/PRE print")


def test_executor_falls_back_on_yahoo_error():
    """Yahoo raise / None should silently fall back to Finnhub."""
    import discord_bot.bot as bot_mod

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED — after-hours.")

    def fake_finnhub(sym):
        return {"price": 100.0, "prev_close": 99.0, "change_pct": 1.0}

    with (
        patch("report.market_data._session_label", side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=None),
        patch("report.market_data._fetch_finnhub_quote", side_effect=fake_finnhub),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["X"]}))

    assert result["quotes"][0]["source"] == "finnhub"
    _ok("executor: falls back to Finnhub when Yahoo helper returns None")


def test_caveat_dropped_when_all_stocks_have_live_ah():
    """When every stock symbol in the batch gets live AH data from Yahoo,
    the stock_quote_data_caveat is irrelevant — must be dropped."""
    import discord_bot.bot as bot_mod

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED — after-hours.")

    fake_yahoo = {
        "last_price": 42.50, "last_timestamp": 9999,
        "last_session": "post", "regular_close": 45.0,
        "prev_close": 44.0, "source": "yahoo",
    }

    with (
        patch("report.market_data._session_label", side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              return_value=fake_yahoo),
    ):
        result = asyncio.run(bot_mod._execute_market_price(
            {"symbols": ["TSLA", "GTLB"]}
        ))

    assert result.get("stock_quote_data_caveat") is None, (
        f"caveat should be None when all stocks have live AH data, "
        f"got: {result.get('stock_quote_data_caveat')!r}"
    )
    _ok("caveat dropped when ALL stocks have live extended-hours data")


def test_caveat_narrows_to_stale_symbols_only():
    """When some stocks have live AH and others fell back to Finnhub,
    the caveat should NAME the fallback symbols rather than blanket-disclaim."""
    import discord_bot.bot as bot_mod

    def fake_session_label(_now_et):
        return ("AFTER-HOURS", "Markets CLOSED — after-hours.")

    def fake_yahoo(sym):
        if sym == "TSLA":
            return {
                "last_price": 421.0, "last_timestamp": 9999,
                "last_session": "post", "regular_close": 423.0,
                "prev_close": 415.0, "source": "yahoo",
            }
        # GTLB: simulate Yahoo failure
        return None

    def fake_finnhub(sym):
        return {"price": 31.0, "prev_close": 33.0, "change_pct": -6.0}

    with (
        patch("report.market_data._session_label", side_effect=fake_session_label),
        patch("report.market_data._fetch_yahoo_extended_hours",
              side_effect=fake_yahoo),
        patch("report.market_data._fetch_finnhub_quote", side_effect=fake_finnhub),
    ):
        result = asyncio.run(bot_mod._execute_market_price(
            {"symbols": ["TSLA", "GTLB"]}
        ))

    caveat = result.get("stock_quote_data_caveat") or ""
    assert "GTLB" in caveat, (
        f"caveat should name GTLB specifically (the stale one), got: {caveat!r}"
    )
    assert "TSLA" not in caveat, (
        f"caveat should NOT name TSLA (live AH), got: {caveat!r}"
    )
    _ok("caveat narrows to stale symbols only (TSLA live, GTLB stale)")


if __name__ == "__main__":
    print("=== Yahoo extended-hours smoke ===")
    test_yahoo_helper_classifies_post_session()
    test_yahoo_helper_walks_past_null_bars()
    test_yahoo_helper_returns_none_on_fetch_failure()
    test_executor_prefers_yahoo_on_after_hours()
    test_executor_falls_back_when_yahoo_session_mismatches()
    test_executor_falls_back_on_yahoo_error()
    test_caveat_dropped_when_all_stocks_have_live_ah()
    test_caveat_narrows_to_stale_symbols_only()
    print("\nALL YAHOO EXTENDED-HOURS SMOKE TESTS PASS")
