"""Returns after dated events, computed in code (2026-10-26 audit A19)."""
import asyncio
from datetime import date, timedelta

from discord_bot import ask_tools as T
from report import market_data as md


def _hist(n=40, start=date(2026, 8, 3)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(), "close": 100.0 + len(out)})
        d += timedelta(days=1)
    return out


def test_base_is_the_close_before_the_date():
    r = T.returns_after(_hist(), "2026-08-06")
    assert r["base_date"] == "2026-08-05" and r["base_close"] == 102.0
    assert r["1_week"] == {"date": "2026-08-12", "close": 107.0, "return_pct": 4.9}
    assert r["1_month"]["date"] == "2026-09-03"


def test_a_horizon_not_reached_says_so():
    r = T.returns_after(_hist(10), "2026-08-12")
    assert r["1_month"].startswith("not yet")


def test_executor_returns_event_returns(monkeypatch):
    monkeypatch.setattr(md, "fetch_price_history", lambda s, a, b=None, i="1d": _hist())
    out = asyncio.run(T._execute_price_history({"symbol": "SPCX", "event_dates": ["2026-08-06", "junk"]}))
    assert [r["date"] for r in out["returns_after_dates"]] == ["2026-08-06"]
    assert "points" not in out
