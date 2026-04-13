"""Discord message delivery with chunking and retry."""

import asyncio
import logging

import discord

log = logging.getLogger(__name__)


async def send_embeds(
    channel: discord.TextChannel,
    embeds: list[discord.Embed],
    delay: float = 0.5,
) -> bool:
    """Send a sequence of embeds to a Discord channel.

    Returns True if all embeds were sent successfully.
    """
    success = True
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
