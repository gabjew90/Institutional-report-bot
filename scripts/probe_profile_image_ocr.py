"""Probe: does enabling image OCR in the user-profile backfill produce
materially different (better) profiles, and at what cost?

For the top N image-posting users in the yapping channels over the last
PROFILE_WINDOW_DAYS, this script generates TWO profiles per user:

1. **Text-only** (current production behavior): images become `[image]`
   placeholder markers.
2. **Vision-enabled**: real image bytes get downloaded and attached as
   multipart Gemini input alongside the message text. The model sees
   what the user actually posted — charts, memes, screenshots, tweets.

Both profiles run against the same prompt and the same sample of messages,
so the only delta is whether the images are visible. Prints both profiles
back-to-back per user, plus a token / dollar cost breakdown.

Doesn't write to the DB. Pure read-only probe. Safe to run locally:
    py scripts/probe_profile_image_ocr.py --top-n 3 --days 30
"""
import argparse
import asyncio
import io
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from config import settings  # noqa: E402
from scripts.backfill_user_profiles import (  # noqa: E402
    PROFILE_PROMPT,
    _extract_embed_texts,
    _format_messages_block,
    DEFAULT_PROFILE_CHANNELS,
)


# Hard cap on bytes per image — large screenshots blow up token cost.
# 4MB is generous; most discord images come in under 1MB.
MAX_IMAGE_BYTES = 4_000_000

# Hard cap on TOTAL images per user — even with vision, 200 images at
# ~1000 tokens each = 200K tokens. Cap keeps the per-user cost predictable.
MAX_IMAGES_PER_USER = 40


async def _download_image(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            if r.status != 200:
                return None
            data = await r.content.read(MAX_IMAGE_BYTES + 1)
            return data if len(data) <= MAX_IMAGE_BYTES else None
    except Exception:
        return None


async def _generate_profile_text_only(
    client: genai.Client, display_name: str, username: str, messages: list[dict]
) -> tuple[str | None, dict]:
    """Generate profile with `[image]` markers — current prod behavior."""
    msgs_block = _format_messages_block(messages)
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        msg_count=len(messages),
        messages_block=msgs_block,
    )
    t0 = time.time()
    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=1500),
    )
    elapsed = time.time() - t0
    text = (response.text or "").strip()
    usage = getattr(response, "usage_metadata", None)
    meta = {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "elapsed_s": round(elapsed, 1),
    }
    return text or None, meta


async def _generate_profile_with_vision(
    client: genai.Client,
    http: aiohttp.ClientSession,
    display_name: str,
    username: str,
    messages: list[dict],
    image_attachments: list[dict],
) -> tuple[str | None, dict]:
    """Generate profile with REAL images attached as multipart input.

    image_attachments: list of {url, content_type, ts} from messages.
    """
    msgs_block = _format_messages_block(messages)
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        msg_count=len(messages),
        messages_block=msgs_block,
    )

    # Cap + download images
    attachments = image_attachments[:MAX_IMAGES_PER_USER]
    downloads = await asyncio.gather(
        *[_download_image(http, a["url"]) for a in attachments],
        return_exceptions=False,
    )

    parts: list = []
    img_count = 0
    for att, blob in zip(attachments, downloads):
        if not blob:
            continue
        mime = (att.get("content_type") or "image/png").split(";")[0]
        parts.append(types.Part.from_bytes(data=blob, mime_type=mime))
        img_count += 1
    parts.append(types.Part.from_text(text=prompt))

    t0 = time.time()
    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=parts,
        config=types.GenerateContentConfig(temperature=0.3, max_output_tokens=1500),
    )
    elapsed = time.time() - t0
    text = (response.text or "").strip()
    usage = getattr(response, "usage_metadata", None)
    meta = {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "images_attached": img_count,
        "elapsed_s": round(elapsed, 1),
    }
    return text or None, meta


def _est_cost(input_tokens: int | None, output_tokens: int | None) -> float:
    """Rough Flash Lite pricing: $0.10/M input, $0.40/M output."""
    if input_tokens is None or output_tokens is None:
        return 0.0
    return (input_tokens / 1_000_000) * 0.10 + (output_tokens / 1_000_000) * 0.40


