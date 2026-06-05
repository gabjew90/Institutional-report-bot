"""Smoke test: message-count question short-circuit replaces the
visible-chat-window inference with a deterministic max(100msg, 6h) query.

Background: 2026-06-04 22:29 UTC, 2pale asked 'how many messages did
kyle send today' — bot answered '9 messages' by counting BK's lines
in the recent-50-chat-window block. That's not 'today', that's
'whatever fit in the visible 50-msg buffer.' User direction: 'look
up either 100 or the last 6 hours, whichever has more messages.'

This smoke covers:
  - Detector fires on the 2026-06-04 failing shape + common variants
  - Target extraction across 6 question shapes
  - Stopword filtering (today/this/etc don't get matched as names)
  - Reply-chain prefix stripping
  - Pool selection: larger of {100 most recent, last 6h} wins
  - Resolution failure → returns None (caller falls back to Gemini)
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# === Detector tests ===

def test_detector_fires_on_2026_06_04_failure():
    from discord_bot.bot import _is_message_count_question
    assert _is_message_count_question(
        "how many messages did kyle send today"
    )
    _ok("detector catches the 2026-06-04 'kyle send today' failure")


def test_detector_fires_on_common_variants():
    from discord_bot.bot import _is_message_count_question
    variants = [
        "how many messages did BK post this week",
        "number of messages from grand nagus",
        "count of posts by @sleep04025",
        "how many posts has kloh sent",
    ]
    for q in variants:
        assert _is_message_count_question(q), f"missed variant: {q!r}"
    _ok(f"detector catches {len(variants)} common count-question variants")


def test_detector_silent_on_non_count_questions():
    from discord_bot.bot import _is_message_count_question
    benign = [
        "what's TSLA at right now",
        "when is the next BOJ meeting",
        "is BK long any AAPL right now",
        "how is the room positioned today",
    ]
    for q in benign:
        assert not _is_message_count_question(q), (
            f"false positive on benign: {q!r}"
        )
    _ok(f"detector ignores {len(benign)} benign questions")


def test_detector_strips_reply_chain_prefix():
    from discord_bot.bot import _is_message_count_question
    q = (
        "[MESSAGE BEING REPLIED TO — from omniwiz — user_id 123]\n"
        "\"some prior bot reply\"\n\n"
        "[2pale's message to you]\nhow many messages did BK send"
    )
    assert _is_message_count_question(q)
    _ok("detector strips reply-chain prefix before scoring")


# === Target extraction tests ===

def test_target_extraction_did_pattern():
    from discord_bot.bot import _extract_message_count_target
    assert _extract_message_count_target(
        "how many messages did kyle send today"
    ) == "kyle"
    _ok("'did NAME send' → extracts NAME")


def test_target_extraction_from_pattern():
    from discord_bot.bot import _extract_message_count_target
    assert _extract_message_count_target(
        "show me number of messages from grand_nagus"
    ) == "grand_nagus"
    _ok("'from NAME' → extracts NAME")


def test_target_extraction_at_mention():
    from discord_bot.bot import _extract_message_count_target
    assert _extract_message_count_target(
        "count of posts from @sleep04025 today"
    ) == "sleep04025"
    _ok("'@NAME' → extracts NAME without the @")


def test_target_extraction_possessive():
    from discord_bot.bot import _extract_message_count_target
    target = _extract_message_count_target(
        "how many of BK's messages were posted yesterday"
    )
    assert target and target.lower() == "bk", target
    _ok("'NAME's messages' possessive → extracts NAME")


def test_target_extraction_filters_stopwords():
    """Words like 'today', 'this', 'the' that match the same-shape
    patterns must not be returned as targets."""
    from discord_bot.bot import _extract_message_count_target
    # Pattern 'did X send' could theoretically match 'did today send' —
    # the stopword filter prevents that
    q = "how many messages did today have in total"
    target = _extract_message_count_target(q)
    assert target != "today", (
        f"'today' should be filtered as stopword, got: {target}"
    )
    _ok("stopword filter prevents 'today'/'this'/'the' being extracted")


def test_target_extraction_returns_none_on_no_match():
    from discord_bot.bot import _extract_message_count_target
    # Count-shaped but no target reference
    assert _extract_message_count_target(
        "how many messages were there today"
    ) is None
    _ok("returns None when question has no target name")


# === Pool-selection logic test (via mocked DB) ===

def test_pool_selection_takes_larger_of_100_or_6h():
    """When the last-6h pool has MORE messages than the most-recent-100
    pool, the executor uses the 6h pool. (Active channel: 200 messages
    in 6h, only 100 most-recent of those would be returned by the
    limit=100 pool — but the 6h pool returns all 200.)"""
    import discord_bot.bot as bot_mod

    # Resolver returns a known user
    with (
        patch("discord_bot.bot._resolve_chat_target",
              return_value=(42, "bankerkyle", "BK")),
        patch("db.record_ask_query", MagicMock()),
    ):
        # Mock: pool_100 returns 100 messages, pool_6h returns 200
        call_log = []

        def fake_search(**kwargs):
            call_log.append(dict(kwargs))
            if kwargs.get("limit", 0) == 100:
                # Pool A — 100 messages, 30 are BK's
                return [
                    {"author_username": "bankerkyle",
                     "posted_at": "2026-06-04T20:00:00Z"}
                    for _ in range(30)
                ] + [
                    {"author_username": "other_user",
                     "posted_at": "2026-06-04T20:00:00Z"}
                    for _ in range(70)
                ]
            else:
                # Pool B (6h, limit=10000) — 200 messages, 60 are BK's
                return [
                    {"author_username": "bankerkyle",
                     "posted_at": "2026-06-04T18:00:00Z"}
                    for _ in range(60)
                ] + [
                    {"author_username": "other_user",
                     "posted_at": "2026-06-04T18:00:00Z"}
                    for _ in range(140)
                ]

        with patch("db.search_chat_messages_for_ask", side_effect=fake_search):
            embed = asyncio.run(bot_mod._answer_message_count_directly(
                question="how many messages did kyle send today",
                user_id=99,
                asker_username="2pale",
                channel_name="stonks-yapping",
            ))

    assert embed is not None
    # The LARGER pool was 6h (200) — BK's count there is 60, not 30
    assert "60" in (embed.description or ""), (
        f"expected count of 60 from larger pool (6h, 200 msgs), got: "
        f"{embed.description!r}"
    )
    assert "last 6 hours" in (embed.description or "")
    _ok("pool selection: larger of {100 most recent, last 6h} wins")


def test_pool_selection_falls_back_to_100_when_6h_smaller():
    """Slow channel scenario: 100 most-recent spans 24h, last 6h only
    has 12 messages. Executor uses the 100 pool."""
    import discord_bot.bot as bot_mod

    with (
        patch("discord_bot.bot._resolve_chat_target",
              return_value=(42, "bankerkyle", "BK")),
        patch("db.record_ask_query", MagicMock()),
    ):
        def fake_search(**kwargs):
            if kwargs.get("limit", 0) == 100:
                # Pool A — 100 messages, 25 are BK's
                return [
                    {"author_username": "bankerkyle",
                     "posted_at": "2026-06-03T18:00:00Z"}
                    for _ in range(25)
                ] + [
                    {"author_username": "other_user",
                     "posted_at": "2026-06-03T18:00:00Z"}
                    for _ in range(75)
                ]
            else:
                # Pool B (6h) — only 12 messages, 3 BK
                return [
                    {"author_username": "bankerkyle",
                     "posted_at": "2026-06-04T22:00:00Z"}
                    for _ in range(3)
                ] + [
                    {"author_username": "other_user",
                     "posted_at": "2026-06-04T22:00:00Z"}
                    for _ in range(9)
                ]

        with patch("db.search_chat_messages_for_ask", side_effect=fake_search):
            embed = asyncio.run(bot_mod._answer_message_count_directly(
                question="how many messages did kyle send",
                user_id=99,
                asker_username="2pale",
                channel_name="stonks-yapping",
            ))

    assert embed is not None
    # Larger pool is 100, BK's count there is 25
    assert "25" in (embed.description or "")
    assert "last 100 channel messages" in (embed.description or "")
    _ok("pool selection: falls back to 100-msg pool when 6h has fewer")


# === Resolution failure → fall through ===

def test_resolution_failure_returns_none():
    """When target user can't be resolved, the short-circuit returns
    None so the caller falls back to Gemini."""
    import discord_bot.bot as bot_mod
    with patch("discord_bot.bot._resolve_chat_target",
               return_value=(None, None, None)):
        embed = asyncio.run(bot_mod._answer_message_count_directly(
            question="how many messages did nobody send today",
            user_id=99,
            asker_username="x",
            channel_name="test",
        ))
    assert embed is None, (
        "resolution failure must return None so caller falls back to Gemini"
    )
    _ok("resolution failure → None (caller falls back to Gemini)")


def test_no_target_extraction_returns_none():
    """Question matches count shape but has no extractable target →
    return None and fall through to Gemini."""
    import discord_bot.bot as bot_mod
    with patch("db.record_ask_query", MagicMock()):
        embed = asyncio.run(bot_mod._answer_message_count_directly(
            question="how many messages were there today",
            user_id=99,
            asker_username="x",
            channel_name="test",
        ))
    assert embed is None
    _ok("no extractable target → None (caller falls back to Gemini)")


if __name__ == "__main__":
    print("=== message-count short-circuit smoke ===")
    test_detector_fires_on_2026_06_04_failure()
    test_detector_fires_on_common_variants()
    test_detector_silent_on_non_count_questions()
    test_detector_strips_reply_chain_prefix()
    test_target_extraction_did_pattern()
    test_target_extraction_from_pattern()
    test_target_extraction_at_mention()
    test_target_extraction_possessive()
    test_target_extraction_filters_stopwords()
    test_target_extraction_returns_none_on_no_match()
    test_pool_selection_takes_larger_of_100_or_6h()
    test_pool_selection_falls_back_to_100_when_6h_smaller()
    test_resolution_failure_returns_none()
    test_no_target_extraction_returns_none()
    print("\nALL MESSAGE-COUNT SHORT-CIRCUIT SMOKE TESTS PASS")
