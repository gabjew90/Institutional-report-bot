"""Smoke test: cross-window anti-recycling guard.

Background: 2026-06-04 Grand Nagus QC found the bot used the SAME hook
("LARPing as a quant") in two /ask answers ~30 min apart. Anti-recycling
rule in _ASK_SYSTEM_INSTRUCTION scans [YOU said earlier]: lines in the
chat_context block — but that block is built from a 50-msg / 24h
channel.history window. On active channels (stonks-yapping), the bot's
prior answer scrolls past in <30 min. Rule has no data, hook gets reused.

Fix: persist every /ask reply into a new ask_bot_answers table keyed by
(asker_user_id, channel_id), then prepend the last 5 cross-window into
the Gemini prompt as [YOU said earlier to this asker]: lines so the
existing anti-recycling rule covers them — bounded by COUNT not recency.

This smoke covers:
  - DB: schema + record_ask_bot_answer + get_recent_bot_answers_to_asker
        scoping (asker × channel only), ordering (newest first), limit
  - Bot: _answer_with_gemini accepts channel_id and the prompt build
        path includes a [YOUR RECENT /ASK ANSWERS TO THIS ASKER ...]
        section when prior answers exist
  - Callsites: both /ask slash and @mention pass channel_id + call
        db.record_ask_bot_answer after the reply
"""

import inspect
import os
import sys
import tempfile

# Run from scripts/ — add repo root to sys.path so `import db` works.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# === DB layer ===

def test_db_schema_has_ask_bot_answers():
    """Boot a fresh in-memory DB and verify the new table + indexes exist."""
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_path = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = tmp.name
    try:
        # Force reload of config + db connection
        import importlib
        from config import settings as _settings
        _settings.db_path = tmp.name
        # Reset cached connection so init_db hits the new path
        db._conn = None
        db.get_connection()  # lazy schema init
        conn = db.get_connection()
        cur = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='ask_bot_answers'"
        )
        assert cur.fetchone() is not None, "ask_bot_answers table missing"
        # Verify the index for the (asker, channel, ts) query plan
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='ask_bot_answers'"
        )
        idx_names = {r["name"] for r in cur.fetchall()}
        assert any("asker_channel" in n for n in idx_names), (
            f"missing the (asker, channel, ts) index, got {idx_names}"
        )
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        if old_path is not None:
            os.environ["DB_PATH"] = old_path
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass  # Windows file lock — best effort
    _ok("ask_bot_answers table + (asker, channel, ts) index exist")


def test_db_record_and_fetch_roundtrip():
    """Insert two answers, fetch them, verify newest-first ordering and
    that only this asker × channel combo comes back."""
    import db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_path = os.environ.get("DB_PATH")
    os.environ["DB_PATH"] = tmp.name
    try:
        from config import settings as _settings
        _settings.db_path = tmp.name
        db._conn = None
        db.get_connection()  # lazy schema init

        # 3 answers to asker 42 in channel 100, 1 to asker 42 in channel 200,
        # 1 to asker 99 in channel 100 — verify scoping
        db.record_ask_bot_answer(42, 100, "Q1", "A1 — first hook")
        db.record_ask_bot_answer(42, 100, "Q2", "A2 — second hook")
        db.record_ask_bot_answer(42, 100, "Q3", "A3 — third hook")
        db.record_ask_bot_answer(42, 200, "Q-other-ch", "A-other-ch")
        db.record_ask_bot_answer(99, 100, "Q-other-asker", "A-other-asker")

        rows = db.get_recent_bot_answers_to_asker(42, 100, limit=5)
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
        # Newest first
        assert "third" in rows[0]["answer"], rows
        assert "second" in rows[1]["answer"], rows
        assert "first" in rows[2]["answer"], rows
        # Scoping: no cross-channel or cross-asker leakage
        for r in rows:
            assert "other-ch" not in r["answer"]
            assert "other-asker" not in r["answer"]

        # Limit clamps
        rows2 = db.get_recent_bot_answers_to_asker(42, 100, limit=2)
        assert len(rows2) == 2

        # Empty input shouldn't crash and shouldn't persist
        db.record_ask_bot_answer(42, 100, "Q4", "")
        rows3 = db.get_recent_bot_answers_to_asker(42, 100, limit=10)
        assert len(rows3) == 3, "empty answer should be a no-op"
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        if old_path is not None:
            os.environ["DB_PATH"] = old_path
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass  # Windows file lock — best effort
    _ok("record_ask_bot_answer + get_recent_bot_answers_to_asker "
        "round-trip, scoped, newest-first, limit-bounded")


