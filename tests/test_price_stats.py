"""Price statistics computed in code (2026-11-21): the parse, the
arithmetic and the posted lines."""
import asyncio
from datetime import date, timedelta

import pytest

from discord_bot import answer_block as A
from discord_bot import ask_router as R
from discord_bot import price_stats as P

TODAY = date(2026, 11, 21)


def _bars(n=300, start=date(2025, 9, 1), px=100.0, step=1.0):
    """Weekday bars, the close rising by `step` a session."""
    d, out = start, []
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "open": px, "high": px + 0.5, "low": px - 0.5, "close": px})
            px += step
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------ parse

@pytest.mark.parametrize("q, tickers, want", [
    ("200 day moving on btc is what", ["BTC"],
     {"symbol": "BTC", "series": "BTC-USD", "name": "Bitcoin", "kind": "average", "n": 200, "unit": "day",
      "ema": False}),
    ("where is nvda vs its 50dma", ["NVDA"],
     {"symbol": "NVDA", "kind": "average", "n": 50, "unit": "day"}),
    ("what's the 200 week moving average on bitcoin", [],
     {"symbol": "BTC", "series": "BTC-USD", "kind": "average", "n": 200, "unit": "week"}),
    ("what is the 9 ema on QQQ", ["QQQ"], {"kind": "average", "n": 9, "ema": True}),
    ("GOOGL 200ma", ["GOOGL"], {"kind": "average", "n": 200}),
    ("What is the average return for the Nasdaq on Thursdays in 2026", [],
     {"symbol": "^IXIC", "name": "Nasdaq Composite", "kind": "weekday", "weekday": 3,
      "start": "2026-01-01", "end": None, "label": "in 2026"}),
    ("average SPY move on fridays in 2025", ["SPY"],
     {"kind": "weekday", "weekday": 4, "start": "2025-01-01", "end": "2025-12-31", "label": "in 2025"}),
    ("how does NVDA usually do on mondays", ["NVDA"],
     {"kind": "weekday", "weekday": 0, "start": "2025-11-21", "label": "over the last year"}),
    ("in the last 21 days how much % has NVDA dropped", ["NVDA"],
     {"kind": "change", "start": "2026-10-31", "label": "over the last 21 days"}),
    ("5 year return on amazon", ["AMZN"], {"kind": "change", "start": "2021-11-21", "label": "over the last 5 years"}),
    ("TSLA 6 month return", ["TSLA"], {"kind": "change", "start": "2026-05-21"}),
    ("NVDA return over the last 30 sessions", ["NVDA"], {"kind": "change", "sessions": 30}),
    ("what is SPY ytd", ["SPY"], {"kind": "change", "start": "2026-01-01", "before": True, "label": "this year"}),
    ("how much is the s&p up this year", [], {"symbol": "^GSPC", "name": "S&P 500", "kind": "change", "before": True}),
    ("how much is btc down this month", ["BTC"], {"series": "BTC-USD", "start": "2026-11-01", "before": True}),
    # "last year" is the calendar year before this one, "the last year" the trailing twelve months
    ("how did NVDA do last year", ["NVDA"],
     {"start": "2025-01-01", "before": True, "end": "2025-12-31", "label": "last year (2025)"}),
    ("how has NVDA done over the last year", ["NVDA"], {"start": "2025-11-21", "end": None}),
    ("how did NVDA do last week", ["NVDA"],
     {"start": "2026-11-09", "before": True, "end": "2026-11-15", "label": "last week (Nov 9 to Nov 13)"}),
    ("how much did SPY gain last month", ["SPY"], {"start": "2026-10-01", "end": "2026-10-31", "label": "last month (October)"}),
    ("how did SPY do in 2022", ["SPY"], {"start": "2022-01-01", "before": True, "end": "2022-12-31", "label": "in 2022"}),
    ("how much is SPY up since the end of last year", ["SPY"], {"start": "2026-01-01", "before": True, "end": None}),
    # a window named beside a year: the window
    ("TSLA 3 month return, worst stretch in 2026", ["TSLA"], {"start": "2026-08-21", "label": "over the last 3 months"}),
    ("how has NVDA done since january", ["NVDA"], {"kind": "change", "start": "2026-01-01", "before": True}),
    ("how much has NVDA dropped since october 15", ["NVDA"], {"start": "2026-10-15", "label": "since Oct 15"}),
    ("how much has NVDA dropped since december 3", ["NVDA"], {"start": "2025-12-03", "label": "since Dec 3, 2025"}),
    ("what percent range has the nasdaq been trading in since August 5th", [],
     {"symbol": "^IXIC", "kind": "change", "start": "2026-08-05", "range_first": True}),
    ("how has RDDT done since ipo", ["RDDT"], {"kind": "change", "max": True}),
])
def test_parse_reads_the_statistic_and_its_window(q, tickers, want):
    got = P.parse(q, tickers, TODAY)
    assert got, q
    for k, v in want.items():
        assert got.get(k) == v, (q, k, got)


