"""Discord message delivery with chunking and retry."""

import asyncio
import logging

import discord

log = logging.getLogger(__name__)


async def send_embeds(
    channel: discord.TextChannel,
    embeds: list[discord.Embed],
    delay: float = 0.5,
    leading_content: str | None = None,
) -> bool:
    """Send a sequence of embeds to a Discord channel.

    If `leading_content` is set, sends it as a plain message FIRST so
    Discord renders any markdown headers (`#`/`##`) at full size. Then
    sends the embeds in sequence. Used by the pulse delivery to put
    the H1 title + date at the top of the report at large rendered size,
    rather than constraining them to embed-title text.

    Returns True if all sends were successful.
    """
    success = True

    if leading_content:
        try:
            await channel.send(content=leading_content)
            await asyncio.sleep(delay)
        except discord.HTTPException as e:
            log.error(f"Failed to send leading content: {e}")
            try:
                await asyncio.sleep(2)
                await channel.send(content=leading_content)
                await asyncio.sleep(delay)
            except discord.HTTPException as e2:
                log.error(f"Retry failed for leading content: {e2}")
                success = False

    for i, embed in enumerate(embeds):
        try:
            await channel.send(embed=embed)
            if i < len(embeds) - 1:
                await asyncio.sleep(delay)  # Respect rate limits
        except discord.HTTPException as e:
            log.error(f"Failed to send embed {i + 1}/{len(embeds)}: {e}")
            # Retry once
            try:
                await asyncio.sleep(2)
                await channel.send(embed=embed)
            except discord.HTTPException as e2:
                log.error(f"Retry failed for embed {i + 1}: {e2}")
                success = False

    return success


async def send_plain_messages(
    channel: discord.TextChannel,
    messages: list[str],
    delay: float = 0.5,
) -> bool:
    """Send a sequence of plain text messages."""
    success = True
    for i, msg in enumerate(messages):
        try:
            await channel.send(msg)
            if i < len(messages) - 1:
                await asyncio.sleep(delay)
        except discord.HTTPException as e:
            log.error(f"Failed to send message {i + 1}/{len(messages)}: {e}")
            success = False

    return success
