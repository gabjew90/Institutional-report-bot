"""Smoke: price history must never emit NaN (invalid JSON -> API 400).

2026-07-29 live failure: "analyze trades opened by analysts relative to
qqq" died twice with

  400 INVALID_ARGUMENT: Invalid JSON payload received. Unexpected token.
  26-07-28", "close": NaN, "volume": 47342

yfinance returns NaN for a non-trading day or an incomplete current
bar. json.dumps writes that as bare `NaN`, which is NOT valid JSON, so
the function_response part was malformed and Gemini rejected the whole
request — surfacing to the user as "Something about that question broke
the model."

Every numeric field must be a real float or the bar is dropped.
"""

import math
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


class _FakeRow(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class _FakeHist:
    """Minimal stand-in for a yfinance DataFrame."""

    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def iterrows(self):
        for idx, r in self._rows:
            yield idx, r


def _hist_with_nan():
    nan = float("nan")
    return _FakeHist([
        ("2026-07-27", _FakeRow(Open=100.0, High=101.0, Low=99.0,
                                Close=100.5, Volume=1000)),
        # holiday / incomplete bar — yfinance yields NaN
        ("2026-07-28", _FakeRow(Open=nan, High=nan, Low=nan,
                                Close=nan, Volume=47342)),
        ("2026-07-29", _FakeRow(Open=102.0, High=103.0, Low=101.0,
                                Close=102.5, Volume=1200)),
    ])


def test_nan_bars_dropped():
    import types
    from report import market_data as md

    class _T:
        def history(self, **kw):
            return _hist_with_nan()

    fake_yf = types.SimpleNamespace(Ticker=lambda s: _T())
    with patch.dict(sys.modules, {"yfinance": fake_yf}):
        out = md.fetch_price_history("QQQ", "2026-07-27")
    assert out, "should still return the good bars"
    dates = [b["date"] for b in out]
    assert "2026-07-28" not in dates, f"NaN bar must be dropped: {out}"
    assert len(out) == 2, out
    for b in out:
        for k in ("open", "high", "low", "close"):
            assert isinstance(b[k], float) and not math.isnan(b[k]), b
    _ok("NaN bars dropped; remaining values are real floats")


def test_result_is_json_serializable_strictly():
    """json.dumps(allow_nan=False) is what a strict JSON consumer does —
    the Gemini API rejects bare NaN. This must not raise."""
    import json
    from report import market_data as md
    import types

    class _T:
        def history(self, **kw):
            return _hist_with_nan()

    fake_yf = types.SimpleNamespace(Ticker=lambda s: _T())
    with patch.dict(sys.modules, {"yfinance": fake_yf}):
        out = md.fetch_price_history("QQQ", "2026-07-27")
    json.dumps({"points": out}, allow_nan=False)  # raises if any NaN
    _ok("payload survives strict JSON serialization (no NaN)")


def test_all_nan_returns_none():
    from report import market_data as md
    import types
    nan = float("nan")

    class _T:
        def history(self, **kw):
            return _FakeHist([
                ("2026-07-28", _FakeRow(Open=nan, High=nan, Low=nan,
                                        Close=nan, Volume=0)),
            ])

    fake_yf = types.SimpleNamespace(Ticker=lambda s: _T())
    with patch.dict(sys.modules, {"yfinance": fake_yf}):
        out = md.fetch_price_history("ZZZZ", "2026-07-27")
    assert out is None, f"all-NaN history must read as no data: {out}"
    _ok("all-NaN history returns None (clean no_data, not a broken payload)")


def test_loop_wide_json_scrub():
    """Backstop: ANY tool result passes through _json_safe, so a future
    tool can't reintroduce the 400."""
    import json
    from discord_bot.bot import _json_safe
    nan = float("nan")
    dirty = {"a": nan, "b": [1.0, nan, {"c": float("inf")}], "d": "ok"}
    clean = _json_safe(dirty)
    json.dumps(clean, allow_nan=False)  # raises if anything survived
    assert clean["a"] is None and clean["b"][1] is None
    assert clean["b"][2]["c"] is None and clean["d"] == "ok"
    import inspect
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_json_safe(result)" in src, (
        "tool loop must scrub every result before sending"
    )
    _ok("loop-wide _json_safe scrub applied to all tool results")


if __name__ == "__main__":
    print("=== price history NaN smoke ===")
    test_nan_bars_dropped()
    test_result_is_json_serializable_strictly()
    test_all_nan_returns_none()
    test_loop_wide_json_scrub()
    print("\nALL PRICE HISTORY NAN SMOKE TESTS PASS")
