"""Member trade batch (2026-09-26).

One Gemini call per member message cost ~1,450 tokens each for a 20-100
token message. The batch reads each alert channel every 30 minutes. On a
week's replay it found ~137 real trades against ~91 for the per-message
path, at 319 calls instead of 3,709.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import db
from analyst_log import member_batch as MB
from analyst_log import watcher as W

BK, SAM = 9_200_000_001, 9_200_000_002


def _m(i, author_id, author, content, parent=None, parent_author=None):
    return {"id": i, "author_id": author_id, "author": author,
            "posted_at": "2026-09-24T14:00:00+00:00", "content": content,
            "parent_content": parent, "parent_author": parent_author}


# --- grouping -------------------------------------------------------------
def test_an_authors_messages_stay_together_and_in_order():
    msgs = [_m(1, BK, "BK", "a"), _m(2, SAM, "Sam", "b"), _m(3, BK, "BK", "c")]
    chunks = MB.build_chunks(msgs)
    assert [m["id"] for m in chunks[0]] == [1, 3, 2]


def test_a_chunk_is_capped():
    msgs = [_m(i, 1000 + i, f"u{i}", "NVDA 190c") for i in range(MB.MAX_MSGS + 5)]
    assert [len(c) for c in MB.build_chunks(msgs)] == [MB.MAX_MSGS, 5]


# --- code checks on the model's trades -------------------------------------
def _t(ticker, strike=100, ctype="call"):
    return {"ticker": ticker, "strike": strike, "contract_type": ctype, "action": "open"}


def test_a_members_name_is_not_a_ticker():
    m = _m(1, SAM, "Sam", "Yes", parent="Sam are you in 800 Friday", parent_author="bulch")
    assert not MB._grounded(_t("SAM", 800), m, {})


def test_reactions_and_questions_are_not_trades():
    parent = "But I bought 40 qqq 730c @0.08"
    assert not MB._grounded(_t("QQQ", 730), _m(1, BK, "BK", "😭😭😭", parent), {})
    assert not MB._grounded(_t("QQQ", 730), _m(1, BK, "BK", "240%", parent), {})
    assert not MB._grounded(_t("SPX", 10000), _m(1, BK, "BK", "You bought 5 0dte Spx 10000c?"), {})


def test_ticker_and_strike_must_be_in_the_text_parent_or_own_history():
    assert not MB._grounded(_t("BAMA", 51), _m(1, BK, "BK", "10/16 32c"), {})
    ctx = {BK: [{"posted_at": "x", "content": "NVDA 190c 10/3 @2.1"}]}
    assert MB._grounded(_t("NVDA", 190), _m(1, BK, "BK", "sold half @3"), ctx)
    assert not MB._grounded(_t("NVDA", 195), _m(1, BK, "BK", "sold half @3"), ctx)


def test_index_options_trade_by_strike_alone():
    assert MB._grounded(_t("SPX", 7725), _m(1, BK, "BK", "Sold my 7725c at 5.6"), {})
    assert not MB._grounded(_t("NVDA", 7725), _m(1, BK, "BK", "Sold my 7725c at 5.6"), {})


def test_numeric_tickers_are_rejected():
    assert not MB._grounded(_t("200", 200), _m(1, BK, "BK", "200/200c"), {})


def test_chart_commands_never_reach_the_model():
    for c in ("fc MU 5", "Fc $spx 1", "fc mrna 15 % <@1>", "FC boats:soxl 15"):
        assert not W.could_be_trade_caption(c, is_reply=False), c


# --- the job -------------------------------------------------------------
CH = "test-member-alerts"


@pytest.fixture
def channel_rows():
    conn = db.get_connection()
    now = datetime.now(timezone.utc)
    rows = [
        (9_300_000_001, BK, "BK", now - timedelta(minutes=20), "NVDA 190c 10/3 @2.10"),
        (9_300_000_002, SAM, "Sam", now - timedelta(minutes=15), "gm fellas"),
        (9_300_000_003, BK, "BK", now - timedelta(minutes=10), "sold half @3.40"),
    ]
    for mid, aid, name, ts, text in rows:
        conn.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, "
            "author_id, author_username, author_display, content, posted_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            (mid, CH, aid, name.lower(), name, text, ts.isoformat()))
    conn.commit()
    yield now
    conn.execute("DELETE FROM chat_messages WHERE channel_name = ?", (CH,))
    conn.execute("DELETE FROM analyst_trades WHERE discord_message_id BETWEEN 9300000001 AND 9300000009")
    conn.execute("DELETE FROM member_batch_state WHERE channel_name = ?", (CH,))
    conn.commit()


def _fake_call(prompt, model):
    trades = []
    if "NVDA 190c" in prompt:
        trades.append({"id": "m1", "ticker": "NVDA", "contract_type": "call", "strike": 190,
                       "expiry": "2026-10-03", "action": "open", "price": 2.1})
        trades.append({"id": "m2", "ticker": "NVDA", "contract_type": "call", "strike": 190,
                       "expiry": None, "action": "trim", "price": 3.4})
    return trades


def test_a_run_writes_member_trades_and_moves_the_position(channel_rows):
    with patch.object(MB, "_call", side_effect=_fake_call):
        r = MB.run_channel(CH, now=channel_rows)
    assert r["trades"] == 2 and r["calls"] == 1
    got = db.get_connection().execute(
        "SELECT discord_message_id, action, tracking_mode, is_trade FROM analyst_trades "
        "WHERE discord_message_id BETWEEN 9300000001 AND 9300000009 ORDER BY 1").fetchall()
    assert [(g[1], g[2], g[3]) for g in got] == [("open", "member", 1), ("trim", "member", 1)]
    assert "gm fellas" not in str(got), "non-trades write no row"
    assert db.member_batch_watermark(CH) is not None
    with patch.object(MB, "_call", side_effect=AssertionError("re-read")):
        assert MB.run_channel(CH, now=channel_rows)["messages"] == 0


def test_a_failed_call_keeps_the_window(channel_rows):
    with patch.object(MB, "_call", side_effect=ConnectionError("down")):
        r = MB.run_channel(CH, now=channel_rows)
    assert r.get("failed") and db.member_batch_watermark(CH) is None


def test_member_text_posts_leave_the_live_path():
    author = SimpleNamespace(name="member", display_name="Member", id=1)
    msg = SimpleNamespace(content="NVDA 190c @2", attachments=[], reference=None,
                          author=author, id=42, channel=SimpleNamespace(name="alerts"),
                          created_at=SimpleNamespace(isoformat=lambda: "2026-09-26T00:00:00"))
    with patch.object(W, "extract_trade_from_caption", new=AsyncMock()) as ex, \
         patch.object(W, "_fetch_reply_parent_caption", new=AsyncMock(return_value="")):
        asyncio.run(W.watch_message(None, msg, tracking_mode="member"))
    ex.assert_not_called()


def test_a_catch_up_message_with_an_older_timestamp_is_still_read(channel_rows):
    """After a restart, catch-up stores missed posts with their original
    timestamps, older than what the batch already read."""
    with patch.object(MB, "_call", side_effect=_fake_call):
        MB.run_channel(CH, now=channel_rows)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, "
        "author_id, author_username, author_display, content, posted_at) "
        "VALUES (9300000004, 1, ?, ?, 'sam', 'Sam', 'bought TSLA 300c @4', ?)",
        (CH, SAM, (channel_rows - timedelta(minutes=30)).isoformat()))
    conn.commit()
    seen = []

    def fake(prompt, model):
        seen.append(prompt)
        return [{"id": "m1", "ticker": "TSLA", "contract_type": "call", "strike": 300,
                 "expiry": "2026-10-02", "action": "open", "price": 4}]
    with patch.object(MB, "_call", side_effect=fake):
        r = MB.run_channel(CH, now=channel_rows)
    assert r["trades"] == 1 and "TSLA 300c" in seen[0]


def test_class_share_tickers_pass():
    assert MB._grounded(_t("BRK.B", 500), _m(1, BK, "BK", "Bought BRK.B 500c"), {})
