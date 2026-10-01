"""The /ask business primer (2026-09-30): built once per ticker from a
grounded search, stored 30 days, injected beside the snapshot."""
import asyncio
import sqlite3
import types
from unittest.mock import patch

import pytest

import db
from discord_bot import primer_tool as P

GOOD = ("1. **SELLS:** Memory chips to data centers and phone makers.\n"
        "SEGMENTS: Cloud Memory ~30% (HBM: high-bandwidth memory beside AI chips)\n"
        "- DRIVERS: Cloud Memory drives growth and carries the highest margin\n"
        "WATCHED: gross margin; the next quarter's revenue guide\n"
        "OUTSIDE: memory prices, hyperscaler spending\n"
        "Some trailing commentary.")


def _resp(text, n_sources=2):
    chunks = [types.SimpleNamespace(web=types.SimpleNamespace(uri=f"https://s/{i}", title=f"s{i}"))
              for i in range(n_sources)]
    return types.SimpleNamespace(text=text, candidates=[types.SimpleNamespace(
        grounding_metadata=types.SimpleNamespace(grounding_chunks=chunks))])


class _Client:
    def __init__(self, replies, delay=0.0):
        self.replies, self.calls = list(replies), 0

        async def gen(**kw):
            self.calls += 1
            if delay:
                await asyncio.sleep(delay)
            return self.replies.pop(0)
        self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE ticker_primers (symbol TEXT PRIMARY KEY, primer TEXT NOT NULL, "
              "sources TEXT, built_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S', 'now')))")
    return c


@pytest.fixture
def conn():
    c = _conn()
    with patch("db_parts.ask._db.get_connection", return_value=c):
        yield c


def test_the_parser_keeps_the_five_labels_whatever_the_formatting():
    out = P._parse(GOOD)
    assert out.splitlines() == [
        "SELLS: Memory chips to data centers and phone makers.",
        "SEGMENTS: Cloud Memory ~30% (HBM: high-bandwidth memory beside AI chips)",
        "DRIVERS: Cloud Memory drives growth and carries the highest margin",
        "WATCHED: gross margin; the next quarter's revenue guide",
        "OUTSIDE: memory prices, hyperscaler spending"]
    assert P._parse("SELLS: x\nnothing else") is None


def test_an_unsourced_reply_is_retried_and_never_stored(conn, monkeypatch):
    c = _Client([_resp(GOOD, 0), _resp(GOOD, 2)])
    monkeypatch.setattr(P, "_get_client", lambda: c)
    r = asyncio.run(P._build("MU", "Micron"))
    assert r["status"] == "ok" and c.calls == 2
    stored = db.get_ticker_primer("mu")
    assert stored["primer"].startswith("SELLS: Memory chips") and len(stored["sources"]) == 2
    c2 = _Client([_resp(GOOD, 0)] * P.BUILD_ATTEMPTS)
    monkeypatch.setattr(P, "_get_client", lambda: c2)
    assert asyncio.run(P._build("ZZZ", ""))["status"] == "no_data"
    assert db.get_ticker_primer("ZZZ") is None


def test_a_stored_primer_is_served_and_an_old_one_is_rebuilt(conn, monkeypatch):
    monkeypatch.setattr(P, "MIN_BUILT_AT", "2000-01-01T00:00:00")
    db.upsert_ticker_primer("NVDA", "SELLS: GPUs", [{"title": "t", "url": "u"}])
    c = _Client([])
    monkeypatch.setattr(P, "_get_client", lambda: c)
    r = asyncio.run(P._execute_ticker_primer({"symbol": "$nvda"}))
    assert r["status"] == "ok" and r["primer"] == "SELLS: GPUs" and c.calls == 0
    conn.execute("UPDATE ticker_primers SET built_at = '2026-01-01T00:00:00'")
    assert db.get_ticker_primer("NVDA") is None


def test_a_slow_build_returns_timeout_and_still_stores(conn, monkeypatch):
    c = _Client([_resp(GOOD, 2)], delay=0.3)
    monkeypatch.setattr(P, "_get_client", lambda: c)
    monkeypatch.setattr(P, "WAIT_S", 0.05)

    async def go():
        first = await P._execute_ticker_primer({"symbol": "AEVA"})
        await asyncio.sleep(0.5)               # the build finishes in the background
        return first
    assert asyncio.run(go())["status"] == "timeout"
    assert db.get_ticker_primer("AEVA")["primer"].startswith("SELLS:")


def test_prose_that_starts_with_a_label_word_is_not_a_label():
    out = P._parse(GOOD + "\nOutside the US, it also sells to Asian phone makers.")
    assert out.count("OUTSIDE:") == 1


def test_a_failed_build_is_remembered_and_a_lookup_does_not_wait(conn, monkeypatch):
    P._failed.clear()
    c = _Client([_resp(GOOD, 0)] * P.BUILD_ATTEMPTS)
    monkeypatch.setattr(P, "_get_client", lambda: c)
    assert asyncio.run(P._execute_ticker_primer({"symbol": "MU"}))["status"] == "no_data"
    calls = c.calls
    assert asyncio.run(P._execute_ticker_primer({"symbol": "MU"}))["status"] == "no_data"
    assert c.calls == calls, "no new grounded calls inside the failure window"
    P._failed.clear()

    slow = _Client([_resp(GOOD, 2)], delay=0.2)
    monkeypatch.setattr(P, "_get_client", lambda: slow)

    async def go():
        first = await P._execute_ticker_primer({"symbol": "TSLA", "wait": False})
        await asyncio.sleep(0.4)
        return first
    assert asyncio.run(go())["status"] == "building"
    assert db.get_ticker_primer("TSLA") is not None, "the background build stored it"


def test_a_building_primer_is_not_a_source():
    from discord_bot import bot, data_footer as F
    assert "building" in bot._FAILED_TOOL_STATUSES
    assert F.footer([{"tool": "ticker_primer", "status": "building"}]) == ""


def test_a_primer_built_with_the_old_prompt_is_rebuilt(conn, monkeypatch):
    """MU's first primer used its pre-2025 segment names."""
    db.upsert_ticker_primer("MU", "SEGMENTS: Compute and Networking", [{"title": "t", "url": "u"}])
    monkeypatch.setattr(P, "MIN_BUILT_AT", "2999-01-01T00:00:00")
    c = _Client([_resp(GOOD, 2)])
    monkeypatch.setattr(P, "_get_client", lambda: c)
    P._failed.clear()
    r = asyncio.run(P._execute_ticker_primer({"symbol": "MU"}))
    assert c.calls == 1 and r["primer"].startswith("SELLS: Memory chips")
    assert "Today is " in P.PROMPT.format(sym="MU", name="", today="October 01, 2026") \
        and "10-K or 10-Q" in P.PROMPT


def test_the_primer_block_and_its_absence():
    from discord_bot import ask_router as R
    block = R.inject_text(R.T_PRIMER, {"status": "ok", "symbol": "MU", "primer": "SELLS: memory"})
    assert block.startswith("[BUSINESS PRIMER") and "BUSINESS OF MU:\nSELLS: memory" in block
    assert R.inject_text(R.T_PRIMER, {"status": "timeout", "symbol": "MU"}) == ""
