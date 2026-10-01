"""The /ask ticker snapshot (2026-09-30): computed from Yahoo data, narrated
with dates, never graded (owner: "don't dictate any thresholds just narrate
the data")."""
import asyncio
import re
from datetime import date

from report import ticker_snapshot as S

INFO = {
    "quoteType": "EQUITY", "longName": "Aeva Technologies, Inc.", "sector": "Technology",
    "industry": "Scientific & Technical Instruments",
    "longBusinessSummary": ("Aeva Technologies, Inc. develops LiDAR sensing systems. "
                            "Its FMCW sensors measure velocity per point. It sells to "
                            "automotive and industrial customers. " + "Filler sentence. " * 40),
    "marketCap": 1.2e9, "currentPrice": 20.0, "fiftyTwoWeekHigh": 40.0, "fiftyTwoWeekLow": 10.0,
    "beta": 2.31, "floatShares": 30e6, "sharesOutstanding": 55e6, "shortPercentOfFloat": 0.183,
    "shortRatio": 5.12, "dateShortInterest": 1789430400, "totalCash": 3.0e8, "totalDebt": 1.0e7,
    "operatingCashflow": -1.2e8, "freeCashflow": -1.39e8,
}
# 3 completed sessions then the current one
BARS = [
    {"high": 11, "low": 9, "close": 10, "volume": 1_000_000},
    {"high": 12, "low": 10, "close": 11, "volume": 2_000_000},
    {"high": 13, "low": 10, "close": 12, "volume": 3_000_000},
    {"high": 21, "low": 12, "close": 20, "volume": 4_000_000},
]
FILINGS = [
    {"date": date(2026, 8, 6), "type": "10-Q"},
    {"date": date(2026, 6, 3), "type": "S-3ASR"},
    {"date": date(2026, 2, 1), "type": "424B5"},
    {"date": date(2026, 1, 5), "type": "424B2"},     # structured notes, not equity
    {"date": date(2025, 6, 1), "type": "S-1"},       # older than a year
]


def test_history_stats_atr_volume_and_relative_volume():
    h = S.history_stats(BARS)
    # true ranges: max(2,1,1)=2, max(3,2,1)=3, max(9,9,0)=9 -> mean 4.67
    assert h["atr"] == 4.67 and h["atr_days"] == 3 and h["atr_pct"] == 23.33
    # averages exclude the current session
    assert h["adv_shares"] == 2_000_000 and h["adv_days"] == 3
    assert h["adv_dollars"] == round((10e6 + 22e6 + 36e6) / 3)
    assert h["today_volume"] == 4_000_000 and h["relative_volume"] == 2.0


def test_offering_filings_keep_equity_registrations_from_the_last_year():
    got = S.offering_filings(FILINGS, today=date(2026, 9, 30))
    assert got == [{"date": "2026-06-03", "form": "S-3ASR"}, {"date": "2026-02-01", "form": "424B5"}]


def test_build_narrates_and_drops_absent_fields():
    snap = S.build("aeva", INFO, BARS, FILINGS, today=date(2026, 9, 30))
    assert snap["status"] == "ok" and snap["symbol"] == "AEVA"
    assert snap["pct_below_52w_high"] == 50.0 and snap["pct_above_52w_low"] == 100.0
    assert snap["short_pct_float"] == 18.3 and snap["short_interest_as_of"] == "2026-09-15"
    assert len(snap["business"]) <= S.SUMMARY_CHARS and snap["business"].endswith(".")
    lean = S.build("X", {"quoteType": "EQUITY", "longName": "X"}, [], [], today=date(2026, 9, 30))
    assert "beta" not in lean and lean["offering_filings_12m"] == []


def test_a_fund_is_not_a_stock():
    assert S.build("SPY", {"quoteType": "ETF", "longName": "SPDR"}, [], [])["status"] == "not_a_stock"