@pytest.mark.parametrize("q, tickers", [
    ("will NVDA be up in 2 weeks", ["NVDA"]),                      # looks ahead
    ("how much mcap has NVDA lost this last week", ["NVDA"]),       # another quantity
    ("am i up this week on NVDA calls", ["NVDA"]),                  # the asker's position
    ("Have I moved up or down in slurs ranking in the last month", ["SLURS"]),  # an unlisted token
    ("who is up the most this week", []),                           # nothing named
    ("NVDA or AMD this year, which is up more", ["NVDA", "AMD"]),   # a comparison
    ("what does NVDA do", ["NVDA"]),                                # no statistic
    ("NVDA earnings this week", ["NVDA"]),                          # a window, no change word
    ("spy 2 ma", ["SPY"]),                                          # not a moving-average length
])
def test_parse_leaves_other_questions_alone(q, tickers):
    assert P.parse(q, tickers, TODAY) is None, q


def test_router_sends_a_statistic_to_the_prefetch_and_keeps_the_others():
    r = R.classify("what is the average qqq return on Thursdays for 2026")
    assert r.shape == R.PRICE_HISTORY and r.reason == "price statistic" and r.tickers == ["QQQ"]
    assert [t for t, _ in r.prefetch] == [R.T_STATS] and not r.google_allowed()
    # an index read by name is no ticker for the turn after
    assert R.classify("how much is the s&p up this year").tickers == []
    # a question that names a member is about the member
    assert R.classify("how much is NVDA up this month", names_member=True).reason != "price statistic"
    # the two index shapes that also say "for the year" keep their route
    assert R.classify("What would the sp500 gain for the year be if you removed the top 10 stocks"
                      ).reason == "index return without its top 10"
    assert R.classify("how has rddt performed last 3 earnings").prefetch[0][0] == R.T_EVENTS
    assert R.classify("tell me how far off all time highs for WDC and MU").reason == "all-time high"


# ---------------------------------------------------------------- compute

def test_moving_average_and_where_the_price_sits():
    bars = _bars(300)                       # closes 100 .. 399
    m = P.moving_average(bars, 200)
    assert m["value"] == 299.5 and m["last"] == 399.0 and m["pct_vs"] == 33.2
    assert P.moving_average(bars, 50)["value"] == 374.5
    assert "error" in P.moving_average(bars[:20], 50)
    flat = _bars(60, step=0.0)
    assert P.moving_average(flat, 20, ema=True)["value"] == 100.0
    # an exponential average sits nearer the latest closes than a simple one
    assert P.moving_average(bars, 20, ema=True)["value"] > P.moving_average(bars, 20)["value"] - 1


