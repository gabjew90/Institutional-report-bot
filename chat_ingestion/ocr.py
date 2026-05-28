"""Gemini-vision OCR for chat image attachments.

Two callers:

  ocr_chat_message_images(bot, discord_message_id, ...)
    Cache-first OCR for a stored chat_messages row. Used by both:
      - Phase 1 (lazy): /ask context builder calls this when assembling
        a block that will reference messages with image attachments.
      - Phase 2 (eager): ingest_message kicks this off as a background
        task for channels in chat_eager_ocr_channels.

  ocr_attachments_inline(message, ...)
    Direct OCR of attachments on a live discord.Message. Used by the
    eager path so we don't have to re-fetch a message we already have.

Caching:
  - Once chat_messages.image_ocr_text is populated (any value, including
    NULL with status='no_images'/'failed'), the helper returns the
    cached value without calling Gemini again. Pass `force=True` to
    re-OCR.
  - Discord CDN URLs expire ~24h after issuance. When we encounter a
    stale URL we refetch the message via channel.fetch_message() to
    get fresh signed URLs. This is one extra Discord API call per
    cache miss but happens at most once per image.

Failure mode:
  - Any unexpected error returns None, writes status='failed', logs the
    exception, and continues. OCR is never blocking — /ask should
    still render the line without [IMAGE:...] if extraction errors out.

Cost model (gemini-3.1-flash-lite, ~$0.10/M input, $0.40/M output):
  - One image ≈ 1500 input tokens + 300 output tokens ≈ $0.0003/image
  - Lazy /ask, capped at 3 images/call ≈ $0.001/call ≈ $3/mo at 100/day
  - Eager gain-loss-porn, ~50 posts/day ≈ $0.015/day ≈ $0.50/mo
"""

import asyncio
import logging

import discord
from google import genai
from google.genai import types

import db
from config import settings

log = logging.getLogger(__name__)


# Cap on how many images per message we OCR. If someone posts a
# carousel of 8 screenshots we don't want to spend 8 Gemini calls on
# one message — the first 2 usually capture the signal.
_MAX_IMAGES_PER_MESSAGE = 2

# Max bytes we'll download per image attachment. Discord caps at 25 MB
# for users but most screenshots are <1 MB. This guards against a
# pathological video-as-image upload bloating the OCR payload.
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

# Per-image total timeout (download + Gemini call). Beyond this we
# bail and write status='failed' so /ask doesn't hang on a slow image.
_PER_IMAGE_TIMEOUT_SEC = 30


_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.google_api_key)
    return _client


_OCR_PROMPT = """\
You are extracting readable content from a screenshot posted in a \
trading group chat. Most images here fall into one of these categories:

  - Brokerage screens (Robinhood, Schwab, IBKR, Webull) showing \
positions, P&L, order tickets, or notifications
  - Chart screenshots (TradingView, brokerage chart) with tickers, \
prices, indicators, drawings
  - Crypto wallet / exchange screens
  - News articles, tweets, Discord screenshots
  - Memes (often with text overlay)

Your job: extract everything readable in 2 sections.

SECTION 1 — TEXT (every readable word, line by line, top to bottom):
List every text element visible: tickers, dollar amounts, percentages, \
button labels, timestamps, headlines, captions, usernames. Preserve \
the order they appear on screen.

SECTION 2 — SUMMARY (1-2 sentences):
Describe what the image is showing. If it's a trade screen, name the \
ticker / strike / direction / current P&L. If it's a chart, name the \
ticker and key levels visible. If it's a meme or non-trade content, \
say so.

Output format (exactly this shape, no extra prose, no markdown):

TEXT:
<every readable line>

SUMMARY:
<1-2 sentence description>
"""


async def _gemini_vision_extract(
    image_payloads: list[tuple[bytes, str]],
) -> str | None:
    """Send 1-N images + the OCR prompt to Gemini. Returns the raw text
    response, or None on failure.
    """
    if not image_payloads:
        return None
    try:
        client = _get_client()
        parts = [
            types.Part.from_bytes(data=img_bytes, mime_type=mime)
            for img_bytes, mime in image_payloads
        ]
        parts.append(types.Part.from_text(text=_OCR_PROMPT))
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=parts,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=800,
            ),
        )
        text = (response.text or "").strip()
        return text or None
    except Exception as e:
        log.warning(f"Gemini OCR call failed: {type(e).__name__}: {e}")
        return None


async def _resolve_discord_message(
    bot: discord.Client,
    *,
    channel_id: int,
    discord_message_id: int,
) -> discord.Message | None:
    """Fetch the live Discord message so we have fresh signed URLs +
    Attachment objects (which discord.py can .read() with auth).
    Returns None if the channel/message is gone or unreadable.
    """
    try:
        channel = bot.get_channel(int(channel_id))
        if channel is None:
            channel = await bot.fetch_channel(int(channel_id))
        if channel is None:
            return None
        return await channel.fetch_message(int(discord_message_id))
    except discord.NotFound:
        log.debug(f"OCR resolve: message {discord_message_id} deleted")
        return None
    except discord.Forbidden:
        log.debug(f"OCR resolve: no access to channel {channel_id}")
        return None
    except Exception as e:
        log.warning(
            f"OCR resolve failed for msg={discord_message_id}: "
            f"{type(e).__name__}: {e}"
        )
        return None


