"""One-off: scan #💅🏾-kyle-alerts-💅🏾 and report on BK's posting
style so we can design a tracker for his trades.

Reports:
- Message count over last 14 days
- Attachment vs no-attachment ratio
- Embed types (image attachments, link previews, etc.)
- Caption patterns (length distribution, action verbs, contract refs)
- Sample of recent messages with truncated captions + attachment info
"""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import discord

from config import settings


TARGET_NAME = "💅🏾-kyle-alerts-💅🏾"
DAYS = 14


async def main():
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            target: discord.TextChannel | None = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name == TARGET_NAME:
                        target = ch
                        break
                if target:
                    break

            if target is None:
                print(f"NOT FOUND: {TARGET_NAME}")
                return

            print(f"Channel: #{target.name} (id={target.id})")
            print(f"Scanning last {DAYS} days...")
            print()

            cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)

            total = 0
            by_author: Counter = Counter()
            with_attach = 0
            with_embed_image = 0
            text_only = 0
            caption_lengths = []
            samples = []  # recent (newest-first) samples — author, ts, content snippet, attach count, embed info

            async for msg in target.history(
                limit=None, after=cutoff, oldest_first=False
            ):
                if msg.author.bot:
                    continue
                total += 1
                by_author[msg.author.name] += 1

                content = (msg.content or "").strip()
                caption_lengths.append(len(content))

                attach_count = 0
                attach_types = []
                for a in msg.attachments:
                    attach_count += 1
                    attach_types.append((a.content_type or "?").split(";")[0])

                embed_image_count = 0
                embed_types = []
                for e in msg.embeds:
                    embed_types.append(e.type or "?")
                    if (getattr(e, "image", None) and e.image.url) or (
                        getattr(e, "thumbnail", None) and e.thumbnail.url
                    ):
                        embed_image_count += 1

                if attach_count:
                    with_attach += 1
                elif embed_image_count:
                    with_embed_image += 1
                else:
                    text_only += 1

                if len(samples) < 30:
                    snippet = content[:140].replace("\n", " ")
                    samples.append({
                        "author": msg.author.name,
                        "ts": msg.created_at.isoformat()[:16],
                        "content": snippet,
                        "n_attach": attach_count,
                        "attach_types": attach_types,
                        "n_embed_img": embed_image_count,
                        "embed_types": embed_types,
                    })

            # ---------- REPORT ----------
            print(f"Total messages (non-bot): {total}")
            print()
            print("By author:")
            for name, n in by_author.most_common():
                print(f"  {name:<30} {n}")
            print()
            print(f"Attachment breakdown:")
            print(f"  with direct attachment:    {with_attach}")
            print(f"  with embed image only:     {with_embed_image}")
            print(f"  text-only:                 {text_only}")
            print()
            if caption_lengths:
                non_empty = [n for n in caption_lengths if n > 0]
                if non_empty:
                    avg = sum(non_empty) / len(non_empty)
                    print(f"Caption length (non-empty msgs):")
                    print(f"  count: {len(non_empty)} / {len(caption_lengths)}")
                    print(f"  avg: {avg:.0f} chars   min: {min(non_empty)}   max: {max(non_empty)}")
                else:
                    print("No non-empty captions.")
            print()
            print("Recent samples (newest first, up to 30):")
            for s in samples:
                line = (
                    f"  [{s['ts']}] {s['author']:<25} "
                    f"attach={s['n_attach']} ({','.join(s['attach_types']) or '-'}) "
                    f"embed_img={s['n_embed_img']} ({','.join(s['embed_types']) or '-'})"
                )
                if s["content"]:
                    line += f" | {s['content']!r}"
                print(line)
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)


asyncio.run(main())
