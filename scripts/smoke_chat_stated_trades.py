"""Smoke test: chat-stated trades in the member trade lookup (2026-06-17).

Members complained the bot told active traders "you did nothing /
batting .000." Root cause: the "how did I do / my trades" lookup read
only the OCR'd screenshot ledger (analyst_trades). Most of the room
calls trades by TALKING ("meta put at open 100%", "buy puts", "fomc
shorts"), so the ledger is empty for them and the bot concluded they
traded nothing — observed 06-17: terlin said "meta put at open 100%"
and got "zero mentions of META, zero puts, zero shorts."

Fix: lookup_trade_log's member path now also returns chat_stated_trades
(the member's own recent trade-shaped chat messages, self-reported);
status is 'ok' when EITHER source has data; the §4 prompt forbids
"you did nothing" when chat shows trades and forbids fabricated dollar
P&L (the "+$8,839.28" invented on a real +65.47%).

Covers: the trade-signal regex precision, get_recent_user_chat_trades,
the executor returning chat_stated_trades, and the prompt rules.
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_trade_regex_precision():
    from db import _CHAT_TRADE_RE as R
    should_match = [
        "meta put at open 100%",
        "buy puts",
        "fomc shorts",
        "bought 30000 ndxp here",
        "Eyeing meta 1dte put",
        "i mentioned meta puts and shorting u dumbfucjk",
        "$SPY calls into close",
        "30000 c looking juicy",
        "rebought 30k c at 70",          # "rebought"
        "shorted the rip +40%",
    ]
    should_not = [
        "good morning everyone",
        "call me later bro",             # bare singular 'call'
        "put it down already",           # bare singular 'put'
        "lmaooo to the moon",
        "what do you think of GEO",
        "Ry cares more about BK",
    ]
    for s in should_match:
        assert R.search(s), f"should match: {s!r}"
    for s in should_not:
        assert not R.search(s), f"should NOT match: {s!r}"
    _ok("trade regex: catches put/call/short trade talk, ignores "
        "'call me' / 'put it down'")


def test_get_recent_user_chat_trades_and_executor():
    import db
    from discord_bot import bot as bot_mod
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        from config import settings as _s
        _s.db_path = tmp.name
        db._conn = None
        con = db.get_connection()
        uid = 1032155245016522763
        # Anchor timestamps relative to NOW (2 days ago) so they stay
        # inside the executor's default 7-day window — fixed dates went
        # stale and straddled the cutoff (2026-06-24 flake).
        from datetime import datetime, timedelta
        _recent = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        msgs = [
            (_recent, "💬-stonks-yapping-💬", "meta put at open 100%"),
            (_recent, "💬-stonks-yapping-💬", "buy puts"),
            (_recent, "💬-stonks-yapping-💬", "fomc shorts"),
            (_recent, "💬-stonks-yapping-💬", "good morning"),       # not a trade
            (_recent, "💬-stonks-yapping-💬", "call me later"),       # not a trade
        ]
        for i, (ts, ch, content) in enumerate(msgs, 1):
            con.execute(
                "INSERT INTO chat_messages (discord_message_id, channel_id, "
                "channel_name, author_id, author_username, posted_at, content) "
                "VALUES (?, 1, ?, ?, 'terlin', ?, ?)",
                (2000 + i, ch, uid, ts, content),
            )
        con.commit()

        # db helper: only the 3 trade messages, non-trade dropped
        chat = db.get_recent_user_chat_trades(uid, days=400)
        texts = [c["text"] for c in chat]
        assert "meta put at open 100%" in texts, texts
        assert "buy puts" in texts and "fomc shorts" in texts
        assert "good morning" not in texts and "call me later" not in texts
        assert len(chat) == 3, texts

        # executor: member path returns chat_stated_trades, status ok,
        # even with no profile snippet
        result = asyncio.run(bot_mod._execute_trade_log(
            {"username": "terlin", "kind": "recent"}
        ))
        assert result["status"] == "ok", result
        assert result.get("profile_recent_trades") in (None, ""), result
        stated = [c["text"] for c in result.get("chat_stated_trades") or []]
        assert "meta put at open 100%" in stated, result
        _ok("member lookup: chat_stated_trades surfaced, status ok with "
            "empty ledger (no more 'you did nothing')")
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass


def test_executor_empty_only_when_both_empty():
    import db
    from discord_bot import bot as bot_mod
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        from config import settings as _s
        _s.db_path = tmp.name
        db._conn = None
        con = db.get_connection()
        uid = 999
        # one non-trade chat message so the user resolves but has no trades
        con.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, posted_at, content) "
            "VALUES (5001, 1, 'c', ?, 'quietguy', '2026-06-17T12:00:00', "
            "'good morning all')",
            (uid,),
        )
        con.commit()
        result = asyncio.run(bot_mod._execute_trade_log(
            {"username": "quietguy", "kind": "recent"}
        ))
        assert result["status"] == "empty", result
        _ok("member lookup: status 'empty' only when ledger AND chat both "
            "empty")
    finally:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:
            pass
        db._conn = None
        try:
            os.unlink(tmp.name)
        except PermissionError:
            pass


def test_prompt_rules():
    from discord_bot.bot import _ASK_SYSTEM_INSTRUCTION as P
    assert "chat_stated_trades" in P
    anchor = P.find('"How did I do"')
    assert anchor != -1, "missing the How-did-I-do binding block"
    window = P[anchor:anchor + 1400]
    assert "batting .000" in window and "zero mentions" in window
    assert "self-reported" in window
    # dollar-fabrication rule
    assert "Never state a DOLLAR P&L" in P
    assert "8,839.28" in P, "anchor the observed dollar fabrication"
    _ok("§4 prompt: how-did-I-do both-sources rule + no-dollar-P&L rule")


if __name__ == "__main__":
    print("=== chat-stated trades smoke ===")
    test_trade_regex_precision()
    test_get_recent_user_chat_trades_and_executor()
    test_executor_empty_only_when_both_empty()
    test_prompt_rules()
    print("\nALL CHAT-STATED TRADES SMOKE TESTS PASS")
