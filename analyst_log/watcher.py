"""Discord event handler that turns analyst-channel posts into trade-log
rows. Called from `discord_bot.bot.on_message` when the message channel
matches `settings.analyst_channel_name`.

Flow per message:
  1. Skip if no image attachments.
  2. For each image attachment, dedup by (message_id, attachment_id).
  3. Download the image, OCR via `analyst_log.ocr.extract_trade_from_image`.
  4. Write a row to `analyst_trades` (whether trade or not — non-trades
     get is_trade=0 so we don't re-OCR them on bot restart).
  5. For actual trades, post a one-line summary to the configured
     announce channel (`settings.analyst_test_announce_channel`).
"""

import logging
from typing import Any

import discord

import db
from analyst_log.ocr import extract_trade_from_caption, extract_trade_from_image
from config import settings

log = logging.getLogger(__name__)


async def _fetch_reply_parent_caption(
    message: discord.Message,
) -> str | None:
    """If `message` is a Discord reply, fetch the parent message and
    return its content (caption). Returns None if not a reply, if
    fetch fails, or the parent has no content.

    Used to enrich caption-only and image extractions with chain
    context — e.g. a reply 'closed' on an earlier 'BTO MSFT 430C 5/20
    @3.65' post resolves to a close of that same contract.
    """
    ref = getattr(message, "reference", None)
    if not ref:
        return None
    parent_id = getattr(ref, "message_id", None)
    if not parent_id:
        return None
    # Prefer the cached resolved message if discord.py already has it
    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return (resolved.content or "").strip() or None
    # Otherwise fetch from the channel
    try:
        parent = await message.channel.fetch_message(parent_id)
    except Exception as e:
        log.debug(f"Analyst log: reply-parent fetch failed for {parent_id}: {e}")
        return None
    return (parent.content or "").strip() or None


async def watch_message(
    bot: discord.Client,
    message: discord.Message,
    caller: dict | None = None,
) -> None:
    """Process an analyst-channel message. Side-effect-only — writes to DB
    and posts to the announce channel. All failures are logged and
    swallowed so a single bad image never blocks subsequent processing.

    `caller` is the matched caller dict from settings.resolve_analyst_callers():
    {name, display, username, channel, enabled}. When provided, the
    watcher uses caller['username'] for the author filter and writes
    caller['name'] as the canonical `caller` field on each row.

    Legacy callers passing only (bot, message) fall back to the global
    `analyst_primary_author` setting for filtering, and the stored
    `caller` field is None — backwards-compatible with the pre-registry
    deployment but loses hard-separation in /ask context.
    """
    # Resolve the username to filter by + the canonical caller name to
    # store. Registry-driven (preferred) or legacy fallback.
    if caller:
        expected_username = caller.get("username", "").strip().lower()
        canonical_caller = caller.get("name", "").strip().lower() or None
    else:
        expected_username = (settings.analyst_primary_author or "").strip().lower()
        canonical_caller = None

    if expected_username:
        author_username = (message.author.name or "").lower()
        if author_username != expected_username:
            log.debug(
                f"Analyst log: skipping non-matching author '{author_username}' "
                f"in {message.channel.name} (expected='{expected_username}', "
                f"caller={canonical_caller})"
            )
            return

    caption = (message.content or "").strip()
    posted_at = message.created_at.isoformat()
    author_name = (
        getattr(message.author, "display_name", None) or message.author.name
    )

    # Reply-chain context: if this message is a Discord reply, fetch
    # the parent's caption so terse follow-ups like "closed" or "sold
    # @19" can be resolved against the position the parent established.
    parent_caption = await _fetch_reply_parent_caption(message)

    # Caption-only path: a message with NO image attachments but a
    # non-empty caption goes through text-only extraction. The caption
    # must contain ticker + strike (expiry defaults to today/0DTE) —
    # otherwise the extraction path tags is_trade=false and we skip
    # recording. Reply-chain context (parent_caption) is passed through
    # so 'closed' style follow-ups resolve correctly.
    if not message.attachments:
        if not caption:
            return
        # Dedup: use a synthetic attachment_id=0 so the UNIQUE(message_id,
        # attachment_id) constraint still works for caption-only rows.
        synthetic_att_id = 0
        if db.analyst_trade_exists(message.id, synthetic_att_id):
            return
        extracted = await extract_trade_from_caption(
            caption, parent_caption=parent_caption
        )
        if extracted is None:
            log.warning(
                f"Analyst log: caption extraction returned None — "
                f"msg={message.id} caption={caption[:120]!r}"
            )
            return
        is_trade = bool(extracted.get("is_trade_screenshot"))
        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=synthetic_att_id,
                author=author_name,
                posted_at=posted_at,
                image_url=None,
                caption=caption,
                is_trade=is_trade,
                gemini_json=extracted,
                ticker=extracted.get("ticker") if is_trade else None,
                contract_type=extracted.get("contract_type") if is_trade else None,
                strike=extracted.get("strike") if is_trade else None,
                expiry=extracted.get("expiry") if is_trade else None,
                action=extracted.get("action") if is_trade else None,
                gain_pct=extracted.get("gain_pct") if is_trade else None,
                price=extracted.get("price") if is_trade else None,
                caller=canonical_caller,
            )
        except Exception as e:
            log.error(f"Analyst log: caption-only DB insert failed: {e}", exc_info=True)
            return
        if is_trade:
            await _announce_to_channel(bot, message, extracted, author_name)
        return

    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if not ct.startswith("image/"):
            continue

        if db.analyst_trade_exists(message.id, att.id):
            log.debug(
                f"Analyst log: skipping already-logged image — "
                f"msg={message.id} att={att.id}"
            )
            continue

        try:
            img_bytes = await att.read()
        except Exception as e:
            log.error(f"Analyst log: failed to read attachment {att.url}: {e}")
            continue

        extracted = await extract_trade_from_image(
            img_bytes, ct, caption, parent_caption=parent_caption
        )
        if extracted is None:
            # OCR failed entirely — don't insert a row, so we'll retry on
            # the next bot restart. (Rare; usually means Gemini call errored.)
            log.warning(
                f"Analyst log: extraction returned None for {att.url} — "
                f"will retry on next restart"
            )
            continue

        is_trade = bool(extracted.get("is_trade_screenshot"))

        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=att.id,
                author=author_name,
                posted_at=posted_at,
                image_url=att.url,
                caption=caption,
                is_trade=is_trade,
                gemini_json=extracted,
                ticker=extracted.get("ticker") if is_trade else None,
                contract_type=extracted.get("contract_type") if is_trade else None,
                strike=extracted.get("strike") if is_trade else None,
                expiry=extracted.get("expiry") if is_trade else None,
                action=extracted.get("action") if is_trade else None,
                gain_pct=extracted.get("gain_pct") if is_trade else None,
                price=extracted.get("price") if is_trade else None,
                caller=canonical_caller,
            )
        except Exception as e:
            log.error(f"Analyst log: DB insert failed: {e}", exc_info=True)
            continue

        if is_trade:
            await _announce_to_channel(bot, message, extracted, author_name)


