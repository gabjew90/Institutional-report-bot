"""A print that already happened (2026-10-01): MU was answered twice from
the banks' 'into the print' previews after it reported, and an ACN options
answer quoted Yahoo's overnight zeros as 'ATM IV 0.8%, zero open interest'."""
import asyncio
import types
from datetime import date
from unittest.mock import patch

from discord_bot import ask_router as R

SNAP = {"status": "ok", "symbol": "MU", "growth": {"last_report": {
    "date": "2026-09-30", "session": "after the close", "actual": 33.42, "estimate": 31.82,
    "surprise_pct": 5.0}}}


def test_a_report_in_the_last_three_days_is_fresh():
    f = R.fresh_print(SNAP, today=date(2026, 10, 1))
    assert f["actual"] == 33.42 and f["symbol"] == "MU"
    assert R.fresh_print(SNAP, today=date(2026, 10, 5)) is None
    assert R.fresh_print({"growth": {"last_report": {"date": "2026-09-30", "actual": 1.0}}},
                         today=date(2026, 10, 1)) is None, "no estimate, nothing to set it against"


def test_notes_written_before_the_report_are_marked_previews():
    fresh = R.fresh_print(SNAP, today=date(2026, 10, 1))
    research = {"status": "ok", "symbol": "MU", "days": 14, "banks": ["JPMorgan", "Citi"],
                "fresh_print": fresh, "notes": [
                    {"source": "JPMorgan", "published": "2026-09-29", "title": "Into the print"},
                    {"source": "Goldman Sachs", "published": "2026-09-30", "title": "Same-day preview"},
                    {"source": "Citi", "published": "2026-10-01", "title": "Post-print"}]}
    text = R.render_research(research)
    assert "RESULTS ARE OUT: MU reported 2026-09-30 after the close: EPS $33.42 vs $31.82" in text
    assert "JPMorgan (2026-09-29) PREVIEW, written before the report" in text
    assert "Goldman Sachs (2026-09-30) PREVIEW" in text, "same day as an after-close print"
    assert "Citi (2026-10-01): Post-print" in text
    before_open = {**fresh, "session": "before the open"}
    assert not R._is_preview({"published": "2026-09-30"}, before_open)


class _Client:
    def __init__(self, text):
        self.calls = 0

        async def gen(**kw):
            self.calls += 1
            return types.SimpleNamespace(text=text)
        self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))


def _run(answer, client, shape="ticker_opinion"):
    from discord_bot import bot
    from google.genai import types as gt
    meta = {"guards": [], "route_shape": shape,
            "fresh_print": R.fresh_print(SNAP, today=date(2026, 10, 1))}
    out = asyncio.run(bot._fresh_print_guard(answer, meta, client, "m", None, gt, lambda r: None))
    return out, meta["guards"]


PREVIEW_ANSWER = ("→ **$MU** closed at **$1,065.11** heading into the print.\n\n→ JPMorgan expects "
                  "the guide well above the Street's **$56.3B** revenue and **$35.71** EPS.")


def test_an_answer_without_the_printed_eps_is_rewritten_to_lead_with_it():
    led = ("→ **$MU** reported after the close: EPS **$33.42** vs **$31.82** expected (+5.0%). "
           "Stock **$1,065.11**.\n\n→ JPMorgan had expected the guide above the Street's "
           "**$56.3B** revenue and **$35.71** EPS.")
    c = _Client(led)
    out, guards = _run(PREVIEW_ANSWER, c)
    assert out == led and guards == ["fresh-print"]


def test_the_print_check_needs_the_exact_figure_and_stays_off_without_a_fresh_print():
    c = _Client("x")
    ok = "→ MU beat: **$33.42** vs **$31.82**, revenue $54.2B"
    assert _run(ok, c)[0] == ok
    assert _run(PREVIEW_ANSWER, c, shape="price")[0] == PREVIEW_ANSWER
    assert c.calls == 0
    rounded = "→ MU earned about **$33** a share, revenue $54.2B"
    c2 = _Client(rounded)                       # the rewrite still lacks $33.42
    out, guards = _run(rounded, c2)
    assert out == rounded and guards[-1] == "fresh-print:kept-original"