def test_render_is_narration_not_a_verdict():
    text = S.render(S.build("AEVA", INFO, BARS, FILINGS, today=date(2026, 9, 30)))
    assert "AEVA: Aeva Technologies, Inc. (Technology / Scientific & Technical Instruments)" in text
    assert "short interest 18.3% of float, 5.1 days to cover (exchange-reported as of 2026-09-15)" in text
    assert "S-3ASR on 2026-06-03" in text
    assert "free cash flow, trailing 12 months -$139.0M" in text
    assert not re.search(r"\b(low float|liquid|illiquid|squeeze|risky|safe|thin|heavy)\b", text, re.I)
    none = S.render(S.build("X", {"quoteType": "EQUITY", "longName": "X"}, [], [],
                            today=date(2026, 9, 30)))
    assert "offering registrations filed in the last 12 months: none" in none


def test_partial_volume_note_only_while_the_session_is_open(monkeypatch):
    from datetime import datetime, timezone
    assert S._market_open(datetime(2026, 9, 30, 15, 0, tzinfo=timezone.utc))       # 11:00 ET Wed
    assert not S._market_open(datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc))  # 22:30 ET
    assert not S._market_open(datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc))  # Saturday
    snap = S.build("AEVA", INFO, BARS, FILINGS, today=date(2026, 9, 30))
    monkeypatch.setattr(S, "_market_open", lambda now=None: False)
    assert "latest session 4.0M (2.0x the average)" in S.render(snap)
    monkeypatch.setattr(S, "_market_open", lambda now=None: True)
    assert "still open, so this is partial" in S.render(snap)


def test_growth_is_computed_and_rendered_growth_first():
    """Owner, 2026-10-01: show y/y growth, not only absolute dollars. ACN's
    live Yahoo figures that morning."""
    rev = {"0q": {"avg": 18037776350, "yearAgoRevenue": 17596260000, "growth": 0.0251,
                  "numberOfAnalysts": 20},
           "0y": {"avg": 73566672090, "yearAgoRevenue": 69672977000, "growth": 0.0559,
                  "numberOfAnalysts": 26}}
    eps = {"0q": {"avg": 3.18294, "yearAgoEps": 3.03, "growth": 0.0505, "numberOfAnalysts": 20}}
    hist = [{"date": "2026-06-18", "session": "before the open", "epsActual": 3.80, "epsEstimate": 3.70865, "surprisePercent": 0.0246},
            {"date": "2026-03-19", "session": "before the open", "epsActual": 2.93, "epsEstimate": 2.83739, "surprisePercent": 0.0326}]
    qrev = [("2026-05-31", 1.871814e10), ("2026-02-28", 1.804407e10), ("2025-11-30", 1.874212e10),
            ("2025-08-31", 1.759626e10), ("2025-05-31", 1.772787e10)]
    g = S.growth_block(rev, eps, hist, qrev)
    assert g["next_quarter"]["revenue_growth_pct"] == 2.5 and g["next_quarter"]["analysts"] == 20
    assert g["eps_reports"][0] == {"date": "2026-06-18", "session": "before the open", "actual": 3.80, "estimate": 3.70865,
                                   "surprise_pct": 2.5}
    assert g["revenue_reports"] == [{"quarter": "2026-05-31", "revenue": 1.871814e10, "yoy_pct": 5.6}]
    lines = S.render_growth(g)
    assert lines[0] == ("consensus, quarter to be reported next, 20 analysts: revenue +2.5% y/y "
                        "($18.0B vs $17.6B), EPS +5.1% y/y ($3.18 vs $3.03)")
    assert "reported 2026-06-18 before the open: $3.80 vs $3.71 (+2.5%)" in lines[2]
    assert S.growth_block({}, {}, [], []) == {} and S.render_growth({}) == []


def test_growth_from_a_loss_is_not_a_percent():
    g = S.growth_block({}, {"0q": {"avg": 0.20, "yearAgoEps": -0.10, "growth": -3.0}}, [], [])
    assert "eps_growth_pct" not in g["next_quarter"]
    assert "EPS n/a y/y ($0.20 vs $-0.10)" in S.render_growth(g)[0]


