"""The /ask news prefetch, the chain's implied move and the earnings date
in New York terms (2026-09-30: MU answered from pre-print previews after
the print; ACN's "121% IV"; a next-morning print called "today")."""
import asyncio
import types
from datetime import datetime, timezone

import pytest

from discord_bot import news_tool as N


@pytest.fixture(autouse=True)
def _fresh_cache():
    N._CACHE.clear()
    yield
    N._CACHE.clear()


from datetime import timedelta as _td
from zoneinfo import ZoneInfo as _Z

_TODAY = datetime.now(timezone.utc).astimezone(_Z("America/New_York")).date()
D = _TODAY.isoformat()                       # inside the news window
D2 = (_TODAY - _td(days=2)).isoformat()
OLD = (_TODAY - _td(days=30)).isoformat()    # outside it


def _resp(text, urls):
    chunks = [types.SimpleNamespace(web=types.SimpleNamespace(uri=u, title=t)) for t, u in urls]
    gm = types.SimpleNamespace(grounding_chunks=chunks)
    return types.SimpleNamespace(text=text, candidates=[types.SimpleNamespace(grounding_metadata=gm)])


class _Client:
    def __init__(self, resp=None, exc=None):
        async def gen(**kw):
            self.kw = kw
            if exc:
                raise exc
            return resp
        self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))


def test_a_grounded_digest_comes_back_with_its_sources(monkeypatch):
    c = _Client(_resp(f"{D} Micron Q4 EPS $33.42 vs $31.61 est (reuters.com)",
                      [("reuters.com", "https://r/1"), ("cnbc.com", "https://c/2"),
                       ("reuters.com", "https://r/1")]))
    monkeypatch.setattr(N, "_get_client", lambda: c)
    r = asyncio.run(N._execute_ticker_news({"symbol": "$mu"}))
    assert r["status"] == "ok" and r["symbol"] == "MU" and "$33.42" in r["digest"]
    assert [s["url"] for s in r["sources"]] == ["https://r/1", "https://c/2"]
    assert "MU" in c.kw["contents"] and c.kw["config"].tools[0].google_search is not None


def test_lines_dated_this_week_count_even_when_the_api_drops_the_links(monkeypatch):
    c = _Client(_resp(f"- {D}: Micron EPS $33.42 vs $31.61 est (GlobeNewswire)", []))
    monkeypatch.setattr(N, "_get_client", lambda: c)
    r = asyncio.run(N._execute_ticker_news({"symbol": "MU"}))
    assert r["status"] == "ok" and r["sources"] == [] and "$33.42" in r["digest"]


def test_a_miss_is_cached_briefly_and_unlinked_news_is_flagged(monkeypatch):
    import time as _t
    c = _Client(_resp("NO RECENT NEWS", []))
    monkeypatch.setattr(N, "_get_client", lambda: c)
    assert asyncio.run(N._execute_ticker_news({"symbol": "QUIET"}))["status"] == "no_data"
    age = _t.monotonic() - N._CACHE["QUIET"][0]
    assert N.CACHE_TTL_S - age <= N.MISS_TTL_S + 1, "a miss expires in about two minutes"
    from discord_bot import ask_router as R
    block = R.inject_text(R.T_NEWS, {"status": "ok", "symbol": "MU", "digest": f"{D} beat",
                                     "sources": []})
    assert "returned no links" in block


def test_an_error_is_retried_once(monkeypatch):
    replies = [RuntimeError("503"), _resp(f"{D} beat (x)", [("x", "https://x")])]

    class _Flaky:
        def __init__(self):
            async def gen(**kw):
                r = replies.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))
    monkeypatch.setattr(N, "_get_client", lambda: _Flaky())
    assert asyncio.run(N._execute_ticker_news({"symbol": "MU"}))["status"] == "ok"


def test_no_news_an_undated_or_old_reply_and_a_failure_are_not_news(monkeypatch):
    for client in (_Client(_resp("NO RECENT NEWS", [("x", "https://x")])),
                   _Client(_resp("Micron did great", [])),           # undated = memory
                   _Client(_resp(f"{OLD} Micron launched a chip (x)", [("x", "https://x")])),
                   _Client(exc=RuntimeError("500"))):
        monkeypatch.setattr(N, "_get_client", lambda c=client: c)
        assert asyncio.run(N._execute_ticker_news({"symbol": "MU"}))["status"] in ("no_data", "error")
    assert asyncio.run(N._execute_ticker_news({}))["status"] == "error"


def test_a_no_news_category_does_not_erase_the_dated_items():
    text = (f"- {D2}: Nvidia authorized a $150B buyback (NVIDIA)\n"
            "- earnings: NO RECENT NEWS\n"
            f"- {OLD}: last month's launch, outside the window\n"
            f"- {D2}: Nvidia introduced OASP (Forkast.News)")
    assert N.dated_lines(text) == [f"- {D2}: Nvidia authorized a $150B buyback (NVIDIA)",
                                   f"- {D2}: Nvidia introduced OASP (Forkast.News)"]
    assert N.dated_lines("NO RECENT NEWS") == []
    assert N._clip("a\nbb\nccc", limit=5) == "a\nbb"


