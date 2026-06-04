"""Smoke test: slur-count question short-circuit bypasses Gemini and
returns a deterministic count answer.

Background: 2026-06-04 16:57-16:58 UTC, 4 /ask interactions in 90
seconds were filter-blocked. All were asking about slur usage counts.
The Voice-strip retry (commit 137e310) handled chat-context slurs by
stripping profile Voice sections — but the trigger here is the
question itself + chat-context slur density, not Voice. Stripping
Voice doesn't help.

Fix: detect 'how many times X used' / 'word count' / 'frequency'
question shapes that reference slurs (explicit or meta). Skip Gemini
entirely. Query db.search_chat_messages_for_ask once per target slur
and assemble a deterministic count response in Python.

This smoke covers:
  - The 4 exact 2026-06-04 failing question shapes (all return True)
  - Benign legitimate questions (all return False)
  - Reply-chain prefix stripping
  - Target extraction (explicit slur → that one; meta → all known)
"""

import asyncio
import sys
from unittest.mock import patch, MagicMock


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# === Detector tests ===

def test_detector_fires_on_2026_06_04_failures():
    """All 4 questions that production-blocked must be detected."""
    from discord_bot.bot import _is_slur_count_question
    failing_qs = [
        "can you do a word count on how many times nigga/nigger used in this chat?",
        "word count word use with an N in this chat",
        "how many times was a slur used ?",
    ]
    for q in failing_qs:
        assert _is_slur_count_question(q), (
            f"detector missed real failure case: {q!r}"
        )
    _ok(f"detector catches {len(failing_qs)} of the 2026-06-04 failing shapes")


def test_detector_strips_reply_chain_prefix():
    """Reply chains embed the prior message in the question text. The
    detector must score the asker's typed text (after the marker),
    not the embedded prior message."""
    from discord_bot.bot import _is_slur_count_question
    q = (
        "[MESSAGE BEING REPLIED TO — from Nft_spaceman — user_id 757772170863837206]\n"
        "\"@omniwiz can you do a word count on nigga/nigger?\"\n\n"
        "[kloh's message to you]\nhow many times was a slur used?"
    )
    assert _is_slur_count_question(q), (
        "detector should strip reply-chain prefix and score kloh's typed text"
    )
    _ok("detector strips reply-chain prefix and scores actual typed text")


def test_detector_silent_on_benign_count_questions():
    """Count-shaped questions without slur reference should NOT route
    to the short-circuit."""
    from discord_bot.bot import _is_slur_count_question
    benign = [
        "send me top ten most used words in chat",
        "how many times was AAPL mentioned this week",
        "how often does BK post in stonks-yapping",
        "word count of CRWV mentions",
        "frequency of TSLA in chat",
    ]
    for q in benign:
        assert not _is_slur_count_question(q), (
            f"detector falsely fired on benign count question: {q!r}"
        )
    _ok(f"detector ignores {len(benign)} benign count questions (no false positives)")


def test_detector_silent_on_non_count_slur_questions():
    """Questions that mention slurs but aren't asking for a count
    should NOT route — those still need Gemini for normal Q/A."""
    from discord_bot.bot import _is_slur_count_question
    non_count = [
        "who's the most racist in this room",
        "is BK actually a racist or just performative",
        "what's the n-word policy in this discord",
        "should we be banning slurs",
    ]
    for q in non_count:
        assert not _is_slur_count_question(q), (
            f"detector falsely fired on non-count slur question: {q!r}"
        )
    _ok(f"detector ignores {len(non_count)} non-count slur questions")


# === Target extraction tests ===

def test_extract_targets_explicit_slur():
    """When a specific slur is named, count just that one."""
    from discord_bot.bot import _extract_slur_targets
    targets = _extract_slur_targets(
        "how many times was nigga used in this chat"
    )
    assert targets == ["nigga"], targets
    _ok("explicit slur reference → just that slur")


def test_extract_targets_two_explicit_slurs():
    """When two slurs are named (nigga/nigger), count both."""
    from discord_bot.bot import _extract_slur_targets
    targets = _extract_slur_targets(
        "word count of nigga and nigger usage"
    )
    assert "nigga" in targets and "nigger" in targets
    _ok("two explicit slurs → both counted")


def test_extract_targets_meta_query_returns_all():
    """When only the meta word 'slur' appears, count all known slurs."""
    from discord_bot.bot import _extract_slur_targets, _KNOWN_SLURS
    targets = _extract_slur_targets("how many times was a slur used")
    assert set(targets) == set(_KNOWN_SLURS), (
        f"meta query should return all known slurs, got {targets}"
    )
    _ok(f"meta 'slur' query → all {len(_KNOWN_SLURS)} known slurs")


