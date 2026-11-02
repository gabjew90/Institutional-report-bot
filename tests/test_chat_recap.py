"""Chat recaps read the whole window in code (discord_bot/chat_recap.py)."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from discord_bot import ask_router as R
from discord_bot import chat_recap as C


@pytest.mark.parametrize("q", [
    "summarize the chat for the last 6 hours", "recap the convo", "tldr of this channel",
    "catch me up", "what did i miss", "what'd i miss in here", "summary of the last 3 hours",
    "give me a recap of today", "summary of this chat for the last 3 hours",
])
def test_a_recap_question(q):
    assert C.asks(q), q


@pytest.mark.parametrize("q", [
    "summarize Owen's messages", "summarize what abe said in the last 6 hours",
    "give me quick summary of market today so far", "recap of earnings today", "summary of the fed today",
    "what did the fed say", "chat", "how many messages has BK sent today",
    "what did i miss on nvda", "recap today's games", "recap the game today", "summarize the call today",
    "summarize the article for the last hour",
])
def test_not_a_recap_question(q):
    assert not C.asks(q), q


@pytest.mark.parametrize("q,hours", [
    ("summarize the chat for the last 6 hours", 6), ("recap the past 2 hrs", 2), ("recap the last hour", 1),
    ("recap the last 30 minutes", 0.5), ("recap the last 2 days", 48), ("recap the last 9 days", 48),
    ("recap the chat", 6), ("recap the last few hours", 3),
])
def test_the_window(q, hours):
    assert C.window_hours(q) == hours


def test_today_is_since_midnight_new_york():
    now = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)          # 3:30 PM New York
    assert C.window_hours("recap today", now=now) == 15.5


def _row(aid, name, text, at="2026-11-02T15:00:00.000000+00:00", **kw):
    return {"author_id": aid, "author_username": name, "author_display": name, "content": text,
            "posted_at": at, "channel_name": "💬-stonks-yapping-💬", "image_ocr_text": None, **kw}


def test_prepare_drops_chart_commands_and_counts_by_author_id():
    rows = [_row(1, "BK", "NVDA looks weak"), _row(1, "Kyle", "Fc qqq 5"), _row(1, "Kyle", "$NVDA puts printed"),
            _row(2, "Sam", "AAPL or NVDA?"), _row(3, "Abe", ""), _row(4, "Wock", "", image_ocr_text="SPY 500P")]
    p = C.prepare(rows)
    assert len(p["lines"]) == 4 and p["people"] == 3
    assert all("Fc qqq" not in x["text"] for x in p["lines"])
    # one person under two names, shown under the latest
    assert p["top_talkers"][0] == {"name": "Kyle", "messages": 2}
    assert p["top_tickers"][0] == {"ticker": "NVDA", "messages": 3, "people": 2}
    assert "[image: SPY 500P]" in p["lines"][-1]["text"]


@pytest.mark.parametrize("text,want", [
    ("AAPL or NVDA?", {"AAPL", "NVDA"}), ("$nvda and $amd", {"NVDA", "AMD"}), ("I AM OUT", set()),
    ("ARE YOU KIDDING ME $TSLA", {"TSLA"}), ("lol ON IT", set()), ("nothing here", set()),
])
def test_symbols_a_line_writes(text, want):
    assert C._symbols(text) == want


def test_chunks_cover_every_line_in_order_and_stop_at_the_limit():
    lines = [{"at": str(i), "text": "x" * 290} for i in range(1500)]
    parts = C.chunks(lines)
    assert len(parts) <= C.MAX_CHUNKS
    assert [x["at"] for p in parts for x in p] == [str(i) for i in range(1500)]
    assert C.chunks([]) == []


def _run(rows, notes):
    with patch("db.chat_window", return_value=rows), \
            patch("discord_bot.bot._mask_slur_tokens", side_effect=lambda t: t), \
            patch.object(C, "_summarise", AsyncMock(side_effect=notes)):
        return asyncio.run(C._execute_chat_recap({"hours": 6, "channel_id": 5}))


def test_a_short_window_goes_to_the_writer_as_it_is():
    res = _run([_row(1, "BK", "NVDA looks weak")], [])
    assert res["status"] == "ok" and res["read"] == 1 and "NVDA looks weak" in res["messages"]
    assert "sections" not in res and res["channel"] == "stonks-yapping"


def test_a_long_window_is_summarised_in_stretches():
    rows = [_row(1, "BK", "y" * 280, at=f"2026-11-02T{14 + i // 200}:{i % 60:02d}:00+00:00") for i in range(1200)]
    res = _run(rows, lambda part: "notes")
    assert res["status"] == "ok" and res["read"] == 1200 and not res["gaps"] and res["sections"]


def test_a_stretch_that_fails_is_a_named_gap_not_counted_as_read():
    rows = [_row(1, "BK", "y" * 280, at=f"2026-11-02T{14 + i // 200}:{i % 60:02d}:00+00:00") for i in range(1200)]
    seen = []

    def one(part):
        seen.append(part)
        return "" if len(seen) == 1 else "notes"
    res = _run(rows, one)
    assert res["gaps"] and res["read"] < 1200
    assert "Could not read" in C.coverage_line(res)


def test_nothing_summarised_is_an_error_and_an_empty_window_says_so():
    rows = [_row(1, "BK", "y" * 280) for _ in range(100)]
    assert _run(rows, lambda part: "")["status"] == "error"
    assert _run([], [])["status"] == "empty"


def test_the_coverage_line():
    line = C.coverage_line({"read": 1161, "people": 23, "channel": "stonks-yapping", "start": "9:23 AM",
                            "end": "3:23 PM", "gaps": []})
    assert line == "→ Read **1,161** messages from **23** people in #stonks-yapping, 9:23 AM to 3:23 PM ET."
    assert "—" not in line and ";" not in line


def test_the_router_prefetches_the_window_for_this_channel():
    r = R.classify("summarize the chat for the last 6 hours", channel_id=77)
    assert r.shape == R.CHAT_HISTORY and r.is_factual
    assert r.prefetch == [(R.T_RECAP, {"hours": 6.0, "channel_id": 77})]


@pytest.mark.parametrize("q", ["summary of AMD for the last 3 hours", "what did i miss on NVDA earnings",
                               "recap $NVDA for the last hour"])
def test_a_stock_named_is_not_a_chat_recap(q):
    assert R.T_RECAP not in [t for t, _ in R.classify(q, channel_id=77).prefetch]


def test_a_channel_the_asker_names_wins():
    r = R.classify("recap <#123> for the last hour", channel_id=77)
    assert r.prefetch == [(R.T_RECAP, {"hours": 1.0, "channel_id": 123})]


def test_a_member_named_stays_with_chat_search():
    r = R.classify("summarize the chat about abe for the last 6 hours", channel_id=77, names_member=True)
    assert R.T_RECAP not in [t for t, _ in r.prefetch]


def test_a_roast_of_a_recap_stays_a_roast():
    from discord_bot import follow_up as F
    assert F.kind("Fuck you clanker shit recap") != F.COMPLAINT


def test_chat_search_cap_keeps_the_newest_rows():
    from discord_bot import ask_tools
    rows = [{"author_display": "x", "author_username": "x", "channel_name": "c",
             "content": f"m{i} " + "y" * 300, "posted_at": f"2026-11-02T{10 + i // 60}:{i % 60:02d}"}
            for i in range(199, -1, -1)]             # SQL gives newest first
    with patch.object(ask_tools.db, "search_chat_messages_for_ask", return_value=rows):
        res = asyncio.run(ask_tools._execute_chat_search(
            {"start_iso": "2026-11-02T10:00:00Z", "end_iso": "2026-11-02T14:00:00Z"}))
    got = [m["content"].split()[0] for m in res["matches"]]
    assert got[-1] == "m199" and got == sorted(got, key=lambda s: int(s[1:]))
    assert len(got) < 200