async def ocr_chat_message_images(
    bot: discord.Client,
    discord_message_id: int,
    *,
    channel_id: int | None = None,
    force: bool = False,
) -> str | None:
    """Return OCR text for image attachments on this Discord message.

    Cache path: if chat_messages.image_ocr_status is non-NULL (and
    force=False), return the cached image_ocr_text (which may itself
    be None for 'no_images' / 'failed' status).

    Cache miss: refetch the message from Discord, download up to
    _MAX_IMAGES_PER_MESSAGE image attachments, OCR via Gemini, write
    result back to chat_messages, return the text.

    Returns None when the message has no image attachments, OCR errored
    out, or the message is no longer accessible.
    """
    row = db.get_chat_message_row(discord_message_id)
    if row is None:
        log.debug(f"OCR: no chat_messages row for {discord_message_id}")
        return None

    if not force and row.get("image_ocr_status"):
        # Cached — return whatever we have (success text, or None for
        # no_images / failed statuses).
        return row.get("image_ocr_text") or None

    if not row.get("has_attachments"):
        db.set_chat_image_ocr(
            discord_message_id, ocr_text=None, status="no_images"
        )
        return None

    chan_id = channel_id if channel_id is not None else row.get("channel_id")
    if chan_id is None:
        log.warning(f"OCR: no channel_id for msg {discord_message_id}")
        return None

    message = await _resolve_discord_message(
        bot, channel_id=int(chan_id), discord_message_id=int(discord_message_id)
    )
    if message is None:
        # Message gone / inaccessible. Mark failed so we don't retry on
        # every /ask.
        db.set_chat_image_ocr(
            discord_message_id, ocr_text=None, status="failed"
        )
        return None

    image_payloads: list[tuple[bytes, str]] = []
    for att in message.attachments[:_MAX_IMAGES_PER_MESSAGE]:
        ct = (getattr(att, "content_type", None) or "").lower()
        if not ct.startswith("image/"):
            continue
        if getattr(att, "size", 0) > _MAX_IMAGE_BYTES:
            continue
        try:
            img_bytes = await asyncio.wait_for(
                att.read(), timeout=15
            )
        except Exception as e:
            log.debug(
                f"OCR: attachment read failed for msg={discord_message_id}: "
                f"{type(e).__name__}: {e}"
            )
            continue
        image_payloads.append((img_bytes, ct))

    if not image_payloads:
        # has_attachments was true but none were images (or all failed
        # to download). Cache as no_images so we don't retry.
        db.set_chat_image_ocr(
            discord_message_id, ocr_text=None, status="no_images"
        )
        return None

    try:
        ocr_text = await asyncio.wait_for(
            _gemini_vision_extract(image_payloads),
            timeout=_PER_IMAGE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning(f"OCR: timed out for msg={discord_message_id}")
        db.set_chat_image_ocr(
            discord_message_id, ocr_text=None, status="failed"
        )
        return None

    if not ocr_text:
        db.set_chat_image_ocr(
            discord_message_id, ocr_text=None, status="failed"
        )
        return None

    db.set_chat_image_ocr(
        discord_message_id, ocr_text=ocr_text, status="success"
    )
    return ocr_text


async def ocr_attachments_inline(
    message: discord.Message,
    *,
    force: bool = False,
) -> str | None:
    """Eager-OCR path for ingest_message. Takes a live discord.Message
    that the bot just received (so we already have valid Attachment
    objects and don't need to refetch). Writes the result to
    chat_messages.image_ocr_text by discord_message_id.

    Skips when:
      - Message has no image attachments
      - Cached result already exists (unless force=True)
      - OCR errors out (logs + caches 'failed' status)
    """
    if force:
        cached_status = None
    else:
        row = db.get_chat_message_row(message.id)
        cached_status = (row or {}).get("image_ocr_status")
    if cached_status:
        return (row or {}).get("image_ocr_text") if not force else None

    image_payloads: list[tuple[bytes, str]] = []
    for att in (message.attachments or [])[:_MAX_IMAGES_PER_MESSAGE]:
        ct = (getattr(att, "content_type", None) or "").lower()
        if not ct.startswith("image/"):
            continue
        if getattr(att, "size", 0) > _MAX_IMAGE_BYTES:
            continue
        try:
            img_bytes = await asyncio.wait_for(att.read(), timeout=15)
        except Exception as e:
            log.debug(
                f"OCR inline: attachment read failed for msg={message.id}: "
                f"{type(e).__name__}: {e}"
            )
            continue
        image_payloads.append((img_bytes, ct))

    if not image_payloads:
        db.set_chat_image_ocr(
            message.id, ocr_text=None, status="no_images"
        )
        return None

    try:
        ocr_text = await asyncio.wait_for(
            _gemini_vision_extract(image_payloads),
            timeout=_PER_IMAGE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        log.warning(f"OCR inline: timed out for msg={message.id}")
        db.set_chat_image_ocr(
            message.id, ocr_text=None, status="failed"
        )
        return None

    if not ocr_text:
        db.set_chat_image_ocr(
            message.id, ocr_text=None, status="failed"
        )
        return None

    db.set_chat_image_ocr(
        message.id, ocr_text=ocr_text, status="success"
    )
    log.info(
        f"OCR inline: msg={message.id} channel='{getattr(message.channel, 'name', '?')}' "
        f"extracted {len(ocr_text)} chars from {len(image_payloads)} image(s)"
    )
    return ocr_text
