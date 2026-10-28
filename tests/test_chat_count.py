"""discord_bot/chat_count.py: chat counts counted and written in code (2026-10-28).

The questions are the room's own, from the ask logs: the slam count that
the chat search could not do (2026-10-06), and the slur and message
shapes the two retired bot.py shortcuts answered (2026-06-04, 06-10)."""
from datetime import datetime, timedelta, timezone

import pytest

import db
from db_parts import chat as chat_part
from discord_bot import chat_count as C

ABE, BK, SAM = 900_001, 423994649317736448, 900_003
STONKS, ABE_ALERTS = 1317587853282119745, 1317587853282119747
MID0 = 9_400_000_000


@pytest.fixture
def room(monkeypatch):
    conn = db.get_connection()
    now = datetime.now(timezone.utc)
    rows = [
        (ABE, "abullish_xyz", "abullish", STONKS, "💬-stonks-yapping-💬", 2, "So slam at open is what you're saying"),
        (ABE, "abullish_xyz", "abullish", STONKS, "💬-stonks-yapping-💬", 1, "I told you to slam it, then I slammed it"),
        (ABE, "abullish_xyz", "abullish", STONKS, "💬-stonks-yapping-💬", 1, "Islam is a religion"),
        (ABE, "abullish_xyz", "abullish", ABE_ALERTS, "🥷🏽-abe-alerts-🥷🏽", 30, "slam these calls"),
        (ABE, "abullish_xyz", "abullish", ABE_ALERTS, "🥷🏽-abe-alerts-🥷🏽", 3, "NVDA 190c @2.10"),
        (BK, "bankerkyle", "BK", STONKS, "💬-stonks-yapping-💬", 0.01, "lol nigga what"),
        (BK, "bankerkyle", "BK", STONKS, "💬-stonks-yapping-💬", 0.02, "lol lol"),
        (SAM, "theorb_18574", "Sam", STONKS, "💬-stonks-yapping-💬", 0.03, "lol"),
    ]
    for i, (aid, user, disp, cid, cname, days_ago, text) in enumerate(rows):
        conn.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
            "author_username, author_display, content, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (MID0 + i, cid, cname, aid, user, disp, text, (now - timedelta(days=days_ago)).isoformat()))
    conn.commit()
    monkeypatch.setitem(chat_part._alias_cache, "map", {"abe": ABE, "kyle": BK, "sam": SAM})
    yield now
    conn.execute("DELETE FROM chat_messages WHERE discord_message_id BETWEEN ? AND ?", (MID0, MID0 + 99))
    conn.commit()


def test_parse_reads_member_words_channel_and_window():
    s = C.parse("How many times has Abe said slam or slammed in his channel in the last 21 days")
    assert s["terms"] == ["slam", "slammed"] and s["subject"] == "abe"
    assert s["scope"] == "own" and s["window"] == {"kind": "last", "n": 21, "unit": "d"}


@pytest.mark.parametrize("q", [
    "can you do a word count on how many times nigga/nigger used in this chat?",
    "word count word use with an N in this chat",
    "how many times was a slur used ?",
    "can i get a tally of the total slangs used in here for everyone",
    "how often does kyle say the n word",
])
def test_the_old_slur_shapes_still_count(q):
    s = C.parse(q)
    assert s and s["slurs"]


@pytest.mark.parametrize("q", [
    "how many times do I need to 10x $100",
    "how many times has kyle said he is selling",
    "what does the slang HODL mean",
    "send me top ten most used words in chat",
    "how many times have you said gay",
    "how often does BK post in stonks-yapping",
])
def test_not_a_count_this_module_answers(q):
    assert C.parse(q) is None


def test_the_slam_count_is_his_channel_with_the_rest_beside_it(room):
    spec = C.parse("How many times has Abe said slam or slammed in his channel in the last 21 days")
    text = C.answer(spec, asker_id=SAM, channel_id=STONKS)
    assert "**abullish** (abullish_xyz): **0** messages" in text and "#abe-alerts" in text
    # Islam is not slam; the 30-day-old alert is outside the window
    assert "In every channel the bot reads: **2** (2 in #stonks-yapping)" in text
    assert "Latest: “I told you to slam it, then I slammed it”" in text


def test_a_short_name_resolves_on_the_logged_name(room):
    text = C.answer(C.parse('how many times has BK said "lol" today'), asker_id=SAM, channel_id=STONKS)
    assert text.startswith("→ **BK** (bankerkyle): **2** messages with “lol”")
    assert "(**3** uses)" in text


def test_a_slur_tally_names_nobody(room):
    text = C.answer(C.parse("how many times was a slur used ?"), asker_id=SAM, channel_id=STONKS)
    assert text.startswith("→ **The room**: **1** message with a slur since")
    assert "Most from" not in text and "Latest" not in text


def test_a_room_word_count_names_who_said_it_most(room):
    text = C.answer(C.parse("how many times was lol said today"), asker_id=ABE, channel_id=STONKS)
    assert "→ **The room**: **3** messages with “lol” today" in text
    assert "Most from: BK (bankerkyle) 2 · Sam (theorb_18574) 1" in text


def test_my_messages_with_no_window_use_the_owner_pool(room):
    text = C.answer(C.parse("how many messages have I sent"), asker_id=BK, channel_id=STONKS)
    # the last 100 messages (6 here) beat the last 6 hours (3): the larger pool
    assert text.startswith("→ **You** sent **2** messages of the last **6** in #stonks-yapping")


def test_a_stated_window_counts_every_channel(room):
    text = C.answer(C.parse("how many messages has abe sent in the last 7 days"),
                    asker_id=SAM, channel_id=STONKS)
    assert text.startswith("→ **abullish** (abullish_xyz) sent **4** messages in the last 7 days")
    assert "By channel: #stonks-yapping 3 · #abe-alerts 1" in text


def test_a_name_nobody_is_logged_under_goes_to_the_model(room):
    assert C.answer(C.parse("how many times has zorblax said slam"), asker_id=SAM, channel_id=STONKS) is None


def test_words_count_their_endings_and_slurs_only_their_plurals():
    words = C._matcher(["slam"], slurs=False)
    assert [w for w in ("slams", "slammed", "slamming", "Islam", "slamdunk") if words.search(w)] == \
        ["slams", "slammed", "slamming"]
    slurs = C._matcher(["spic"], slurs=True)
    assert [w for w in ("spics", "spicy", "spices") if slurs.search(w)] == ["spics"]
