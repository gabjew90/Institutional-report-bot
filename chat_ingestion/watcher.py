"""Live and catch-up ingestion of Discord chat into chat_messages.

Two entry points:

  ingest_message(message)
    Called from bot.on_message for every message the bot sees. Filters
    to the configured channel allowlist and stores a row. Idempotent
    via the UNIQUE(discord_message_id) constraint.

  run_chat_catchup(bot, reason)
    Called from bot.on_ready and bot.on_resumed. For each configured
    channel, scans Discord history since the latest stored posted_at
    (or 30 days back on cold-start) and stores anything new. Closes
    the gap on gateway flaps + bootstraps the table the first time
    the bot runs after this feature lands.

The store is best-effort — failures log a warning and return False
rather than raising. Chat ingestion should never block message
delivery or other bot work.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import discord

import db
from config import settings

log = logging.getLogger(__name__)


# Catch-up rate limiting — on_resumed can fire many times during a
# gateway flap-storm. Without this we'd hammer Discord history endpoints
# every reconnect.
_CATCHUP_MIN_INTERVAL_SEC = 120  # 2 minutes between runs
_CATCHUP_HARD_CAP_DAYS = 30      # never scan more than 30d per channel
_CATCHUP_BUFFER_MIN = 60         # 1h overlap before latest-known msg
_last_catchup_at: dict[str, datetime] = {}


def _extract_embed_texts(embeds) -> list[str]:
    """Pull readable text from Discord embeds (tweet quotes, link
    previews, etc). Keeps the JSON payload small — just the fields
    that carry signal."""
    out: list[str] = []
    for e in embeds or []:
        for attr in ("title", "description"):
            v = getattr(e, attr, None)
            if v and isinstance(v, str):
                out.append(v[:500])
        author = getattr(e, "author", None)
        if author is not None:
            name = getattr(author, "name", None)
            if name and isinstance(name, str):
                out.append(f"by {name[:120]}")
        for f in getattr(e, "fields", []) or []:
            name = getattr(f, "name", None)
            val = getattr(f, "value", None)
            if name and val:
                out.append(f"{name}: {val[:400]}")
    return out


async def ingest_message(message: discord.Message) -> bool:
    """Store a Discord message in chat_messages if it's in a configured
    ingestion channel. Returns True if a new row was written, False if
    filtered, deduped, or errored.

    Bot messages are skipped (bot's own replies + ingestion-feed embeds
    aren't part of the chat we care about preserving).
    """
    if message.author.bot:
        return False
    chan_name = getattr(message.channel, "name", None)
    if not chan_name:
        return False
    allowlist = settings.resolve_chat_ingestion_channels()
    if allowlist and chan_name not in allowlist:
        return False

    content = (message.content or "").strip()
    attachment_urls = [
        a.url for a in (message.attachments or [])
        if getattr(a, "url", None)
    ]
    embed_texts = _extract_embed_texts(message.embeds)

    ref = getattr(message, "reference", None)
    reply_parent_id = getattr(ref, "message_id", None) if ref else None

    author_display = (
        getattr(message.author, "display_name", None) or message.author.name
    )

    return db.store_chat_message(
        discord_message_id=message.id,
        channel_id=message.channel.id,
        channel_name=chan_name,
        author_id=message.author.id,
        author_username=message.author.name,
        author_display=author_display,
        content=content or None,
        posted_at=message.created_at.isoformat(),
        has_attachments=bool(attachment_urls),
        attachment_urls=json.dumps(attachment_urls) if attachment_urls else None,
        embed_texts=json.dumps(embed_texts) if embed_texts else None,
        reply_parent_id=reply_parent_id,
    )


async def run_chat_catchup(
    bot: discord.Client,
    *,
    reason: str = "startup",
    force: bool = False,
) -> int:
    """Scan each configured chat-ingestion channel for messages we may
    have missed during a downtime / gateway flap. Idempotent via the
    UNIQUE constraint on discord_message_id — already-stored rows
    silently skip.

    Scan window per channel:
      max(latest_stored_posted_at - 1h buffer, now - 30d hard cap)

    Returns the count of newly-stored rows across all channels.
    """
    global _last_catchup_at
    now = datetime.now(timezone.utc)
    if not force:
        last = _last_catchup_at.get("global")
        if last and (now - last).total_seconds() < _CATCHUP_MIN_INTERVAL_SEC:
            log.debug(
                f"Chat catchup skipped — ran "
                f"{(now - last).total_seconds():.0f}s ago"
            )
            return 0
    _last_catchup_at["global"] = now

    target_names = settings.resolve_chat_ingestion_channels()
    if not target_names:
        log.debug("Chat catchup: no channels configured")
        return 0

    total_new = 0
    hard_floor = now - timedelta(days=_CATCHUP_HARD_CAP_DAYS)

    for chan_name in target_names:
        target = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == chan_name:
                    target = ch
                    break
            if target:
                break
        if target is None:
            log.debug(f"Chat catchup: channel '{chan_name}' not found")
            continue

        latest_iso = db.get_latest_chat_message_posted_at(target.id)
        if latest_iso:
            try:
                norm = latest_iso.replace(" ", "T")
                if norm.endswith("Z"):
                    norm = norm[:-1] + "+00:00"
                latest_dt = datetime.fromisoformat(norm)
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=timezone.utc)
                scan_from = max(
                    latest_dt - timedelta(minutes=_CATCHUP_BUFFER_MIN),
                    hard_floor,
                )
            except Exception as e:
                log.warning(f"Chat catchup: bad timestamp {latest_iso!r}: {e}")
                scan_from = hard_floor
        else:
            # Cold-start for this channel — scan the full 30-day window
            scan_from = hard_floor

        new_this_channel = 0
        seen_this_channel = 0
        try:
            async for msg in target.history(
                limit=None, after=scan_from, oldest_first=True
            ):
                seen_this_channel += 1
                if msg.author.bot:
                    continue
                # Reuse the live-ingestion helper for consistent shape
                if await ingest_message(msg):
                    new_this_channel += 1
            if new_this_channel:
                log.info(
                    f"Chat catchup ({reason}): #{chan_name} — "
                    f"scanned {seen_this_channel}, stored "
                    f"{new_this_channel} new rows "
                    f"(from {scan_from.isoformat()})"
                )
                total_new += new_this_channel
            else:
                log.debug(
                    f"Chat catchup ({reason}): #{chan_name} — "
                    f"scanned {seen_this_channel}, nothing new"
                )
        except Exception as e:
            log.warning(
                f"Chat catchup ({reason}) failed for #{chan_name}: {e}",
                exc_info=True,
            )

    return total_new
