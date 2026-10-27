"""Returns after dated events: dates by search, returns from the closes,
both in code (2026-10-27)."""
import asyncio
from datetime import date

from discord_bot import ask_router as R
from discord_bot import event_returns as E

REPLY = """2026-10-22: Q3 earnings tranche, 300M shares, after the close (Reuters)
2026-08-06: 912M shares unlocked — first lock-up expiry (fool.com)
Some preamble the model added
2027-01-01: future date that has not happened (x.com)
2026-08-06: duplicate line (y.com)"""


def test_parse_keeps_dated_past_lines_newest_first():
    ev = E.parse_events(REPLY, date(2026, 10, 27), "unlocks")
    assert [e["date"] for e in ev] == ["2026-10-22", "2026-08-06"]
    assert ev[0]["after_close"] is True and ev[1]["after_close"] is False
    assert ev[1]["label"] == "912M shares unlocked, first lock-up expiry"
    assert E.parse_events("2026-09-17: rate decision (fed.gov)", date(2026, 10, 27), "fomc")[0]["after_close"]


def test_route_prefetches_event_returns():
    r = R.classify("what were the 1 week and 1 month returns after the last few $SPCX unlocks")
    assert r.shape == R.HISTORICAL_STAT and r.google_allowed()
    assert r.prefetch == [(R.T_EVENTS, {"symbol": "SPCX", "event": "unlocks", "count": 4})]


def _hist():
    # 30 sessions of closes, +1 a day from 100, starting Aug 3
    from datetime import timedelta
    d, out, px = date(2026, 8, 3), [], 100.0
    while len(out) < 30:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": px})
            px += 1
        d += timedelta(days=1)
    return out


def test_executor_and_block(monkeypatch):
    async def fake_search(sym, event, n, name):
        return "2026-08-06: 912M shares unlocked (fool.com)", [{"title": "fool.com", "url": "https://fool.com/x"}]
    monkeypatch.setattr(E, "_search", fake_search)
    from report import market_data
    monkeypatch.setattr(market_data, "fetch_price_history", lambda *a: _hist())
    E._CACHE.clear()
    out = asyncio.run(E._execute_event_returns({"symbol": "SPCX", "event": "unlocks", "count": 4}))
    assert out["status"] == "ok" and out["sources"][0]["url"] == "https://fool.com/x"
    ev = out["events"][0]
    # Aug 6 at the open: base is the Aug 5 close (102), a week later 107
    assert ev["base_date"] == "2026-08-05" and ev["1_week"]["return_pct"] == round((107 / 102 - 1) * 100, 1)
    lines = E.block_lines(out)
    assert lines == ["→ **Aug 6, 2026**, 912M shares unlocked: from $102.00 (Aug 5 close), "
                     "+1.0% after 1 day · +4.9% after 1 week · +20.6% after 1 month"]


def test_no_dates_is_no_data(monkeypatch):
    async def fake_search(sym, event, n, name):
        return "NONE FOUND", []
    monkeypatch.setattr(E, "_search", fake_search)
    E._CACHE.clear()
    out = asyncio.run(E._execute_event_returns({"symbol": "ZZZ", "event": "splits"}))
    assert out["status"] == "no_data" and "never guess dates" in out["error"]


def test_dates_without_links_are_flagged():
    out = {"events": [{"date": "2026-08-06", "label": "", "error": "x"}], "dates_confirmed": False}
    assert E.block_lines(out)[-1].startswith("→ The search behind these dates returned no links")
