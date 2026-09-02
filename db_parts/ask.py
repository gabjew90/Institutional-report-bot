"""/ask: query log, quota, bot answers.

Moved verbatim from db.py on 2026-09-01. Every reference to a db.py
function goes through `_db.<name>` so the facade stays the single
patch point and the thread-local connection model lives in db.py.
"""
from datetime import datetime, date, timedelta
import logging

import db as _db  # noqa: E402

log = logging.getLogger("db")


def count_ask_queries_today_for_user(user_id: int) -> int:
    """Count this user's /ask queries since UTC midnight."""
    today_utc_midnight = datetime.utcnow().strftime("%Y-%m-%d 00:00:00")
    row = _db.get_connection().execute(
        "SELECT COUNT(*) AS c FROM ask_queries WHERE user_id = ? AND asked_at >= ?",
        (int(user_id), today_utc_midnight),
    ).fetchone()
    return int(row["c"]) if row else 0


def record_ask_query(user_id: int) -> None:
    conn = _db.get_connection()
    conn.execute("INSERT INTO ask_queries (user_id) VALUES (?)", (int(user_id),))
    conn.commit()


def record_ask_bot_answer(
    asker_user_id: int,
    channel_id: int,
    question: str | None,
    answer: str,
) -> None:
    """Persist the bot's /ask reply so the next /ask from this asker in
    this channel can see it via [YOU said earlier (to this asker, cross-
    window)]: lines — the anti-recycling guard's source of truth when the
    50-msg channel.history window has scrolled past."""
    if not answer or not str(answer).strip():
        return
    conn = _db.get_connection()
    conn.execute(
        "INSERT INTO ask_bot_answers "
        "(asker_user_id, channel_id, question, answer) VALUES (?, ?, ?, ?)",
        (
            int(asker_user_id),
            int(channel_id),
            (question or "")[:2000] or None,
            str(answer)[:4000],
        ),
    )
    conn.commit()


def get_recent_bot_answers_to_asker(
    asker_user_id: int,
    channel_id: int,
    limit: int = 5,
    max_age_days: int = 7,
) -> list[dict]:
    """Return the bot's last `limit` /ask answers to this asker in this
    channel within `max_age_days`, newest first. Returned dicts have
    keys: question, answer, answered_at.

    The recency bound (added 2026-06-10) prevents stale-answer
    injection: count-only bounding meant a quiet channel could surface
    weeks-old answers, suppressing legitimately fresh re-answers when
    circumstances had changed (prices moved, news landed). 7 days
    covers the observed recycling window (the Grand Nagus case was 30
    minutes apart) with a wide margin."""
    rows = _db.get_connection().execute(
        "SELECT question, answer, answered_at "
        "FROM ask_bot_answers "
        "WHERE asker_user_id = ? AND channel_id = ? "
        "  AND answered_at >= datetime('now', ?) "
        "ORDER BY answered_at DESC, id DESC LIMIT ?",
        (int(asker_user_id), int(channel_id),
         f"-{int(max_age_days)} day", int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]
