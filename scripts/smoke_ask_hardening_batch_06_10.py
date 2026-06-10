"""Smoke test: 2026-06-10 /ask hardening batch — six fix groups from
the second-pass adversarial review.

  1. asyncio.to_thread around all sync network I/O in tool executors
     (blocking urllib/yfinance on the event loop froze the whole bot)
  2. Guarded `_tool_executors` dispatch map (uncaught executor
     exceptions now degrade to a tool-error result, not a dead /ask)
  3. Ask-log completeness: interaction_type / tool_trace / raw_answer
     params on append_ask_interaction; short-circuits + failures logged
  4. Quick batch: word-cap contradiction fixed (350 everywhere),
     anti-recycling 7-day window, 'slang(s)'/'tally' detector vocab,
     chat-search 12KB result size cap
  5. Prompt diet (conservative): routing rule de-duplicated — the
     critical_routing_directive is now a pointer to HARD ROUTING RULES
  6. Slash parity (subject-verbatim + raw-mention profiles), token
     reconciliation moved after retries, trade-close matching prefers
     most-recently-touched open
"""

import asyncio
import inspect
import os
import re
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# =====================================================================
# 1 — to_thread on sync I/O
# =====================================================================

def test_executors_use_to_thread():
    """Every sync network fetcher called from an async executor must go
    through asyncio.to_thread. Count call sites: binance, yahoo-AH,
    finnhub-quote (market_price), yahoo-options x2 (options_chain),
    economic-calendar (calendar) = 6 wrapped sites."""
    import discord_bot.bot as bot_mod
    src_mp = inspect.getsource(bot_mod._execute_market_price)
    src_oc = inspect.getsource(bot_mod._execute_options_chain)
    src_ec = inspect.getsource(bot_mod._execute_economic_calendar)

    assert "asyncio.to_thread" in src_mp, (
        "_execute_market_price must route sync fetchers through "
        "asyncio.to_thread"
    )
    # All three fetchers in market_price wrapped
    assert src_mp.count("asyncio.to_thread") >= 3, (
        f"market_price has 3 sync fetchers (binance, yahoo-AH, finnhub) "
        f"— all must be wrapped, got "
        f"{src_mp.count('asyncio.to_thread')}"
    )
    assert src_oc.count("asyncio.to_thread") >= 2, (
        "_execute_options_chain has 2 yfinance call sites — both must "
        "be wrapped"
    )
    assert "asyncio.to_thread" in src_ec, (
        "_execute_economic_calendar must wrap the Finnhub fetcher"
    )
    # No remaining BARE sync calls (direct invocation without to_thread)
    for bare in ("= _md._fetch_binance_24h(", "= _md._fetch_finnhub_quote(",
                 "yh = _md._fetch_yahoo_extended_hours(",
                 "raw = _md._fetch_yahoo_options_chain(",
                 "events = _nd.fetch_economic_calendar_structured("):
        assert bare not in src_mp + src_oc + src_ec, (
            f"bare sync call remains: {bare!r}"
        )
    _ok("all 6 sync network call sites in tool executors wrapped in "
        "asyncio.to_thread")


# =====================================================================
# 2 — guarded dispatch map
# =====================================================================

def test_dispatch_map_covers_all_seven_tools():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    for tool in ("search_chat_messages", "lookup_user_profile",
                 "lookup_trade_log", "lookup_market_price",
                 "lookup_options_chain", "lookup_economic_calendar"):
        assert f'"{tool}":' in src, f"executor map missing {tool}"
    _ok("executor map covers all 6 function-declared tools")


