"""One-off: print the Discord channel ID for a given channel name.
Used to look up the ID for 💬-stonks-yapping-💬 so we can update
DISCORD_INGEST_FEED_CHANNEL_ID on Railway."""
import asyncio
import os
import sys

import discord
from config import settings


TARGET_NAME = "💬-stonks-yapping-💬"


async def main():
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name == TARGET_NAME:
                        print(f"FOUND: {ch.name} → id={ch.id} (guild={guild.name})")
                        return
            print(f"NOT FOUND: no text channel named {TARGET_NAME!r}")
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)


asyncio.run(main())