def test_a_quarter_that_just_reported_is_not_the_next_one():
    """Yahoo can lag after a print: '0q' still holds the reported quarter."""
    rev = {"0q": {"avg": 54e9, "yearAgoRevenue": 11.3e9, "growth": 3.8}}
    eps = {"0q": {"avg": 31.82, "yearAgoEps": 2.83, "growth": 10.2}}
    hist = [{"date": "2026-09-30", "session": "after the close", "epsActual": 33.42, "epsEstimate": 31.818, "surprisePercent": 0.05}]
    g = S.growth_block(rev, eps, hist, [], today=date(2026, 9, 30))
    assert "next_quarter" not in g and g["eps_reports"][0]["actual"] == 33.42
    rolled = S.growth_block(rev, {"0q": {"avg": 38.02, "yearAgoEps": 4.78, "growth": 6.95}}, hist, [],
                            today=date(2026, 9, 30))
    assert rolled["next_quarter"]["eps"] == 38.02


def test_injected_block_uses_the_rendered_text():
    from discord_bot import ask_router as R
    snap = S.build("AEVA", INFO, BARS, FILINGS, today=date(2026, 9, 30))
    block = R.inject_text(R.T_SNAPSHOT, snap)
    assert block.startswith("[TICKER SNAPSHOT") and "never grade" in block
    assert "S-3ASR on 2026-06-03" in block and '"status"' not in block


class _FakeTicker:
    calls = []

    def __init__(self, sym, info=None, fail_info=False, filings=None):
        self.sym, self._info, self._fail, self._filings = sym, info or {}, fail_info, filings or []

    @property
    def info(self):
        _FakeTicker.calls.append(("info", self.sym))
        if self._fail:
            raise RuntimeError("429")
        return self._info

    def history(self, **kw):
        import pandas as pd
        return pd.DataFrame([{"High": b["high"], "Low": b["low"], "Close": b["close"],
                              "Volume": b["volume"]} for b in BARS])

    @property
    def sec_filings(self):
        _FakeTicker.calls.append(("filings", self.sym))
        return self._filings


def _fake_yf(monkeypatch, **kw):
    import sys
    import types
    mod = types.SimpleNamespace(Ticker=lambda sym: _FakeTicker(sym, **kw))
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    S._CACHE.clear()
    _FakeTicker.calls = []


def test_fetch_caches_a_full_read_but_not_one_without_the_quote_summary(monkeypatch):
    _fake_yf(monkeypatch, fail_info=True)
    thin = S.fetch("aeva")
    assert thin["status"] == "ok" and "name" not in thin and "AEVA" not in S._CACHE
    _fake_yf(monkeypatch, info=INFO, filings=FILINGS)
    assert S.fetch("AEVA")["name"] == "Aeva Technologies, Inc." and "AEVA" in S._CACHE
    n = len(_FakeTicker.calls)
    S.fetch("AEVA")
    assert len(_FakeTicker.calls) == n, "served from the cache"


def test_fetch_skips_filings_for_a_fund_and_reports_an_unknown_symbol(monkeypatch):
    _fake_yf(monkeypatch, info={"quoteType": "ETF", "longName": "SPDR"})
    assert S.fetch("SPY")["status"] == "not_a_stock"
    assert ("filings", "SPY") not in _FakeTicker.calls
    assert S.build("APPLE", {"trailingPegRatio": None}, [], [])["status"] == "no_data"


def test_a_zero_volume_premarket_row_is_not_the_latest_session():
    h = S.history_stats(BARS + [{"high": 20, "low": 20, "close": 20, "volume": 0}])
    assert h["today_volume"] == 4_000_000 and h["atr_days"] == 3


def test_a_missing_snapshot_is_one_quiet_line():
    from discord_bot import ask_router as R
    line = R.inject_text(R.T_SNAPSHOT, {"status": "no_data", "symbol": "APPLE"})
    assert line.startswith("[TICKER SNAPSHOT: none for APPLE") and "\n" not in line


def test_executor_times_out_and_rejects_an_empty_symbol(monkeypatch):
    from discord_bot import snapshot_tool as T
    assert asyncio.run(T._execute_snapshot({}))["status"] == "error"
    monkeypatch.setattr(T, "SNAPSHOT_TIMEOUT_S", 0.01)
    import time
    monkeypatch.setattr(S, "fetch", lambda sym: time.sleep(0.2) or {"status": "ok"})
    assert asyncio.run(T._execute_snapshot({"symbol": "$mu"}))["status"] == "error"