def test_change_bases_on_the_last_close_on_or_before_the_start():
    bars = _bars(30, start=date(2026, 10, 12))          # Mon Oct 12 = 100, +1 a session
    # Saturday Oct 31: the base is Friday Oct 30's close
    c = P.change(bars, start="2026-10-31")
    assert c["base_date"] == "2026-10-30" and c["base"] == 114.0
    assert c["last"] == 129.0 and c["pct"] == 13.2 and c["sessions"] == 15
    assert c["low_date"] == "2026-11-02" and c["low"] == 114.5 and c["high"] == 129.5
    # the start of a month: the last close before it
    assert P.change(bars, start="2026-11-02", before=True)["base_date"] == "2026-10-30"
    assert P.change(bars, start="2026-11-02")["base_date"] == "2026-11-02"
    # sessions count bars
    s = P.change(bars, sessions=5)
    assert s["base"] == 124.0 and s["sessions"] == 5
    # a window that starts before the history: the first close, and it says so
    early = P.change(bars, start="2020-01-01")
    assert early["base_date"] == "2026-10-12" and early["from_first_bar"] is True
    assert "error" in P.change(bars, start="2027-01-01") and "error" in P.change(bars, sessions=99)
    assert "error" in P.change(bars)
    # a window that has ended: the last close is the last one inside it
    done = P.change(bars, start="2026-10-19", before=True, end="2026-10-25")
    assert done["base_date"] == "2026-10-16" and done["last_date"] == "2026-10-23" and done["pct"] == 4.8


def test_weekday_moves_average_one_weekday():
    # +1% every Thursday, flat otherwise
    d, px, bars = date(2026, 1, 1), 100.0, []
    while d <= date(2026, 3, 31):
        if d.weekday() < 5:
            if d.weekday() == 3 and bars:
                px = round(px * 1.01, 6)
            bars.append({"date": d.isoformat(), "close": px, "high": px, "low": px})
        d += timedelta(days=1)
    w = P.weekday_moves(bars, 3, "2026-01-01")
    assert w["avg_pct"] == 1.0 and w["median_pct"] == 1.0 and w["up"] == w["count"] and w["down"] == 0
    assert w["count"] == 12 and w["first_day"] == "2026-01-08"      # Jan 1 has no prior close
    assert P.weekday_moves(bars, 0, "2026-01-01")["avg_pct"] == 0.0
    assert P.weekday_moves(bars, 3, "2026-01-01", "2026-01-31")["count"] == 4
    assert "error" in P.weekday_moves(bars, 5, "2026-01-01")


def test_execute_fetches_unadjusted_closes_once(monkeypatch):
    from report import market_data
    seen = []

    def fake(symbol, start, end=None, interval="1d", adjust=True):
        seen.append((symbol, interval, adjust))
        return _bars(300)

    monkeypatch.setattr(market_data, "fetch_price_history", fake)
    spec = P.parse("200 day moving on btc is what", ["BTC"], TODAY)
    r = asyncio.run(P._execute_price_stats(spec))
    assert r["status"] == "ok" and r["value"] == 299.5 and r["symbol"] == "BTC" and r["series"] == "BTC-USD"
    assert seen == [("BTC-USD", "1d", False)]
    monkeypatch.setattr(market_data, "fetch_price_history", lambda *a: None)
    assert asyncio.run(P._execute_price_stats(spec))["status"] == "no_data"
    monkeypatch.setattr(market_data, "fetch_price_history", lambda *a: _bars(20))
    assert asyncio.run(P._execute_price_stats(spec))["status"] == "no_data"      # 20 closes, 200 needed
    assert asyncio.run(P._execute_price_stats({}))["status"] == "error"


# ------------------------------------------------------------------ lines