async def run(days: int, channels: list[str], top_n: int) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    discord_client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)
    summary_lines: list[str] = []

    @discord_client.event
    async def on_ready():
        async with aiohttp.ClientSession() as http:
            try:
                targets: list[discord.TextChannel] = []
                for guild in discord_client.guilds:
                    for ch in guild.text_channels:
                        if ch.name in channels:
                            targets.append(ch)
                if not targets:
                    print(f"ERROR: none of {channels} found", file=sys.stderr,
                          flush=True)
                    return
                print(f"Channels: {[ch.name for ch in targets]}", flush=True)
                print(f"Scanning last {days} days for image-heavy users...",
                      flush=True)

                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                skip_username = (
                    settings.analyst_primary_author or ""
                ).lower().strip()

                by_user: dict[int, list[dict]] = defaultdict(list)
                user_meta: dict[int, dict] = {}
                images_by_user: dict[int, list[dict]] = defaultdict(list)

                for ch in targets:
                    print(f"\nScanning #{ch.name}...", flush=True)
                    count = 0
                    async for msg in ch.history(limit=None, after=cutoff,
                                                oldest_first=True):
                        if msg.author.bot:
                            continue
                        uname = (msg.author.name or "").lower()
                        if skip_username and uname == skip_username:
                            continue
                        uid = msg.author.id
                        if uid not in user_meta:
                            user_meta[uid] = {
                                "username": msg.author.name,
                                "display_name": (
                                    getattr(msg.author, "display_name", None)
                                    or msg.author.name
                                ),
                            }
                        image_count = 0
                        for a in msg.attachments:
                            ct = (a.content_type or "").lower()
                            if ct.startswith("image/"):
                                image_count += 1
                                images_by_user[uid].append({
                                    "url": a.url,
                                    "content_type": a.content_type,
                                    "ts": msg.created_at.isoformat(),
                                })
                        embed_texts = _extract_embed_texts(msg)
                        by_user[uid].append({
                            "timestamp": msg.created_at.isoformat(),
                            "content": (msg.content or "").strip(),
                            "image_count": image_count,
                            "embed_texts": embed_texts,
                        })
                        count += 1
                    print(f"  {count} msgs, {len(by_user)} unique authors so far",
                          flush=True)

                # Rank users by image count, pick top N (only ones meeting
                # the min-message threshold so the profile is meaningful)
                min_msgs = settings.profile_min_messages
                ranked = sorted(
                    [
                        (uid, len(images_by_user.get(uid, [])))
                        for uid in by_user
                        if len(by_user[uid]) >= min_msgs
                    ],
                    key=lambda t: -t[1],
                )[:top_n]
                if not ranked:
                    print("\nNo eligible image-posters found "
                          f"(need >= {min_msgs} messages).", flush=True)
                    return
                print(
                    f"\nTop {len(ranked)} image-posters "
                    f"(>= {min_msgs} msgs):",
                    flush=True,
                )
                for uid, n in ranked:
                    meta = user_meta[uid]
                    print(
                        f"  {meta['display_name']:<25} "
                        f"({meta['username']}) — "
                        f"{len(by_user[uid])} msgs, {n} images",
                        flush=True,
                    )

                # Run text-only + vision profile for each
                sample_size = settings.profile_sample_size
                total_cost_text = 0.0
                total_cost_vision = 0.0
                for uid, _img_count in ranked:
                    meta = user_meta[uid]
                    sample = by_user[uid][-sample_size:]
                    images = images_by_user.get(uid, [])

                    print(f"\n{'=' * 78}", flush=True)
                    print(
                        f"{meta['display_name']} ({meta['username']}) — "
                        f"{len(by_user[uid])} msgs, {len(images)} images "
                        f"(using last {len(sample)} msgs, "
                        f"capped {min(len(images), MAX_IMAGES_PER_USER)} imgs)",
                        flush=True,
                    )
                    print('=' * 78, flush=True)

                    # Run both in parallel
                    text_only, vision = await asyncio.gather(
                        _generate_profile_text_only(
                            gemini_client,
                            meta["display_name"],
                            meta["username"],
                            sample,
                        ),
                        _generate_profile_with_vision(
                            gemini_client,
                            http,
                            meta["display_name"],
                            meta["username"],
                            sample,
                            images,
                        ),
                        return_exceptions=False,
                    )

                    text_profile, text_meta = text_only
                    vision_profile, vision_meta = vision
                    text_cost = _est_cost(
                        text_meta.get("input_tokens"),
                        text_meta.get("output_tokens"),
                    )
                    vision_cost = _est_cost(
                        vision_meta.get("input_tokens"),
                        vision_meta.get("output_tokens"),
                    )
                    total_cost_text += text_cost
                    total_cost_vision += vision_cost

                    print("\n--- TEXT-ONLY PROFILE ---", flush=True)
                    print(text_profile or "[empty]", flush=True)
                    print(
                        f"\n  tokens: {text_meta.get('input_tokens')} in / "
                        f"{text_meta.get('output_tokens')} out "
                        f"| elapsed: {text_meta.get('elapsed_s')}s "
                        f"| est cost: ${text_cost:.4f}",
                        flush=True,
                    )

                    print("\n--- VISION-ENABLED PROFILE ---", flush=True)
                    print(vision_profile or "[empty]", flush=True)
                    print(
                        f"\n  tokens: {vision_meta.get('input_tokens')} in / "
                        f"{vision_meta.get('output_tokens')} out "
                        f"| images: {vision_meta.get('images_attached')} "
                        f"| elapsed: {vision_meta.get('elapsed_s')}s "
                        f"| est cost: ${vision_cost:.4f}",
                        flush=True,
                    )

                    summary_lines.append(
                        f"## {meta['display_name']} ({meta['username']})\n\n"
                        f"### Text-only\n\n{text_profile}\n\n"
                        f"### Vision-enabled\n\n{vision_profile}\n\n"
                        f"---\n\n"
                    )

                # Totals
                print(f"\n{'=' * 78}", flush=True)
                print("COST SUMMARY", flush=True)
                print('=' * 78, flush=True)
                print(f"  Text-only:      ${total_cost_text:.4f} for "
                      f"{len(ranked)} users", flush=True)
                print(f"  Vision-enabled: ${total_cost_vision:.4f} for "
                      f"{len(ranked)} users", flush=True)
                print(
                    f"  Cost multiplier: "
                    f"{(total_cost_vision / total_cost_text):.1f}x"
                    if total_cost_text > 0 else "  Cost multiplier: n/a",
                    flush=True,
                )
                print(
                    f"\nExtrapolated daily (37 profiled users):",
                    flush=True,
                )
                per_user_text = total_cost_text / len(ranked)
                per_user_vision = total_cost_vision / len(ranked)
                print(
                    f"  Text-only:      "
                    f"${per_user_text * 37:.2f}/day ≈ "
                    f"${per_user_text * 37 * 30:.2f}/month",
                    flush=True,
                )
                print(
                    f"  Vision-enabled: "
                    f"${per_user_vision * 37:.2f}/day ≈ "
                    f"${per_user_vision * 37 * 30:.2f}/month",
                    flush=True,
                )

            finally:
                await discord_client.close()

    await discord_client.start(settings.discord_bot_token)

    out_name = (
        f"probe_profile_image_ocr_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    out_path = _REPO_ROOT / out_name
    out_path.write_text("".join(summary_lines), encoding="utf-8")
    print(f"\nSide-by-side summary written to {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Days of history to scan (default 30)")
    parser.add_argument(
        "--channels", type=str, default=",".join(DEFAULT_PROFILE_CHANNELS),
        help="Comma-separated channel names (default: yapping channels)"
    )
    parser.add_argument(
        "--top-n", type=int, default=3,
        help="Number of top image-posters to profile (default 3)"
    )
    args = parser.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels, args.top_n))


if __name__ == "__main__":
    main()
