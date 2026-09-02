"""One-line ops pings to OPS_ALERT_CHANNEL_ID, from any context.

Extracted from discord_bot/bot.py on 2026-09-01 so worker threads that
never touch the bot (the Dropbox poller, the calendar job) can raise an
alarm without importing the 10k-line bot module. Sends via the REST API
rather than the gateway client, so a sick client cannot also take down
its own alarm. Best-effort by contract: a failed alert only logs.

State (`_OPS_ALERT_LAST`, `_OPS_ALERT_MIN_INTERVAL_S`) lives here and is
re-exported by bot.py, so tests that clear `bot._OPS_ALERT_LAST` still
clear the live dict.
"""
import logging

from config import settings

log = logging.getLogger(__name__)

_OPS_ALERT_LAST: dict[str, float] = {}
_OPS_ALERT_MIN_INTERVAL_S = 3600


def _should_send(text: str, dedupe_key: str) -> str | None:
    """Channel id to post to, or None when disabled or rate-limited."""
    cid = (settings.ops_alert_channel_id or "").strip()
    if not cid:
        return None
    import time as _time
    key = dedupe_key or text[:40]
    now = _time.time()
    if now - _OPS_ALERT_LAST.get(key, 0) < _OPS_ALERT_MIN_INTERVAL_S:
        return None
    _OPS_ALERT_LAST[key] = now
    return cid


def _post(cid: str, text: str) -> None:
    import json as _json
    import urllib.request as _rq
    req = _rq.Request(
        f"https://discord.com/api/v10/channels/{cid}/messages",
        data=_json.dumps({"content": text[:1900]}).encode(),
        headers={"Authorization": "Bot " + settings.discord_bot_token,
                 "Content-Type": "application/json",
                 "User-Agent": "omnibeta-ops"},
        method="POST")
    _rq.urlopen(req, timeout=10)


def ops_alert_sync(text: str, dedupe_key: str = "") -> None:
    """Blocking variant for worker threads (never call on the loop)."""
    cid = _should_send(text, dedupe_key)
    if not cid:
        return
    try:
        _post(cid, text)
    except Exception as e:
        log.warning(f"ops alert failed: {e}")


async def ops_alert(text: str, dedupe_key: str = "") -> None:
    """Async variant: the POST runs in a thread so the loop never blocks."""
    cid = _should_send(text, dedupe_key)
    if not cid:
        return
    import asyncio as _aio
    try:
        await _aio.to_thread(_post, cid, text)
    except Exception as e:
        log.warning(f"ops alert failed: {e}")