def test_lines_for_each_statistic():
    avg = {"status": "ok", "symbol": "BTC-USD", "name": "Bitcoin", "kind": "average", "n": 200, "unit": "day",
           "value": 72425.49, "last": 59709.98, "last_date": "2026-11-21", "pct_vs": -17.6, "in_session": True}
    assert P.block_lines(avg, TODAY) == [
        "→ **Bitcoin** 200-day moving average: **$72,425.49**, from the last 200 daily closes through Nov 21",
        "→ Price now: **$59,709.98**, **17.6% below** the average"]
    wk = {**avg, "unit": "week", "in_session": False}
    assert "weekly closes, this week's included" in P.block_lines(wk, TODAY)[0]
    assert P.block_lines(wk, TODAY)[1].startswith("→ Latest price: ")

    chg = {"status": "ok", "symbol": "NVDA", "name": "NVDA", "kind": "change", "label": "over the last 21 days",
           "base_date": "2026-10-30", "base": 204.64, "last_date": "2026-11-20", "last": 125.53, "pct": -38.7,
           "low": 124.5, "low_date": "2026-11-20", "high": 209.98, "high_date": "2026-11-02", "range_pct": 68.7}
    assert P.block_lines(chg, TODAY) == [
        "→ **NVDA** is down **38.7%** over the last 21 days: **$204.64** at the Oct 30 close to **$125.53** "
        "at the Nov 20 close",
        "→ Low since then **$124.50** (Nov 20), high **$209.98** (Nov 2)"]
    # an index in points, the range first when the range was asked, a year on an older date
    rng = {**chg, "symbol": "^IXIC", "name": "Nasdaq Composite", "label": "since Aug 5", "range_first": True,
           "base_date": "2025-08-05"}
    lines = P.block_lines(rng, TODAY)
    assert lines[0] == ("→ **Nasdaq Composite** has traded between **124.50** (Nov 20) and **209.98** (Nov 2) "
                        "since Aug 5, **68.7%** from low to high")
    assert "at the Aug 5, 2025 close" in lines[1] and "$" not in lines[1]
    # a stock over six months or more says dividends are not counted. An index does not.
    long = {**chg, "base_date": "2025-12-31", "label": "this year"}
    assert P.block_lines(long, TODAY)[-1] == "→ Price change only. Dividends are not counted."
    # a window that has ended is told in the past
    past = P.block_lines({**chg, "end": "2026-11-15", "label": "last week (Nov 9 to Nov 13)"}, TODAY)
    assert past[0].startswith("→ **NVDA** was down **38.7%** last week (Nov 9 to Nov 13): ")
    assert past[1].startswith("→ Low in that stretch ")
    assert len(lines) == 2

    wd = {"status": "ok", "symbol": "QQQ", "name": "QQQ", "kind": "weekday", "weekday": 3, "label": "in 2026",
          "count": 46, "avg_pct": -0.26, "median_pct": -0.31, "up": 19, "down": 27,
          "best": {"date": "2026-06-11", "pct": 3.4}, "worst": {"date": "2026-11-05", "pct": -6.5},
          "first_day": "2026-01-08", "last_day": "2026-11-19"}
    assert P.block_lines(wd, TODAY) == [
        "→ **QQQ** on Thursdays in 2026: average move **-0.26%**, median **-0.31%**, close to close over "
        "46 Thursdays (Jan 8 to Nov 19)",
        "→ Up on 19, down on 27. Best **+3.4%** (Jun 11), worst **-6.5%** (Nov 5)"]


def test_the_answer_block_posts_the_lines_and_drops_an_opening_with_its_own_figure():
    res = {"status": "ok", "symbol": "QQQ", "name": "QQQ", "kind": "weekday", "weekday": 3, "label": "in 2026",
           "count": 46, "avg_pct": -0.26, "median_pct": -0.31, "up": 19, "down": 27,
           "best": {"date": "2026-06-11", "pct": 3.4}, "worst": {"date": "2026-11-05", "pct": -6.5},
           "first_day": "2026-01-08", "last_day": "2026-11-19"}
    blk = A.build("price_stats", res, TODAY)
    assert blk and len(blk["lines"]) == 2
    out, used = A.compose("→ Thursdays have leaned red this year.", [blk], "average qqq return on Thursdays")
    assert used and out.startswith("→ Thursdays have leaned red this year.") and blk["lines"][0] in out
    out, _ = A.compose("→ QQQ averages **-1.9%** on Thursdays.", [blk], "average qqq return on Thursdays")
    assert "-1.9%" not in out and blk["lines"][1] in out
    assert A.build("price_stats", {"status": "no_data", "error": "x"}, TODAY) is None
