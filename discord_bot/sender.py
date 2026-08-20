"""Discord message delivery with chunking and retry.

Hardening (2026-06-01):
- Honors `Retry-After` HTTP header on 429 rate-limit responses (capped
  at MAX_RETRY_AFTER_S so a hostile/buggy header can't park the worker).
- Up to MAX_ATTEMPTS attempts per embed with exponential backoff.
- Returns a structured outcome dict on top of the bool so callers in the
  bridge can log diagnostic detail (how many embeds shipped, last error).
  Callers that only want True/False can read `result["success"]`.
"""

import asyncio
import logging
from typing import Any

import discord

log = logging.getLogger(__name__)

# Maximum number of attempts per embed (initial + retries). Three gives
# enough cushion to absorb a transient Discord 500 + a real Retry-After,
# without parking a hung send forever.
MAX_ATTEMPTS = 3

# Cap on how long we will honor a server-supplied Retry-After. Discord
# rarely asks for more than a few seconds — anything >60s is almost
# certainly a global-rate-limit or an error condition the bridge poll
# can resolve later by simply trying again.
MAX_RETRY_AFTER_S = 60.0


def _extract_retry_after(exc: discord.HTTPException) -> float | None:
    """Parse Retry-After from a discord.HTTPException, returning seconds.

    discord.py exposes the underlying aiohttp response on the exception.
    For 429s, the body usually contains a JSON `retry_after` (float seconds)
    and the HTTP `Retry-After` header echoes the same value. Try both.
    Returns None if nothing parseable is found.
    """
    # 1. JSON body — `e.code` is Discord's code, separate from HTTP. The
    #    JSON payload sits on `e.response` or is reparsed onto `e.text`.
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            header_val = response.headers.get("Retry-After") or response.headers.get(
                "retry-after"
            )
            if header_val:
                return float(header_val)
        except (ValueError, AttributeError):
            pass

    # 2. discord.py sometimes surfaces this as `exc.retry_after`
    val = getattr(exc, "retry_after", None)
    if val is not None:
        try:
            return float(val)
        except (TypeError, ValueError):
            pass

    return None


async def _send_with_retry(
    coro_factory,
    label: str,
) -> tuple[bool, str | None]:
    """Run `coro_factory()` with retry on transient discord HTTPException.

    coro_factory is a callable that returns a fresh awaitable each call
    (we can't re-await a consumed coroutine).

    Returns (success, last_error_str).
    """
    last_error: str | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            await coro_factory()
            return True, None
        except discord.HTTPException as e:
            status = getattr(e, "status", "?")
            last_error = f"{type(e).__name__} status={status}: {e}"
            # 401/403/404 are permission/wrong-channel errors — no point retrying.
            if status in (401, 403, 404):
                log.error(f"{label}: non-retryable {last_error}")
                return False, last_error
            if attempt >= MAX_ATTEMPTS:
                log.error(f"{label}: gave up after {attempt} attempts ({last_error})")
                return False, last_error
            # 429 → honor Retry-After when present
            if status == 429:
                retry_after = _extract_retry_after(e)
                if retry_after is not None:
                    wait_s = min(retry_after, MAX_RETRY_AFTER_S)
                    log.warning(
                        f"{label}: 429 rate-limited, honoring Retry-After "
                        f"{retry_after:.1f}s (waiting {wait_s:.1f}s, attempt {attempt})"
                    )
                else:
                    # No header, default to exponential backoff
                    wait_s = min(2.0 * (2 ** (attempt - 1)), MAX_RETRY_AFTER_S)
                    log.warning(
                        f"{label}: 429 without Retry-After header, backing off "
                        f"{wait_s:.1f}s (attempt {attempt})"
                    )
            else:
                # Other 5xx / transient: exponential backoff (2s, 4s)
                wait_s = min(2.0 * (2 ** (attempt - 1)), MAX_RETRY_AFTER_S)
                log.warning(
                    f"{label}: transient {last_error}, backing off "
                    f"{wait_s:.1f}s (attempt {attempt})"
                )
            await asyncio.sleep(wait_s)
        except Exception as e:
            # Non-HTTP errors (e.g. asyncio.CancelledError, connection drop)
            # — surface as failure, don't retry blindly.
            last_error = f"{type(e).__name__}: {e}"
            log.error(f"{label}: non-HTTP exception, not retrying — {last_error}")
            return False, last_error
    return False, last_error


