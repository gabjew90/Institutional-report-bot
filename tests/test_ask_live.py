"""The /ask live harness (scripts/ask_live.py, 2026-09-30): read-only stubs
and a chat block shaped like bot._fetch_chat_context."""
import sqlite3
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import ask_live as H
from discord_bot import chat_context as cc

BOT_EMBED = f'["→ $MU $1072 into the print · {cc.NFA_FOOTER}"]'
FEED_EMBED = '["Goldman Sachs | US Equities Weekly Rundown"]'


def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE chat_messages (id INTEGER PRIMARY KEY, channel_id INTEGER, channel_name TEXT,
            author_id INTEGER, author_username TEXT, author_display TEXT, content TEXT,
            embed_texts TEXT, has_attachments INTEGER DEFAULT 0, posted_at TEXT);
    """)
    rows = [
        (1, 77, "general", 10, "kloh.", "kloh", "sold sndk +10%", None, 0, "2099-01-01 10:00:00"),
        (2, 77, "general", 99, "omnibot", "omnibot", "", BOT_EMBED, 0, "2099-01-01 10:01:00"),
        (3, 77, "general", 11, "bk", "bk", "", None, 1, "2099-01-01T10:02:00"),   # T form, image only
        (4, 77, "general", 12, "abe", "abe", "", None, 0, "2099-01-01 10:03:00"),  # nothing usable
        (5, 78, "alerts", 10, "kloh.", "kloh", "other channel", None, 0, "2099-01-01 10:04:00"),
        # the research feed posts more embeds than the bot answers
        (6, 79, "feed", 55, "feed", "feed", "", FEED_EMBED, 0, "2099-01-01 09:00:00"),
        (7, 79, "feed", 55, "feed", "feed", "", FEED_EMBED, 0, "2099-01-01 09:01:00"),
        (8, 79, "feed", 55, "feed", "feed", "", FEED_EMBED, 0, "2099-01-01 09:02:00"),
    ]
    conn.executemany("INSERT INTO chat_messages VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    return conn


def test_chat_block_matches_the_channel_shape():
    conn = _conn()
    block, channel_id = H._chat_block(conn, "general", bot_user_id=99, max_messages=50,
                                      max_age_min=10 ** 9, per_msg_chars=600)
    assert channel_id == 77
    assert block.startswith(cc.CHAT_HEADER)
    lines = block[len(cc.CHAT_HEADER):].split("\n")
    # oldest -> newest across the space and T timestamp forms; the empty row is skipped;
    # display == username collapses to one name
    assert lines == ["kloh (kloh.): sold sndk +10%",
                     f"{cc.BOT_SPEAKER}: → $MU $1072 into the print · {cc.NFA_FOOTER}",
                     "bk: (image)"]


def test_chat_block_is_empty_for_a_quiet_channel_and_the_bot_is_found_by_its_footer():
    conn = _conn()
    assert H._chat_block(conn, "nowhere", None, 50, 10 ** 9, 600) == ("", None)
    assert H._bot_user_id(conn) == 99, "the feed has more embeds; the NFA footer picks the bot"


def test_stubs_capture_the_writes_instead_of_writing():
    fake_db = types.SimpleNamespace()
    conn = _conn()
    H._install_stubs(fake_db, conn)
    assert fake_db.get_connection() is conn
    fake_db.record_ask_query(5)
    fake_db.append_ask_interaction(question="q", answer="a", meta={"route_shape": "price"})
    fake_db.record_gemini_call(caller="ask", model="m")
    assert fake_db.set_chat_image_ocr(1, 2, status="failed") is None
    assert H.CAPTURED["quota_increments"] == 1
    assert H.CAPTURED["ask_log"]["meta"]["route_shape"] == "price"
    assert H.CAPTURED["gemini_calls"][0]["caller"] == "ask"
    # a write the harness does not know about fails loudly on the read-only handle
    ro = sqlite3.connect("file::memory:?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ro.execute("CREATE TABLE t (x)")


def test_the_channel_path_uses_the_same_formatter():
    import discord_bot.bot as bot
    assert bot.chat_context is cc
    assert cc.speaker("kloh", "kloh.") == "kloh (kloh.)" and cc.speaker("bk", "BK") == "bk"
    assert cc.line("bk", "") == "bk: (image)"