def test_a_next_date_lookup_is_not_rewritten_but_a_result_question_is():
    from discord_bot import bot
    from google.genai import types as gt
    c = _Client("→ MU reported **$33.42** vs **$31.82**. Next report Dec 17, $61.3B")
    for prefetch, calls in ((["lookup_earnings_date", "lookup_ticker_snapshot"], 0),
                            (["lookup_earnings_date", "ticker_news"], 1)):
        meta = {"guards": [], "route_shape": "earnings_date", "route_prefetch": prefetch,
                "fresh_print": R.fresh_print(SNAP, today=date(2026, 10, 1))}
        asyncio.run(bot._fresh_print_guard("→ MU reports next on Dec 17, consensus $61.3B",
                                           meta, c, "m", None, gt, lambda r: None))
        assert c.calls == calls


def test_a_release_yahoo_has_not_filled_yet_still_marks_previews():
    from datetime import datetime, timezone
    snap = {"symbol": "ACN", "growth": {
        "last_report": {"date": "2026-06-18", "actual": 3.80, "estimate": 3.71},
        "next_report": {"date": "2026-10-01", "session": "before the open", "estimate": 3.18}}}
    before = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)     # 08:00 ET
    after = datetime(2026, 10, 1, 14, 0, tzinfo=timezone.utc)      # 10:00 ET
    assert R.fresh_print(snap, now=before) is None
    f = R.fresh_print(snap, now=after)
    assert f["actual"] is None and f["date"] == "2026-10-01"
    text = R.render_research({"symbol": "ACN", "fresh_print": f, "banks": ["Goldman Sachs"],
                              "notes": [{"source": "Goldman Sachs", "published": "2026-09-28",
                                         "title": "ACN preview"}]})
    assert "figures are not in the data yet" in text and "PREVIEW" in text


def test_a_nine_oclock_release_is_before_the_open():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from report.ticker_snapshot import _session
    ny = ZoneInfo("America/New_York")
    assert _session(datetime(2026, 10, 1, 9, 0, tzinfo=ny)) == "before the open"
    assert _session(datetime(2026, 10, 1, 12, 0, tzinfo=ny)) == "during the session"
    assert _session(datetime(2026, 10, 1, 16, 5, tzinfo=ny)) == "after the close"


def _chain(bid):
    return {"underlying_symbol": "ACN", "underlying_spot_price": 183.37,
            "expiration_dates": ["2026-10-02"],
            "chain": {"expiration_iso": "2026-10-02",
                      "calls": [{"strike": 185, "bid": bid, "ask": bid + 0.4 if bid else 0,
                                 "openInterest": 900 if bid else 0, "volume": 6847,
                                 "impliedVolatility": 1.21 if bid else 1e-05}],
                      "puts": [{"strike": 185, "bid": bid, "ask": bid + 0.4 if bid else 0,
                                "openInterest": 800 if bid else 0, "volume": 7958,
                                "impliedVolatility": 1.21 if bid else 1e-05}]}}


def test_an_overnight_chain_is_not_read_as_data():
    from report.market_data import summarize_options_chain
    s = summarize_options_chain(_chain(0))
    assert s["live_quotes"] is False and s["atm_iv"] is None and s["call_oi"] is None
    assert s["call_volume"] == 6847, "the last session's volume stays"
    assert "implied_move_pct" not in s


def test_the_last_live_chain_is_served_with_its_time_when_quotes_stop():
    from discord_bot import ask_tools as T
    T._LAST_LIVE_CHAIN.clear()
    with patch("report.market_data._fetch_yahoo_options_chain", return_value=_chain(0)):
        cold = asyncio.run(T._execute_options_chain({"symbol": "ACN"}))
    assert "not quoting right now" in cold["quotes_note"] and cold["summary"]["atm_iv"] is None
    with patch("report.market_data._fetch_yahoo_options_chain", return_value=_chain(6.5)):
        live = asyncio.run(T._execute_options_chain({"symbol": "ACN"}))
    assert live["summary"]["live_quotes"] and "quotes_note" not in live
    with patch("report.market_data._fetch_yahoo_options_chain", return_value=_chain(0)):
        night = asyncio.run(T._execute_options_chain({"symbol": "ACN"}))
    assert night["summary"]["implied_move_pct"] == live["summary"]["implied_move_pct"]
    assert night["as_of"] == live["as_of"] and "last live quotes" in night["quotes_note"]
    T._LAST_LIVE_CHAIN.clear()
