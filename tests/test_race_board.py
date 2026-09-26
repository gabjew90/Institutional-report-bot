"""The racism board (2026-09-26).

The rank used to be an LLM 0-100 judgment re-made every six hours from
only the newest messages: cemini23 went 10 -> 92 in two days on 13
lifetime slurs, Sam was #10 with 239 slurs, and DarkMark, with none, was
#26 of 44. Members challenged ranks the bot could not back up. Now each
message is tagged once and the rank is a count over 30 days, and every
rank answer carries that count.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import db
from discord_bot import race_tagger as T
from discord_bot import rank_evidence as E
from scripts.slur_patterns import count_racial_slurs

A, B, C, CLEAN = 9_100_000_001, 9_100_000_002, 9_100_000_003, 9_100_000_004


# --- the deterministic half ----------------------------------------------
def test_racial_slurs_are_counted_once_per_word():
    assert count_racial_slurs("nigger") == 1, "the two n-slur patterns overlap"
    assert count_racial_slurs("retard fag") == 0, "not racial"
    assert count_racial_slurs("kike and chink") == 2


def test_rule_decides_slurs_and_empty_messages_without_a_model():
    msgs = [{"id": 1, "author_id": A, "posted_at": "t", "content": "lol nigga"},
            {"id": 2, "author_id": A, "posted_at": "t", "content": "🚀🚀"},
            {"id": 3, "author_id": A, "posted_at": "t", "content": "buying NVDA calls"}]
    decided, rest = T.split_by_rule(msgs)
    assert [(d[0], d[3], d[5]) for d in decided] == [(1, 1, "regex"), (2, 0, "empty")]
    assert [m["id"] for m in rest] == [3]


def test_a_refused_batch_splits_until_the_refusing_message_is_alone():
    batch = [{"id": i, "author_id": A, "posted_at": "t", "content": f"m{i}"}
             for i in range(4)]

    def fake(b):
        if any(m["id"] == 2 for m in b):
            raise T._Refused("blocked")
        return {m["id"] for m in b if m["id"] == 0}

    with patch.object(T, "_classify", side_effect=fake):
        rows = T._tag_batch(batch)
    got = {r[0]: (r[3], r[5]) for r in rows}
    assert got == {0: (1, "model"), 1: (0, "model"), 2: (-1, "refused"), 3: (0, "model")}


# --- the board -----------------------------------------------------------
@pytest.fixture
def board_rows():
    conn = db.get_connection()
    now = datetime.utcnow()
    rows, tags = [], []
    mid = 9_100_000_000_000

    def add(author, days_ago, edged, slurs=0):
        nonlocal mid
        mid += 1
        ts = (now - timedelta(days=days_ago)).isoformat()
        rows.append((mid, 1, "chat", author, f"u{author}", f"U{author}", "x", ts))
        tags.append((author, ts, edged, slurs))

    for _ in range(5):
        add(A, 2, 1, 1)             # A: 5 race-edged, 5 slurs
    for _ in range(5):
        add(B, 3, 1, 0)             # B: 5 race-edged, no slurs
    for _ in range(3):
        add(C, 1, 1, 2)             # C: 3
    for _ in range(9):
        add(C, 40, 1, 0)            # C led the PRIOR window
    for _ in range(20):
        add(CLEAN, 1, 0)            # never race-edged
    conn.executemany(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, "
        "author_id, author_username, author_display, content, posted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM chat_messages WHERE discord_message_id >= 9100000000000 "
        "ORDER BY discord_message_id").fetchall()]
    db.insert_race_tags([(i, a, ts, e, s, "model")
                         for i, (a, ts, e, s) in zip(ids, tags)])
    conn.commit()
    yield
    conn.execute("DELETE FROM race_tags WHERE author_id IN (?, ?, ?, ?)", (A, B, C, CLEAN))
    conn.execute("DELETE FROM chat_messages WHERE discord_message_id >= 9100000000000")
    conn.commit()
    from db_parts import chat as _chat
    _chat._board_cache.clear()


def test_rank_is_the_count_then_slurs(board_rows):
    board = db.race_board()
    order = [r["user_id"] for r in board["rows"] if r["user_id"] in (A, B, C)]
    assert order == [A, B, C], "A and B tie on 5; A's slurs break it"
    assert CLEAN not in {r["user_id"] for r in board["rows"]}, "0 race-edged is unranked"


def test_an_unranked_member_gets_a_zero_count_not_a_rank(board_rows):
    st = db.race_standing(CLEAN)
    assert st["rank"] is None and st["race_edged"] == 0 and st["messages"] == 20


def test_the_prior_window_answers_month_over_month(board_rows):
    board, prior = db.race_board(), db.race_board(offset_days=30)
    st = db.race_standing(C, board, prior)
    assert st["prior_race_edged"] == 9
    assert st["prior_rank"] < st["rank"], "C led the earlier window"


def test_the_board_reports_its_window_and_coverage(board_rows):
    b = db.race_board()
    assert b["window_days"] == 30 and b["coverage"] == 1.0


# --- evidence ships with every rank --------------------------------------
def _single(racism_rank, edged, msgs, examples=()):
    return {"mode": "single_user", "users": [{
        "display_name": "Sam", "user_id": 1,
        "trader_rank": 8, "trader_rank_total": 59,
        "trader_evidence": {"wins": 9, "losses": 6, "window_days": 21,
                            "receipt_points": 11, "ghosted": 16,
                            "avg_gain_pct_on_closes": 804},
        "racism_rank": racism_rank, "racism_rank_total": 44,
        "racism_evidence": {"race_edged": edged, "racial_slurs": 12,
                            "messages": msgs, "per_100": 1.3,
                            "window_days": 30, "coverage": 1.0,
                            "examples": list(examples)},
    }]}


def test_a_racism_answer_carries_the_count_and_receipts():
    out = E.footer("you're #10 on the slur board",
                   [_single(10, 41, 3100, [{"date": "2026-09-24", "text": "a quote"}])])
    assert "racism #10/44: 41 race-edged messages of 3,100 in 30d" in out
    assert '"a quote" (09-24)' in out
    assert "trader #" not in out, "a racism answer does not need the trade line"


def test_an_unranked_member_is_shown_as_zero():
    out = E.footer("you're clean on slurs", [_single(None, 0, 1709)])
    assert "racism unranked: 0 race-edged messages of 1,709 in 30d" in out


def test_a_trader_answer_carries_the_ledger():
    out = E.footer("you're the #8 trader", [_single(10, 41, 3100)])
    assert "trader #8/59: 9W/6L documented in 21d, 11 win pts" in out
    assert "race-edged" not in out


def test_no_payload_no_footer():
    assert E.footer("anything", None) == "" and E.footer("x", []) == ""


def test_a_leaderboard_lists_every_row_with_its_count():
    p = {"mode": "top_n", "metric": "racism", "users": [
        {"rank": i, "rank_total": 44, "metric": "racism", "display_name": f"m{i}",
         "user_id": i, "racism_evidence": {"race_edged": 50 - i, "messages": 900,
                                           "window_days": 30, "coverage": 1.0}}
        for i in (1, 2, 3)]}
    out = E.footer("top 3 racists", [p])
    assert out.count("race-edged") == 3 and "m1 · racism #1/44: 49" in out


def test_partial_tagging_is_disclosed():
    p = _single(3, 10, 500)
    p["users"][0]["racism_evidence"]["coverage"] = 0.4
    assert "tagging 40% done" in E.footer("racism rank", [p])


def test_a_tag_write_clears_the_cached_board(board_rows):
    before = db.race_board()
    assert db.race_board() is before, "a second read inside the TTL is the cache"
    db.insert_race_tags([])            # empty write: no-op, cache kept
    assert db.race_board() is before
    conn = db.get_connection()
    mid = conn.execute("SELECT id, posted_at FROM chat_messages "
                       "WHERE author_id = ? LIMIT 1", (CLEAN,)).fetchone()
    conn.execute("DELETE FROM race_tags WHERE message_id = ?", (mid[0],))
    db.insert_race_tags([(mid[0], CLEAN, mid[1], 1, 0, "model")])
    after = db.race_board()
    assert after is not before
    assert CLEAN in {r["user_id"] for r in after["rows"]}


def test_evidence_survives_a_long_answer():
    from discord_bot import bot as B
    import inspect
    src = inspect.getsource(B._ask_10_log_and_render)
    assert "answer[:max(0, 4000 - len(_footers))]" in src


def test_the_client_is_built_once_across_threads():
    import threading
    built = []

    class FakeClient:
        def __init__(self, **kw):
            built.append(self)

    T._client = None
    try:
        with patch("google.genai.Client", FakeClient):
            ts = [threading.Thread(target=T._get_client) for _ in range(8)]
            [t.start() for t in ts]
            [t.join() for t in ts]
        assert len(built) == 1
    finally:
        T._client = None