# === Bot wiring ===

def test_answer_with_gemini_accepts_channel_id():
    """The _answer_with_gemini signature must accept channel_id."""
    from discord_bot.bot import _answer_with_gemini
    sig = inspect.signature(_answer_with_gemini)
    assert "channel_id" in sig.parameters, (
        f"channel_id missing from _answer_with_gemini, got "
        f"{list(sig.parameters)}"
    )
    p = sig.parameters["channel_id"]
    assert p.default is None, (
        f"channel_id should default to None for back-compat, got {p.default!r}"
    )
    _ok("_answer_with_gemini accepts channel_id (default None)")


def test_answer_with_gemini_calls_cross_window_helper():
    """Source-level anchor: _answer_with_gemini calls the new helper."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "get_recent_bot_answers_to_asker" in src, (
        "_answer_with_gemini must query db.get_recent_bot_answers_to_asker"
    )
    # And it must build a section the model can read
    assert "YOUR RECENT /ASK ANSWERS TO THIS ASKER" in src or \
           "YOU said earlier to this asker" in src, (
        "_answer_with_gemini must label the cross-window block with a "
        "[YOU said earlier]:-compatible tag so the existing anti-"
        "recycling rule covers it"
    )
    _ok("_answer_with_gemini builds the cross-window [YOU said earlier]: "
        "block")


def test_cross_window_section_threads_through_retries():
    """Voice-strip retry + slur-mask retry must both include the cross-
    window block. Without this, a filter-trip retry loses the anti-
    recycling signal."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    # Both retry sections should reference cross_window_block
    n = src.count("cross_window_block")
    assert n >= 4, (
        f"cross_window_block should appear at least 4x (build + main "
        f"sections + voice-strip retry + slur-mask retry), got {n}"
    )
    _ok(f"cross_window_block referenced {n}x — threads through both retries")


def test_callsites_pass_channel_id_and_record_answer():
    """Both /ask slash and @mention callsites must (a) pass channel_id
    and (b) call db.record_ask_bot_answer after sending the embed."""
    bot_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "discord_bot", "bot.py",
    )
    with open(bot_path, encoding="utf-8") as f:
        src = f.read()
    # channel_id must be passed at both callsites — count of "channel_id="
    # arguments in the source
    n_channel_id_kw = src.count("channel_id=")
    assert n_channel_id_kw >= 2, (
        f"channel_id keyword should be passed at >=2 callsites "
        f"(/ask slash + @mention), got {n_channel_id_kw}"
    )
    # record_ask_bot_answer must be called at both callsites
    n_record = src.count("record_ask_bot_answer(")
    # >=3: 1 def in db.py reference, 2 call sites (slash + mention)
    assert n_record >= 2, (
        f"record_ask_bot_answer should be called at >=2 sites in bot.py, "
        f"got {n_record}"
    )
    _ok(f"both callsites pass channel_id ({n_channel_id_kw} sites) and "
        f"record the answer ({n_record} record calls)")


if __name__ == "__main__":
    print("=== cross-window anti-recycling guard smoke ===")
    test_db_schema_has_ask_bot_answers()
    test_db_record_and_fetch_roundtrip()
    test_answer_with_gemini_accepts_channel_id()
    test_answer_with_gemini_calls_cross_window_helper()
    test_cross_window_section_threads_through_retries()
    test_callsites_pass_channel_id_and_record_answer()
    print("\nALL CROSS-WINDOW ANTI-RECYCLING SMOKE TESTS PASS")
