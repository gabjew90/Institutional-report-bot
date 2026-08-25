"""Smoke: earnings implied move (ATM straddle) — 2026-08-25.

The whole value of this column is that a printed number is real. These
tests pin the honesty guards: no one-sided quotes, no absurd spreads,
no expiry that predates the print, no out-of-band results, and every
failure path returns None so the sheet renders a dash instead of a
fabricated move.
"""

import sys
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _chain(spot=100.0, expirations=("2026-08-28",), exp="2026-08-28",
           call=None, put=None, strike=100.0):
    call = {"strike": strike, "bid": 3.0, "ask": 3.2} if call is None else call
    put = {"strike": strike, "bid": 2.8, "ask": 3.0} if put is None else put
    return {
        "underlying_symbol": "TEST",
        "underlying_spot_price": spot,
        "expiration_dates": list(expirations),
        "chain": {"expiration_iso": exp, "calls": [call], "puts": [put]},
        "source": "yahoo",
    }


def _run(raw, date="2026-08-26"):
    from report import implied_move as im
    import report.market_data as md
    with patch.object(md, "_fetch_yahoo_options_chain",
                      lambda sym, expiration_iso=None: raw):
        return im.implied_move_pct("TEST", date)


def test_happy_path():
    # (3.1 + 2.9) / 100 = 6.0%
    assert _run(_chain()) == 6.0, _run(_chain())
    _ok("straddle math: call mid + put mid over spot")


def test_rejects_one_sided_and_wide_quotes():
    zero_bid = _run(_chain(call={"strike": 100.0, "bid": 0.0, "ask": 4.0}))
    assert zero_bid is None, f"zero bid must not price: {zero_bid}"
    missing = _run(_chain(put={"strike": 100.0, "bid": None, "ask": 3.0}))
    assert missing is None, "missing quote must not price"
    # spread wider than the mid is a guess, not a price
    wide = _run(_chain(call={"strike": 100.0, "bid": 0.5, "ask": 6.0}))
    assert wide is None, f"absurd spread must not price: {wide}"
    _ok("illiquid chains: zero bid / missing / absurd spread all -> None")


def test_expiry_must_cover_the_print():
    # only expiry is BEFORE the report date
    stale = _run(_chain(expirations=("2026-08-21",), exp="2026-08-21"),
                 date="2026-08-26")
    assert stale is None, "an expiry before the print prices another event"
    _ok("expiry selection: pre-print expiries rejected")


def test_sanity_band():
    tiny = _run(_chain(call={"strike": 100.0, "bid": 0.10, "ask": 0.12},
                       put={"strike": 100.0, "bid": 0.10, "ask": 0.12}))
    assert tiny is None, "sub-1% on an earnings print is a stale chain"
    huge = _run(_chain(call={"strike": 100.0, "bid": 25.0, "ask": 26.0},
                       put={"strike": 100.0, "bid": 25.0, "ask": 26.0}))
    assert huge is None, ">40% is a broken quote for a calendar sheet"
    _ok("sanity band: sub-1% and >40% rejected")


def test_total_on_failure():
    from report import implied_move as im
    import report.market_data as md
    with patch.object(md, "_fetch_yahoo_options_chain",
                      side_effect=RuntimeError("yahoo 429")):
        assert im.implied_move_pct("TEST", "2026-08-26") is None
    with patch.object(md, "_fetch_yahoo_options_chain", lambda *a, **k: None):
        assert im.implied_move_pct("TEST", "2026-08-26") is None
    # no spot, no expirations, empty chain
    assert _run(_chain(spot=0)) is None
    assert _run(_chain(expirations=())) is None
    _ok("failure paths: exception / None / no spot / no expiries -> None")


def test_batch_skips_bad_symbols():
    from report import implied_move as im
    calls = []

    def _one(sym, date):
        calls.append(sym)
        if sym == "BAD":
            raise RuntimeError("boom")
        return 5.0

    with patch.object(im, "implied_move_pct", _one):
        out = im.implied_moves_for(["A", "BAD", "C"], "2026-08-26",
                                   pace_seconds=0)
    assert out == {"A": 5.0, "C": 5.0}, out
    assert calls == ["A", "BAD", "C"], "one bad symbol must not abort"
    _ok("batch: bad symbol skipped, run continues")


def test_renderer_shows_dash_and_legend():
    import inspect
    from report import calendar_render as cr
    src = inspect.getsource(cr.render_calendar_png)
    assert '"—"' in src, "unpriced names must render a dash"
    assert "implied_move" in src, "renderer must read the field"
    assert "ATM STRADDLE" in src, "legend names the method"
    _ok("renderer: dash for unpriced, straddle legend present")


if __name__ == "__main__":
    print("=== implied move smoke ===")
    test_happy_path()
    test_rejects_one_sided_and_wide_quotes()
    test_expiry_must_cover_the_print()
    test_sanity_band()
    test_total_on_failure()
    test_batch_skips_bad_symbols()
    test_renderer_shows_dash_and_legend()
    print("\nALL IMPLIED-MOVE SMOKE TESTS PASS")
