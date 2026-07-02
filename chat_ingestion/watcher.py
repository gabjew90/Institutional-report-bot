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

import asyncio
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


# Eager-OCR concurrency cap. Lazily initialized on first use so we have
# a live asyncio event loop (Semaphore binds to the loop at creation).
# Cap is sourced from settings.eager_ocr_max_concurrent; without this
# a burst of image messages spawns N concurrent Gemini OCR calls and
# either hits rate limits or starves PDF analysis / /ask of capacity.
_eager_ocr_semaphore: asyncio.Semaphore | None = None


def _get_eager_ocr_semaphore() -> asyncio.Semaphore:
    """Return the module-level eager-OCR semaphore, creating it on demand.

    Built lazily so it binds to whatever event loop is running when
    on_message first fires (matches discord.py's loop, not whatever
    loop happened to exist at import time).
    """
    global _eager_ocr_semaphore
    if _eager_ocr_semaphore is None:
        cap = max(1, int(getattr(settings, "eager_ocr_max_concurrent", 3)))
        _eager_ocr_semaphore = asyncio.Semaphore(cap)
        log.info(f"Eager OCR concurrency cap = {cap}")
    return _eager_ocr_semaphore


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


async def ingest_message(
    message: discord.Message,
    *,
    trigger_eager_ocr: bool = True,
) -> bool:
    """Store a Discord message in chat_messages if it's in a configured
    ingestion channel. Returns True if a new row was written, False if
    filtered, deduped, or errored.

    Bot messages are skipped (bot's own replies + ingestion-feed embeds
    aren't part of the chat we care about preserving).

    `trigger_eager_ocr`: when True (default, live on_message path),
    image attachments in chat_eager_ocr_channels kick off background
    OCR via asyncio.create_task. The catchup path passes False to
    avoid flooding Gemini with hundreds of concurrent OCR tasks during
    a 30-day backfill — lazy OCR handles those rows on demand via /ask.
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

    stored = db.store_chat_message(
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

    # Eager OCR (Phase 2). For channels in chat_eager_ocr_channels with
    # image attachments, fire the OCR pass as a background task so the
    # ingest call returns immediately. The OCR result lands in
    # chat_messages.image_ocr_text a few seconds later.
    #
    # We use create_task (no await) because:
    #   - on_message is dispatched concurrently across handlers but a
    #     slow handler still chains within itself (e.g. mention reply
    #     after ingest). Synchronous OCR here would add 2-3s before
    #     any analyst/mention work that follows in the dispatch chain.
    #   - The task is non-critical: if it fails, we just have no OCR
    #     text — /ask falls back to the lazy path on next reference.
    if stored and attachment_urls and trigger_eager_ocr:
        try:
            eager_channels = settings.resolve_chat_eager_ocr_channels()
            if chan_name in eager_channels:
                from chat_ingestion.ocr import ocr_attachments_inline
                asyncio.create_task(
                    _safe_ocr_inline(message, chan_name),
                    name=f"eager_ocr_{message.id}",
                )
        except Exception as e:
            log.warning(
                f"Eager OCR dispatch failed for msg={message.id}: "
                f"{type(e).__name__}: {e}"
            )

    return stored


async def _safe_ocr_inline(message: discord.Message, chan_name: str) -> None:
    """Run the existing image-OCR pipeline AND the new text+vision
    classifier under one semaphore acquisition.

    Step 1: ocr_attachments_inline — the original path. Writes
    extraction_source='image' rows for messages with image
    attachments that look trade-shaped to the image-only OCR.

    Step 2: _classify_message_for_trade — new path (2026-06-02).
    Picks up text-only trade narratives that the image-only pipeline
    misses entirely (e.g., ZHawk's 'PURR Leaps 12/18 $14 @4.10'
    posts that have no screenshot). Two-tier dedup in
    db.insert_text_extracted_trade_if_not_dup prevents creating a
    second row for messages where Step 1 already wrote one.

    Both steps share the eager-OCR semaphore so concurrent calls are
    capped at settings.eager_ocr_max_concurrent (default 3).
    """
    sem = _get_eager_ocr_semaphore()
    async with sem:
        # Step 1: existing image-OCR pipeline — unchanged
        try:
            from chat_ingestion.ocr import ocr_attachments_inline
            await ocr_attachments_inline(message)
        except Exception as e:
            log.warning(
                f"Eager OCR (image path) failed for msg={message.id} "
                f"channel='{chan_name}': {type(e).__name__}: {e}"
            )

        # Step 2: NEW text+vision classifier
        try:
            await _classify_message_for_trade(message, chan_name)
        except Exception as e:
            log.warning(
                f"Eager text classifier failed for msg={message.id} "
                f"channel='{chan_name}': {type(e).__name__}: {e}"
            )


async def _classify_message_for_trade(
    message: discord.Message, chan_name: str,
) -> None:
    """Run the unified text+vision classifier on a Discord message.
    Writes a row to analyst_trades if the classifier identifies a
    trade. Dedup against existing rows (image + prior text) happens
    inside db.insert_text_extracted_trade_if_not_dup.
    """
    from analyst_log.ocr import extract_trade_from_message

    text = (message.content or "").strip()
    image_bytes: list[bytes] = []
    for att in (message.attachments or []):
        ct = (getattr(att, "content_type", "") or "").lower()
        if not ct.startswith("image/"):
            continue
        try:
            data = await att.read()
            image_bytes.append(data)
        except Exception as e:
            log.debug(
                f"text classifier: attachment read failed "
                f"msg={message.id}: {e}"
            )

    extracted = await extract_trade_from_message(
        text=text,
        image_bytes_list=image_bytes,
        author_username=message.author.name,
        channel_name=chan_name,
    )
    if not extracted:
        return

    inserted = db.insert_text_extracted_trade_if_not_dup(
        author_id=message.author.id,
        author_username=message.author.name,
        discord_message_id=message.id,
        posted_at=message.created_at.isoformat(),
        extracted=extracted,
        channel_name=chan_name,
    )
    if inserted:
        log.info(
            f"text classifier: wrote row for {message.author.name} in "
            f"{chan_name} — {extracted.get('ticker')} {extracted.get('action')}"
        )
    else:
        log.debug(
            f"text classifier: deduped against existing row for "
            f"msg={message.id}"
        )


async def run_chat_catchup(
    bot: discord.Client,
    *,
    reason: str = "startup",
    force: bool = False,
    force_full_window: bool = False,
) -> int:
    """Scan each configured chat-ingestion channel for messages we may
    have missed during a downtime / gateway flap. Idempotent via the
    UNIQUE constraint on discord_message_id — already-stored rows
    silently skip.

    Scan window per channel:
      `force_full_window=False` (default) →
        max(latest_stored_posted_at - 1h buffer, now - 30d hard cap)
      `force_full_window=True` →
        now - 30d (ignore latest_stored, scan the full window)

    Use force_full_window=True when you suspect there are GAPS in the
    stored data — the MAX-based resume will hide gaps if any recent
    messages are present (live ingestion writes today's messages, MAX
    moves forward, the gap behind it stays unfilled).

    `force=True` bypasses the 2-minute rate-limit guard.

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

        if force_full_window:
            # Recovery mode — ignore MAX(posted_at), scan the full 30d
            # window. Dedup handles already-stored rows silently.
            scan_from = hard_floor
        else:
            # GAP-AWARE RESUME (fix #3). Two-tier resume:
            #   1. If chat_messages has an internal gap (>=60 min between
            #      consecutive stored messages within the 30d window),
            #      resume from the START of the oldest gap to fill it.
            #      This catches the case where live ingestion advanced
            #      MAX(posted_at) past a historical hole.
            #   2. Otherwise, resume from MAX(posted_at) - 1h buffer
            #      (the simple incremental path — coverage is contiguous
            #      so we just need to pick up where we left off).
            #   3. Cold-start for a brand-new channel: scan the full 30d.
            scan_from = None
            try:
                gap_iso = db.find_oldest_chat_gap(target.id, days=_CATCHUP_HARD_CAP_DAYS, gap_minutes=60)
            except Exception as e:
                log.warning(f"Chat catchup: gap-detect failed: {e}")
                gap_iso = None
            resume_basis_iso = gap_iso or db.get_latest_chat_message_posted_at(target.id)
            if resume_basis_iso:
                try:
                    norm = resume_basis_iso.replace(" ", "T")
                    if norm.endswith("Z"):
                        norm = norm[:-1] + "+00:00"
                    resume_dt = datetime.fromisoformat(norm)
                    if resume_dt.tzinfo is None:
                        resume_dt = resume_dt.replace(tzinfo=timezone.utc)
                    # Tiny safety buffer — overlap by a minute so a message
                    # at the exact gap-edge isn't missed by an exclusive
                    # `after` filter.
                    scan_from = max(
                        resume_dt - timedelta(minutes=_CATCHUP_BUFFER_MIN),
                        hard_floor,
                    )
                    if gap_iso:
                        log.info(
                            f"Chat catchup ({reason}): #{chan_name} — "
                            f"detected gap, resuming from {scan_from.isoformat()} "
                            f"(gap_start={gap_iso[:19]})"
                        )
                except Exception as e:
                    log.warning(f"Chat catchup: bad timestamp {resume_basis_iso!r}: {e}")
                    scan_from = hard_floor
            else:
                # Cold-start for this channel — scan the full 30-day window
                scan_from = hard_floor

        # Per-channel scan with retry + resume-from-high-water-mark.
        # Heavy channels (stonks-yapping at 10k+ msgs over 30 days) can
        # trip Discord gateway / HTTP errors mid-iteration. Without
        # retry the iterator raises, the catch-up exits early, and the
        # gap stays unfilled — that's how Phase 1 deployed with the
        # Apr 25 → May 20 month of stonks-yapping data missing on first
        # run.
        #
        # `last_msg` tracks the most-recent Discord message object we've
        # iterated. On retry we pass `after=last_msg` (exclusive) so the
        # next attempt picks up after the failed point WITHOUT re-paging
        # everything we already saw. Without this, a 3-attempt retry on
        # an oldest-first 10k-msg channel would page the same 10k msgs
        # up to 3 times.
        import asyncio
        _MAX_SCAN_ATTEMPTS = 3
        _RETRY_BACKOFF = [5, 15, 30]

        new_this_channel = 0
        seen_this_channel = 0
        last_msg: discord.Message | None = None
        for attempt in range(1, _MAX_SCAN_ATTEMPTS + 1):
            kwargs: dict = {"limit": None, "oldest_first": True}
            if last_msg is not None:
                # Resume from where the previous attempt left off
                kwargs["after"] = last_msg
            else:
                kwargs["after"] = scan_from
            try:
                async for msg in target.history(**kwargs):
                    seen_this_channel += 1
                    last_msg = msg
                    if msg.author.bot:
                        continue
                    # Catchup mode: skip eager OCR. A 30d backfill of
                    # gain-loss-porn would otherwise spawn hundreds of
                    # concurrent OCR tasks. Lazy OCR handles those rows
                    # on /ask demand instead.
                    if await ingest_message(msg, trigger_eager_ocr=False):
                        new_this_channel += 1
                break  # full iteration succeeded
            except Exception as e:
                if attempt < _MAX_SCAN_ATTEMPTS:
                    wait_s = _RETRY_BACKOFF[attempt - 1]
                    resume_marker = (
                        last_msg.created_at.isoformat()
                        if last_msg is not None
                        else scan_from.isoformat()
                    )
                    log.warning(
                        f"Chat catchup #{chan_name} hit "
                        f"{type(e).__name__}: {str(e)[:120]} after "
                        f"{seen_this_channel} msgs / "
                        f"{new_this_channel} new — retry {attempt}/"
                        f"{_MAX_SCAN_ATTEMPTS - 1} in {wait_s}s "
                        f"(resume from {resume_marker[:19]})"
                    )
                    await asyncio.sleep(wait_s)
                else:
                    log.warning(
                        f"Chat catchup #{chan_name} FAILED after "
                        f"{_MAX_SCAN_ATTEMPTS} attempts: "
                        f"{type(e).__name__}: {e}. Partial data preserved "
                        f"({seen_this_channel} msgs / {new_this_channel} "
                        f"new written); will retry on next on_ready / "
                        f"on_resumed.",
                        exc_info=True,
                    )

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

    # Observability (fix #7): record run summary so we can see catchup
    # frequency / volume historically without spelunking logs.
    try:
        db.record_pipeline_event(
            "chat_catchup",
            "completed",
            {
                "reason": reason,
                "force_full_window": bool(force_full_window),
                "channels_configured": len(target_names),
                "total_new_rows": total_new,
            },
        )
    except Exception as e:
        log.debug(f"Chat catchup: could not record pipeline event: {e}")

    return total_new