async def _announce_to_channel(
    bot: discord.Client,
    source_msg: discord.Message,
    extracted: dict[str, Any],
    author_name: str,
) -> None:
    """Post a one-line summary of the logged trade to the configured
    announce channel as an embed. The source channel name is the
    clickable link back to the original alert.
    Skips silently if the channel can't be found.
    """
    chan_name = (settings.analyst_test_announce_channel or "").strip()
    if not chan_name:
        return

    target: discord.TextChannel | None = None
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name.lower() == chan_name.lower():
                target = ch
                break
        if target:
            break

    if target is None:
        log.warning(
            f"Analyst log: announce channel '{chan_name}' not found in any guild"
        )
        return

    description = _format_announce_line(extracted, source_msg, author_name)
    embed = discord.Embed(description=description)
    try:
        await target.send(embed=embed)
    except Exception as e:
        log.error(f"Analyst log: announce failed in #{chan_name}: {e}")


def _format_announce_line(
    extracted: dict[str, Any],
    source_msg: discord.Message,
    author_name: str,
) -> str:
    """Render the announce line for the embed description. Format:

    📝 Logged: [**#🥷🏽-abe-alerts-🥷🏽**](jump_url) CLOSE **NOW 95C 5/29** (+79.6%) — "I'm out!"

    The bolded channel name links to the source alert message. Embed
    rendering means markdown links work and the URL doesn't show as
    visible text. `author_name` is kept in the signature for backwards
    compatibility but is no longer displayed in the output (the source
    channel is the clearer attribution).
    """
    ticker = extracted.get("ticker") or "?"
    strike = extracted.get("strike")
    contract_type = (extracted.get("contract_type") or "").lower()
    expiry = extracted.get("expiry") or ""
    action = (extracted.get("action") or "?").upper()
    gain_pct = extracted.get("gain_pct")
    price = extracted.get("price")
    screenshot_type = extracted.get("screenshot_type") or ""
    caption = (source_msg.content or "").strip()

    type_suffix = {"call": "C", "put": "P"}.get(contract_type, "")
    strike_str = (
        f"{int(strike) if strike == int(strike) else strike}" if strike else "?"
    )
    expiry_short = expiry[5:] if len(expiry) >= 10 else expiry  # MM-DD slice
    contract_str = f"{ticker} {strike_str}{type_suffix} {expiry_short}".strip()

    # Channel name with markdown link to the source message. Works inside
    # an embed description.
    channel_name = source_msg.channel.name if source_msg.channel else "source"
    jump_url = getattr(source_msg, "jump_url", "")
    if jump_url:
        channel_link = f"[**#{channel_name}**]({jump_url})"
    else:
        channel_link = f"**#{channel_name}**"

    line = f"📝 Logged: {channel_link} {action} **{contract_str}**"
    # Display rule: entry price only shows on opens (OPEN/ADD); gain%
    # only shows on exits (CLOSE/TRIM). These are mutually exclusive at
    # display time — opens are valued by their fill price, closes by
    # their realized %. Both fields are stored regardless if extracted.
    # 0-values are treated as missing (model sentinel for "couldn't
    # extract", not an actual data point).
    is_open_event = action in ("OPEN", "ADD")
    is_exit_event = action in ("CLOSE", "TRIM")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        gain_f = float(gain_pct) if gain_pct is not None else None
    except (TypeError, ValueError):
        gain_f = None
    if is_open_event and price_f and price_f != 0:
        line += f" @{price_f:.2f}"
    if is_exit_event and gain_f is not None and gain_f != 0:
        line += f" ({gain_f:+.1f}%)"
    if screenshot_type == "stats_screen" and action == "VIEWING":
        line += " _(stats screen)_"
    if caption:
        # Trim long captions, escape Discord markdown noise
        cap_safe = caption.replace("`", "'").replace("\n", " ")[:80]
        line += f' — "{cap_safe}"'
    # (Previously appended a trailing <jump_url> here; replaced by the
    # markdown link on the channel-name segment above.)
    return line[:1900]  # Discord 2000-char hard limit, leave buffer