def test_extract_targets_n_word_euphemism():
    """'n-word' / 'word with an N' euphemism → count nigga + nigger."""
    from discord_bot.bot import _extract_slur_targets
    targets = _extract_slur_targets("how often does the n-word get dropped")
    # Either returns just n-variants or all known (the helper currently
    # treats n-word as meta and returns all); both are acceptable.
    assert "nigga" in targets or "nigger" in targets or len(targets) >= 5
    _ok("'n-word' euphemism captures n-variants (or falls back to all)")


# === Short-circuit execution tests ===

def test_short_circuit_calls_search_and_formats_count():
    """The short-circuit must call db.search_chat_messages_for_ask
    per target slur and return a formatted embed — NOT call Gemini."""
    from discord_bot.bot import _answer_slur_count_directly

    fake_rows_by_slur = {
        "nigga": [{"x": 1}] * 47,
        "nigger": [{"x": 1}] * 8,
    }
    calls = []

    def fake_search(*, keyword, days, channel_name, limit):
        calls.append((keyword, days, channel_name, limit))
        return fake_rows_by_slur.get(keyword.lower(), [])

    with (
        patch("db.search_chat_messages_for_ask", side_effect=fake_search),
        patch("db.record_ask_query", MagicMock()),
    ):
        embed = asyncio.run(_answer_slur_count_directly(
            question="how many times was nigga and nigger used",
            user_id=123,
            channel_name="stonks-yapping",
        ))

    # Verify search was called for each named slur
    keywords_called = {c[0] for c in calls}
    assert "nigga" in keywords_called
    assert "nigger" in keywords_called
    # Verify the embed contains both counts
    assert "47" in (embed.description or "")
    assert "8" in (embed.description or "")
    # Verify scope mentions the channel
    assert "stonks-yapping" in (embed.description or "")
    _ok("short-circuit calls search per slur + formats counts into embed")


def test_short_circuit_records_ask_query_for_quota():
    """The short-circuit must record the /ask query so quota tracking
    matches the Gemini path."""
    from discord_bot.bot import _answer_slur_count_directly

    record_calls = []

    def fake_record(user_id):
        record_calls.append(user_id)

    with (
        patch("db.search_chat_messages_for_ask", return_value=[]),
        patch("db.record_ask_query", side_effect=fake_record),
    ):
        asyncio.run(_answer_slur_count_directly(
            question="how many times was a slur used",
            user_id=42,
            channel_name="test",
        ))

    assert record_calls == [42], record_calls
    _ok("short-circuit records ask query for quota tracking")


def test_short_circuit_single_slur_clean_phrasing():
    """Single-slur queries get a one-line response, not a list."""
    from discord_bot.bot import _answer_slur_count_directly
    with (
        patch("db.search_chat_messages_for_ask", return_value=[{}] * 5),
        patch("db.record_ask_query", MagicMock()),
    ):
        embed = asyncio.run(_answer_slur_count_directly(
            question="how many times was kike used",
            user_id=1, channel_name="test",
        ))
    desc = embed.description or ""
    assert "kike" in desc and "5" in desc
    # Single-slur response should NOT have bullet list
    assert "•" not in desc, (
        f"single-slur response should be one-line, got:\n{desc}"
    )
    _ok("single-slur query → one-line response (no bullet list)")


def test_short_circuit_handles_db_error_gracefully():
    """If the DB query raises, the response should be a graceful
    error message — NOT an exception."""
    from discord_bot.bot import _answer_slur_count_directly
    with (
        patch("db.search_chat_messages_for_ask",
              side_effect=RuntimeError("DB hiccup")),
        patch("db.record_ask_query", MagicMock()),
    ):
        embed = asyncio.run(_answer_slur_count_directly(
            question="how many times was nigga used",
            user_id=1, channel_name="test",
        ))
    desc = (embed.description or "").lower()
    assert "hiccup" in desc or "try again" in desc or "couldn't" in desc, desc
    _ok("DB error → graceful error embed, no exception")


if __name__ == "__main__":
    print("=== slur-count short-circuit smoke ===")
    test_detector_fires_on_2026_06_04_failures()
    test_detector_strips_reply_chain_prefix()
    test_detector_silent_on_benign_count_questions()
    test_detector_silent_on_non_count_slur_questions()
    test_extract_targets_explicit_slur()
    test_extract_targets_two_explicit_slurs()
    test_extract_targets_meta_query_returns_all()
    test_extract_targets_n_word_euphemism()
    test_short_circuit_calls_search_and_formats_count()
    test_short_circuit_records_ask_query_for_quota()
    test_short_circuit_single_slur_clean_phrasing()
    test_short_circuit_handles_db_error_gracefully()
    print("\nALL SLUR-COUNT SHORT-CIRCUIT SMOKE TESTS PASS")