def test_the_news_is_cached_per_symbol(monkeypatch):
    N._CACHE.clear()
    c = _Client(_resp(f"{D} beat (x)", [("x", "https://x")]))
    calls = []
    monkeypatch.setattr(N, "_get_client", lambda: calls.append(1) or c)
    asyncio.run(N._execute_ticker_news({"symbol": "MU"}))
    asyncio.run(N._execute_ticker_news({"symbol": "mu"}))
    assert len(calls) == 1
    N._CACHE.clear()


def test_news_links_are_cited_only_when_the_answer_uses_the_news():
    from discord_bot import data_footer as F
    news = {"digest": "2026-09-30 Micron EPS $33.42 vs $31.61 est (Reuters)",
            "sources": [{"title": "reuters.com", "url": "https://r/1"}]}
    trace = [{"tool": "lookup_research", "status": "ok"}, {"tool": "ticker_news", "status": "ok"}]
    used = F.compose("", "→ **MU** beat: EPS **$33.42** vs **$31.61**", news, trace)
    assert used.startswith("\n\nSources:\n[1] [reuters.com](<https://r/1>)\nData: ")
    assert "news search (Google)" in used and used.count("Data:") == 1
    ignored = F.compose("", "→ Goldman sees a 2026 beat on HBM", news, trace)
    assert "Sources" not in ignored and ignored.startswith("\n\nData: bank research notes")
    # Google sources keep the Data line beneath them (2026-10-08)
    grounded = F.compose("\n\nSources:\n[1] g", "x", news, trace)
    assert grounded.startswith("\n\nSources:\n[1] g\nData: ") and grounded.count("Data:") == 1
    assert F.compose("", "x", None, []) == ""


def test_a_date_lookup_skips_the_news_search_but_a_result_question_gets_it():
    from discord_bot import ask_router as R
    assert R.T_NEWS not in [t for t, _ in R.classify("when does NVDA report").prefetch]
    assert R.T_NEWS in [t for t, _ in R.classify("did PLTR beat last quarter").prefetch]


def test_the_news_block_reads_as_dated_lines():
    from discord_bot import ask_router as R
    block = R.inject_text(R.T_NEWS, {"status": "ok", "symbol": "MU", "digest": "2026-09-30 beat",
                                     "sources": [{"title": "reuters.com", "url": "u"}]})
    assert block.startswith("[RECENT NEWS ON THE TICKER") and "supersedes any bank note" in block
    assert "NEWS ON MU:\n2026-09-30 beat\n(searched: reuters.com)" in block


def test_the_chain_summary_carries_the_straddle_move():
    from report.market_data import summarize_options_chain
    raw = {"underlying_spot_price": 183.0, "chain": {"expiration_iso": "2026-10-02",
           "calls": [{"strike": 180, "bid": 9.0, "ask": 9.4, "volume": 1, "openInterest": 1,
                      "impliedVolatility": 1.2},
                     {"strike": 185, "bid": 6.0, "ask": 6.4, "volume": 1, "openInterest": 1,
                      "impliedVolatility": 1.2}],
           "puts": [{"strike": 185, "bid": 7.8, "ask": 8.2, "volume": 1, "openInterest": 1,
                     "impliedVolatility": 1.2}]}}
    s = summarize_options_chain(raw)
    assert s["atm_strike"] == 185 and s["implied_move_dollars"] == 14.2
    assert s["implied_move_pct"] == 7.8
    raw["chain"]["puts"][0]["bid"] = 0          # one-sided quote: no move
    assert "implied_move_pct" not in summarize_options_chain(raw)


def test_earnings_dates_are_said_in_new_york_terms():
    from discord_bot.ask_tools import _relative_day_et
    late_evening_et = datetime(2026, 10, 1, 2, 45, tzinfo=timezone.utc)   # 22:45 ET on 9/30
    assert _relative_day_et("2026-10-01", "before market open", now=late_evening_et) == \
        "tomorrow (Thu Oct 1), before market open"
    assert _relative_day_et("2026-09-30", now=late_evening_et) == "today (Wed Sep 30)"
    assert _relative_day_et("2026-10-08", now=late_evening_et).startswith("in 8 days")
    assert _relative_day_et("2026-09-25", now=late_evening_et).startswith("5 days ago")


def test_a_report_earlier_today_reads_as_released():
    """2026-10-07: APLD and LEVI were called "reports today after the
    close" 10-16 minutes after they reported."""
    from datetime import datetime, timezone
    from discord_bot.ask_tools import _relative_day_et as f
    after = datetime(2026, 10, 7, 20, 20, tzinfo=timezone.utc)   # 4:20 PM ET
    assert "already released" in f("2026-10-07", "after market close", now=after)
    before = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)   # 3:00 PM ET
    assert "already released" not in f("2026-10-07", "after market close", now=before)
    assert "already released" in f("2026-10-07", "before market open", now=before)