def test_dispatch_guards_executor_exceptions():
    """An executor that raises must produce a structured error result,
    not propagate. Anchor on the guard + the no-fabricate fallback."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "result = await executor(args)" in src
    # The try/except around the executor call with the degrade message
    anchor = src.find("result = await executor(args)")
    window = src[max(0, anchor - 200):anchor + 1200]
    assert "except Exception" in window, (
        "executor call must be wrapped in try/except"
    )
    assert "failed internally" in window
    assert "Do NOT fabricate" in window, (
        "the degrade message must instruct the model not to fabricate "
        "the data the tool would have returned"
    )
    _ok("dispatch guards executor exceptions with no-fabricate degrade "
        "message")


def test_tool_trace_recorded():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_ask_tool_trace: list[dict] = []" in src
    assert "_ask_tool_trace.append" in src
    assert "tool_trace=_ask_tool_trace" in src, (
        "the trace must be passed to append_ask_interaction"
    )
    _ok("tool trace recorded per call + passed to the ask-log")


# =====================================================================
# 3 — ask-log completeness
# =====================================================================

def test_append_ask_interaction_new_params():
    import db
    sig = inspect.signature(db.append_ask_interaction)
    for p in ("interaction_type", "tool_trace", "raw_answer"):
        assert p in sig.parameters, f"append_ask_interaction missing {p}"
    _ok("append_ask_interaction accepts interaction_type / tool_trace / "
        "raw_answer")


def test_short_circuits_log_to_ask_log():
    import discord_bot.bot as bot_mod
    src_slur = inspect.getsource(bot_mod._answer_slur_count_directly)
    src_msg = inspect.getsource(bot_mod._answer_message_count_directly)
    assert "append_ask_interaction" in src_slur, (
        "slur-count short-circuit must log to the ask-log"
    )
    assert "short_circuit_slur_count" in src_slur
    assert "append_ask_interaction" in src_msg, (
        "message-count short-circuit must log to the ask-log"
    )
    assert "short_circuit_message_count" in src_msg
    _ok("both short-circuits log to the ask-log with type markers")


def test_failures_log_to_ask_log():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert 'interaction_type="failed"' in src, (
        "the exception path must log a 'failed' entry so QC sees the "
        "complete record"
    )
    _ok("Gemini failures logged with interaction_type='failed'")


def test_raw_answer_snapshot_before_cleanup():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    snap_idx = src.find("_raw_answer_pre_clean = answer")
    clean_idx = src.find("answer, hit_kinds = _clean_voice_violations(answer)")
    assert snap_idx != -1 and clean_idx != -1
    assert snap_idx < clean_idx, (
        "raw answer must be snapshotted BEFORE voice cleanup"
    )
    assert "raw_answer=_raw_answer_pre_clean" in src
    _ok("raw answer snapshotted pre-cleanup + passed to the ask-log")


def test_ask_log_renders_new_sections():
    """db-side rendering: type line, tools table, raw block (only when
    different)."""
    import db
    src = inspect.getsource(db.append_ask_interaction)
    assert "**Type:**" in src
    assert "**Tools called:**" in src
    assert "Raw model output" in src
    # raw block conditional on difference
    assert "raw_answer.strip() != (answer or" in src.replace("\n", " ") or \
           'raw_answer.strip() != (answer or "").strip()' in src, (
        "raw block must render only when raw differs from posted answer"
    )
    _ok("ask-log renders Type / Tools / Raw sections")


# =====================================================================
# 4 — quick batch
# =====================================================================

def test_word_cap_consistent():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    assert "≤250 words total (hard cap" not in S, (
        "the stale 250-word Full DD cap must be gone"
    )
    assert "≤350 words total (hard cap" in S
    assert "Hard ceiling at 350 words regardless of type" in S
    _ok("Full DD word cap consistent at 350 everywhere")


def test_anti_recycling_seven_day_window():
    import db
    sig = inspect.signature(db.get_recent_bot_answers_to_asker)
    assert "max_age_days" in sig.parameters
    assert sig.parameters["max_age_days"].default == 7
    src = inspect.getsource(db.get_recent_bot_answers_to_asker)
    assert "datetime('now', ?)" in src, (
        "the SQL must bound by recency, not count only"
    )
    _ok("anti-recycling guard bounded to 7 days")


def test_slang_vocab_added():
    from discord_bot.bot import _is_slur_count_question
    # The exact 2026-06-10 miss
    assert _is_slur_count_question(
        "can i get a tally of the total slangs used in here for everyone"
    ), "the SV 'tally of slangs' shape must now route to the short-circuit"
    # Existing shapes still work
    assert _is_slur_count_question("how many times was a slur used ?")
    # Benign non-count slang questions must NOT fire
    assert not _is_slur_count_question("what does the slang HODL mean"), (
        "no count intent → no short-circuit"
    )
    _ok("'tally of slangs' routes to slur-count; benign slang Qs don't")


def test_chat_search_size_cap():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._execute_chat_search)
    assert "_CHAT_RESULT_MAX_CHARS" in src
    assert "truncated" in src, (
        "truncation must be surfaced to the model so it can say "
        "'showing N of M'"
    )
    _ok("chat-search results capped with surfaced truncation note")


# =====================================================================
# 5 — prompt diet (conservative)
# =====================================================================

def test_routing_rule_deduplicated():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as S
    # The GTLB anecdote previously appeared 2-3x; exactly one copy stays
    n_gtlb = S.count("GTLB +9%")
    assert n_gtlb == 1, (
        f"GTLB wrong-direction anecdote should appear exactly once "
        f"(canonical HARD ROUTING RULES), got {n_gtlb}"
    )
    # The directive is now a compact pointer
    assert "Full routing rules + examples: HARD ROUTING RULES" in S
    # Canonical section + its smoke-anchored line intact
    assert "HARD ROUTING RULES" in S
    assert "call `lookup_market_price` FIRST" in S
    _ok("price-routing rule de-duplicated to one canonical copy")


# =====================================================================
# 6 — parity, reconciliation, matching
# =====================================================================

def test_slash_path_has_subject_verbatim_and_raw_mentions():
    """Read the slash /ask handler source from the file (it's a nested
    function, so anchor on the file text near the slash-path comment)."""
    bot_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "discord_bot", "bot.py",
    )
    with open(bot_path, encoding="utf-8") as f:
        src = f.read()
    assert "Subject-verbatim (parity with @mention path)" in src
    assert "raw_mention_ids" in src
    assert src.count("_format_subject_verbatim_block(") >= 3, (
        "subject-verbatim must be invoked on BOTH entry paths "
        "(definition + slash + @mention = 3 occurrences minimum)"
    )
    _ok("slash /ask: subject-verbatim + raw-mention profile loading")


def test_token_reconciliation_after_retries():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_tally_retry_usage" in src
    # All four retry generate_content sites tally usage
    n_tallies = src.count("_tally_retry_usage(")
    assert n_tallies >= 5, (  # 1 def + 4 call sites
        f"all 4 retry calls must tally usage (repetition, rewrite, "
        f"voice-strip, slur-mask), got {n_tallies - 1} call sites"
    )
    # record_actual must come AFTER the retry blocks (after slur-mask)
    reconcile_idx = src.find("get_budget().record_actual")
    mask_retry_idx = src.find("masked_resp = await client.aio.models.generate_content")
    assert reconcile_idx > mask_retry_idx > 0, (
        "record_actual must run AFTER the slur-mask retry so retry "
        "tokens are reconciled"
    )
    _ok("token reconciliation runs after all retries, all 4 retries "
        "tally usage")


def test_trade_close_matching_most_recent():
    import db
    src = inspect.getsource(db.find_matching_open_expiry)
    assert "ORDER BY posted_at DESC" in src, (
        "close matching must prefer the most-recently-touched open"
    )
    assert "ORDER BY date(expiry) ASC" not in src, (
        "the soonest-expiry ordering must be gone"
    )
    _ok("trade-close matching prefers most-recently-touched open")


if __name__ == "__main__":
    print("=== 2026-06-10 /ask hardening batch smoke ===")
    print("\n--- 1: to_thread ---")
    test_executors_use_to_thread()
    print("\n--- 2: guarded dispatch ---")
    test_dispatch_map_covers_all_seven_tools()
    test_dispatch_guards_executor_exceptions()
    test_tool_trace_recorded()
    print("\n--- 3: ask-log completeness ---")
    test_append_ask_interaction_new_params()
    test_short_circuits_log_to_ask_log()
    test_failures_log_to_ask_log()
    test_raw_answer_snapshot_before_cleanup()
    test_ask_log_renders_new_sections()
    print("\n--- 4: quick batch ---")
    test_word_cap_consistent()
    test_anti_recycling_seven_day_window()
    test_slang_vocab_added()
    test_chat_search_size_cap()
    print("\n--- 5: prompt diet ---")
    test_routing_rule_deduplicated()
    print("\n--- 6: parity + reconciliation + matching ---")
    test_slash_path_has_subject_verbatim_and_raw_mentions()
    test_token_reconciliation_after_retries()
    test_trade_close_matching_most_recent()
    print("\nALL 2026-06-10 HARDENING BATCH SMOKE TESTS PASS")