async def send_embeds_detailed(
    channel: discord.TextChannel,
    embeds: list[discord.Embed],
    delay: float = 0.5,
    leading_content: str | None = None,
) -> dict[str, Any]:
    """Structured variant of send_embeds with per-embed delivery accounting.

    Returns:
        {
          "success": bool,             # True iff all embeds + leading shipped
          "embeds_sent": int,          # number of embeds successfully posted
          "embeds_total": int,         # total intended
          "leading_sent": bool,        # True iff leading_content shipped (or absent)
          "last_error": str | None,    # most recent error message, for logging
        }
    """
    embeds_total = len(embeds)
    embeds_sent = 0
    leading_sent = True
    last_error: str | None = None

    if leading_content:
        ok, err = await _send_with_retry(
            lambda: channel.send(content=leading_content),
            label=f"leading-content channel={channel.id}",
        )
        if ok:
            await asyncio.sleep(delay)
        else:
            leading_sent = False
            last_error = err

    for i, embed in enumerate(embeds):
        ok, err = await _send_with_retry(
            lambda emb=embed, idx=i: channel.send(embed=emb),
            label=f"embed {i + 1}/{embeds_total} channel={channel.id}",
        )
        if ok:
            embeds_sent += 1
            if i < embeds_total - 1:
                await asyncio.sleep(delay)
        else:
            last_error = err
            # Stop the channel on first failed embed — Discord ordering
            # matters (header → sections → footer). A gap in the middle
            # is worse than half-delivery: the user sees a broken pulse.
            # The bridge's retry window will pick the whole pulse up
            # again on the next poll.
            log.error(
                f"send_embeds: embed {i + 1}/{embeds_total} failed on channel "
                f"{channel.id}, aborting remaining embeds for this channel"
            )
            break

    success = leading_sent and embeds_sent == embeds_total
    return {
        "success": success,
        "embeds_sent": embeds_sent,
        "embeds_total": embeds_total,
        "leading_sent": leading_sent,
        "last_error": last_error,
    }


async def send_embeds(
    channel: discord.TextChannel,
    embeds: list[discord.Embed],
    delay: float = 0.5,
    leading_content: str | None = None,
) -> bool:
    """Send a sequence of embeds to a Discord channel.

    Backwards-compatible bool-returning wrapper around send_embeds_detailed.
    Returns True iff every embed + leading_content shipped successfully.

    If `leading_content` is set, sends it as a plain message FIRST so
    Discord renders any markdown headers (`#`/`##`) at full size. Then
    sends the embeds in sequence. Used by the pulse delivery to put
    the H1 title + date at the top of the report at large rendered size,
    rather than constraining them to embed-title text.
    """
    result = await send_embeds_detailed(
        channel, embeds, delay=delay, leading_content=leading_content,
    )
    if not result["success"]:
        log.warning(
            f"send_embeds: partial/failed delivery — "
            f"embeds_sent={result['embeds_sent']}/{result['embeds_total']} "
            f"leading={result['leading_sent']} last_error={result['last_error']}"
        )
    return result["success"]


async def send_plain_messages(
    channel: discord.TextChannel,
    messages: list[str],
    delay: float = 0.5,
) -> bool:
    """Send a sequence of plain text messages."""
    success = True
    for i, msg in enumerate(messages):
        ok, err = await _send_with_retry(
            lambda m=msg: channel.send(m),
            label=f"plain msg {i + 1}/{len(messages)} channel={channel.id}",
        )
        if not ok:
            success = False
        if ok and i < len(messages) - 1:
            await asyncio.sleep(delay)
    return success


async def send_file_to_channels(
    bot,
    file_bytes: bytes,
    filename: str,
    caption: str = "",
) -> int:
    """Post one file attachment (fresh discord.File per channel — a
    consumed BytesIO can't be re-sent) to every channel in
    DISCORD_CHANNEL_ID. Returns the number of channels that succeeded.
    Added 2026-08-20 for the daily calendar graphic."""
    import io
    from config import settings

    raw = (settings.discord_channel_id or "").strip()
    channel_ids = [c.strip() for c in raw.split(",") if c.strip()]
    sent = 0
    for cid in channel_ids:
        try:
            channel = bot.get_channel(int(cid))
            if channel is None:
                channel = await bot.fetch_channel(int(cid))
        except Exception as e:
            log.error(f"send_file_to_channels: resolve {cid} failed: {e}")
            continue
        ok, _err = await _send_with_retry(
            lambda ch=channel: ch.send(
                content=caption or None,
                file=discord.File(io.BytesIO(file_bytes), filename=filename),
            ),
            label=f"file {filename} channel={cid}",
        )
        sent += 1 if ok else 0
    return sent
