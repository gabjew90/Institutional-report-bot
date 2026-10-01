"""Run one /ask question through the production pipeline and print what the
channel would see. The live-replica harness behind .claude/skills/ask-harness.

    /opt/venv/bin/python scripts/ask_live.py "what do you think of MU" \
        --asker kloh --channel general

Runs on the worker (`railway ssh`), where the live database, the API keys
and the network are the bot's own. It calls the real `_answer_with_gemini`
(the same router, prompt, tools, model, validators and retry ladder the
channel runs), so a change to any of those is measured, not mirrored.

What differs from the channel, and only this:
  * the database is opened READ-ONLY. The two /ask writes (the daily quota
    row and the /data/ask-logs QC entry), the Gemini spend ledger and the
    bot's cross-window answer memory are replaced by stubs that capture
    their arguments for the report. A run here therefore never appears in
    the QC log, the quota or the spend ledger.
  * recent channel chat is rebuilt from `chat_messages` (the bot's own
    copy of the channel) instead of Discord's history API, in the same
    "speaker: text" block. Image attachments are not OCR'd on the fly.
  * `<@id>` mentions are not resolved through the guild; type names.

Never run it with a writable connection, and never import this module in
the worker process. Exit code is 0 on an answer, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CAPTURED: dict = {"ask_log": None, "gemini_calls": [], "quota_increments": 0}


def _adopt_worker_env() -> None:
    """Re-exec with the worker process's LD_LIBRARY_PATH. A `railway ssh`
    shell lacks the Nix gcc lib path the worker (PID 1) runs with, so
    numpy, and with it yfinance, fails to import and every Yahoo-backed
    tool (options chain, price history) returned an error that the
    channel never sees (2026-09-30, the ACN run). libz is the other half:
    it is not on that path either, and the worker only resolves it because
    the interpreter maps it when `zlib` is imported, so main() imports
    zlib before anything pulls in numpy."""
    if os.environ.get("ASK_LIVE_ENV_ADOPTED"):
        return
    try:
        with open("/proc/1/environ", "rb") as h:
            env1 = dict(kv.split("=", 1) for kv in h.read().decode(errors="replace").split("\0") if "=" in kv)
    except OSError:
        return                                   # not on the worker (local run)
    want = env1.get("LD_LIBRARY_PATH")
    if not want or want == os.environ.get("LD_LIBRARY_PATH"):
        return
    env = dict(os.environ, LD_LIBRARY_PATH=want, ASK_LIVE_ENV_ADOPTED="1")
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def _open_readonly(db_path: str) -> sqlite3.Connection:
    # The executors read from worker threads (asyncio.to_thread), so the
    # one shared connection must allow cross-thread use.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _install_stubs(db, conn: sqlite3.Connection) -> None:
    """Point every db call at the read-only connection and turn the /ask
    path's writes into captures. Anything else that tries to write raises
    `attempt to write a readonly database`, which is the right outcome:
    it names a write this harness did not know about."""
    db.get_connection = lambda: conn

    def _record_ask_query(user_id, *a, **k):
        CAPTURED["quota_increments"] += 1

    def _append_ask_interaction(**kw):
        CAPTURED["ask_log"] = kw

    def _record_gemini_call(**kw):
        CAPTURED["gemini_calls"].append(kw)

    def _record_ask_bot_answer(**kw):
        return None

    def _set_chat_image_ocr(*a, **kw):
        # the lazy image-OCR cache; the chat-search tool reaches it and its
        # call sites are the failure handlers themselves
        return None

    db.record_ask_query = _record_ask_query
    db.append_ask_interaction = _append_ask_interaction
    db.record_gemini_call = _record_gemini_call
    db.record_ask_bot_answer = _record_ask_bot_answer
    db.set_chat_image_ocr = _set_chat_image_ocr


def _chat_block(conn: sqlite3.Connection, channel_name: str, bot_user_id: int | None,
                max_messages: int, max_age_min: int, per_msg_chars: int) -> tuple[str, int | None]:
    """The channel's recent chat in the exact shape of bot._fetch_chat_context,
    read from chat_messages. Returns (block, channel_id)."""
    from discord_bot import chat_context as cc
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_min)).strftime("%Y-%m-%dT%H:%M:%S")
    # posted_at mixes SQLite's space form and Python's T form (CLAUDE.md);
    # normalise in both the filter and the sort.
    rows = conn.execute(
        "SELECT channel_id, author_id, author_username, author_display, content, embed_texts, "
        "has_attachments, posted_at FROM chat_messages WHERE channel_name = ? "
        "AND replace(posted_at, ' ', 'T') >= ? ORDER BY replace(posted_at, ' ', 'T') DESC LIMIT ?",
        (channel_name, cutoff, max_messages)).fetchall()
    if not rows:
        return "", None
    lines = []
    for r in reversed(rows):                      # oldest -> newest
        text = (r["content"] or "").strip()
        if not text and r["embed_texts"]:
            try:
                text = " | ".join(t for t in json.loads(r["embed_texts"]) if t).strip()
            except Exception:
                text = ""
        if not text and not r["has_attachments"]:
            continue
        text = text[:per_msg_chars]
        if bot_user_id is not None and r["author_id"] == bot_user_id:
            lines.append(cc.line(cc.BOT_SPEAKER, text))
            continue
        lines.append(cc.line(cc.speaker(r["author_display"], r["author_username"]), text))
    if not lines:
        return "", rows[0]["channel_id"]
    return cc.block(lines), rows[0]["channel_id"]


def _bot_user_id(conn: sqlite3.Connection) -> int | None:
    """The bot's own author_id: the author whose recent embeds carry the
    /ask NFA footer. The ingestion feed and webhooks post embeds too, so
    counting embed-only posts would pick the wrong account."""
    from discord_bot import chat_context as cc
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
    row = conn.execute(
        "SELECT author_id FROM chat_messages WHERE replace(posted_at, ' ', 'T') >= ? "
        "AND embed_texts LIKE ? GROUP BY author_id ORDER BY COUNT(*) DESC LIMIT 1",
        (since, f"%{cc.NFA_FOOTER}%")).fetchone()
    return int(row[0]) if row else None


def _render(embeds, files, out_dir: str | None) -> str:
    parts = []
    for e in embeds:
        if getattr(e, "title", None):
            parts.append(f"# {e.title}")
        if getattr(e, "description", None):
            parts.append(e.description)
        for f in getattr(e, "fields", []) or []:
            parts.append(f"**{f.name}**\n{f.value}")
        footer = getattr(getattr(e, "footer", None), "text", None)
        if footer:
            parts.append(f"_{footer}_")
        parts.append(f"(embed color #{e.color.value:06x})" if getattr(e, "color", None)
                     else "(embed, no color)")
    if files and out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for i, f in enumerate(files):
            path = os.path.join(out_dir, getattr(f, "filename", f"file{i}"))
            fp = getattr(f, "fp", None)
            if fp is not None:
                fp.seek(0)
                with open(path, "wb") as h:
                    h.write(fp.read())
            parts.append(f"(attachment saved: {path})")
    return "\n\n".join(parts)


def main(argv: list[str]) -> int:
    _adopt_worker_env()
    import zlib  # noqa: F401  maps libz.so.1 before numpy's extensions need it (see above)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question")
    ap.add_argument("--asker", default="", help="Discord username of the asker (resolved from the DB)")
    ap.add_argument("--asker-id", type=int, default=0, help="asker's Discord user id when the name is not on file")
    ap.add_argument("--channel", default="general", help="channel name; its recent chat becomes the context block")
    ap.add_argument("--no-chat", action="store_true", help="omit the recent-chat block")
    ap.add_argument("--db", default="", help="database path (default: settings.db_path)")
    ap.add_argument("--json", default="", help="also write the full trace (prompt, tools, meta, answer) here")
    ap.add_argument("--files-dir", default="", help="where to save chart attachments, if any")
    args = ap.parse_args(argv)

    import db
    from config import settings
    conn = _open_readonly(args.db or settings.db_path)
    _install_stubs(db, conn)
    # bot.py binds `db` at import; the stubs above replace attributes on the
    # module object, so they hold there too.
    import discord_bot.bot as bot

    asker_id = args.asker_id or (db.resolve_username_to_user_id(args.asker) if args.asker else 0) or 0
    if args.asker and not asker_id:
        print(f"asker {args.asker!r} not found in user_profiles or chat_messages; pass --asker-id", file=sys.stderr)
        return 2
    prof = db.get_user_profile(asker_id) if asker_id else None
    asker_username = (prof or {}).get("username") or args.asker
    asker_display = (prof or {}).get("display_name") or asker_username

    bot_uid = _bot_user_id(conn)
    chat_block, channel_id = ("", None) if args.no_chat else _chat_block(
        conn, args.channel, bot_uid, bot._ASK_CONTEXT_MAX_MESSAGES,
        bot._ASK_CONTEXT_MAX_AGE_MIN, bot._ASK_CONTEXT_PER_MSG_CHARS)
    if channel_id is None:
        row = conn.execute("SELECT channel_id FROM chat_messages WHERE channel_name = ? LIMIT 1",
                           (args.channel,)).fetchone()
        channel_id = int(row[0]) if row else None

    question = args.question
    # Same pre-processing as the slash command: fetch any URL the question
    # carries, load profiles for the asker, add other members' verbatim.
    async def go():
        fetched_urls = await bot._maybe_fetch_user_urls(question)
        mentioned = []
        try:
            mentioned = db.find_users_mentioned_in_text(question)
        except Exception:
            pass
        q = question
        if mentioned:
            sv = bot._format_subject_verbatim_block(mentioned, exclude_user_id=asker_id)
            if sv:
                q = f"{sv}\n\n{q}"
        out_meta: dict = {}
        result = await bot._answer_with_gemini(
            q, asker_id, chat_context=chat_block, fetched_urls=fetched_urls,
            profile_user_ids=[asker_id] if asker_id else [],
            asker_display_name=asker_display, asker_username=asker_username,
            channel_name=args.channel, channel_id=channel_id, out_meta=out_meta)
        return bot._normalize_ask_result(result), out_meta

    (embeds, files), out_meta = asyncio.run(go())

    log = CAPTURED["ask_log"] or {}
    meta = log.get("meta") or {}
    trace = log.get("tool_trace") or []
    print("=" * 72)
    print(f"Q ({asker_username or 'anon'} in #{args.channel}): {question}")
    print(f"route: {meta.get('route_shape', '?')}  grounded: {meta.get('grounded')}  "
          f"sources: {meta.get('sources')}  retries: {meta.get('retry_calls')}  "
          f"latency: {meta.get('latency_s')}s  guards: {meta.get('guards')}")
    if trace:
        print("tools:")
        for t in trace:
            if isinstance(t, dict):
                print(f"  - {t.get('tool') or t.get('name')}: {t.get('status', '')} {json.dumps(t.get('args', {}), default=str)[:160]}")
            else:
                print(f"  - {t}")
    print(f"gemini calls: {len(CAPTURED['gemini_calls'])}  chat lines: {chat_block.count(chr(10)) if chat_block else 0}")
    print("-" * 72)
    print(_render(embeds, files, args.files_dir or None))
    print("=" * 72)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as h:
            json.dump({"question": question, "asker": asker_username, "channel": args.channel,
                       "chat_block": chat_block, "ask_log": log, "gemini_calls": CAPTURED["gemini_calls"],
                       "out_meta": out_meta,
                       "answer": [getattr(e, "description", "") for e in embeds]},
                      h, default=str, indent=1)
        print(f"trace written: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
