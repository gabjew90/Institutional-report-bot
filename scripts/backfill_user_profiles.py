"""One-shot backfill of user personality profiles for active group members.

Scans the configured "yapping" channels for the last N days, groups messages
by author, and generates a 100-150 word LLM-summarized profile per user.
Writes to the user_profiles table for use by /ask context fetching.

Design choices baked in (per recent design discussion):
  - Sample most-recent 100 messages per user (not all of them — diminishing
    returns past that, and bounded token cost regardless of message volume)
  - Include text content + embed text (titles, descriptions) — embeds are
    cheap to read and carry signal about what users share
  - Skip image OCR — too expensive ($0.50+ for backfill) and noisy for
    personality profiling. Image attachments become "[image]" markers so
    the model knows the user posts charts/memes without burning tokens
  - Skip lurkers — minimum 20 messages over window. Below that the
    profile is generic guesswork
  - Skip the analyst (settings.analyst_primary_author) — he has a hand-
    written KEY USERS block in the system prompt
  - Parallel Gemini calls in groups of 5 to respect free-tier rate limits

Usage:
    railway ssh "/opt/venv/bin/python scripts/backfill_user_profiles.py --days 30"

Output: per-user log line and a summary saved to
backfill_user_profiles_<timestamp>.md in /app/ for review.
"""

import argparse
import asyncio
import io
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

import db  # noqa: E402
from config import settings  # noqa: E402

# Default channels for personality profiling. The yapping channels are
# where personality shows; alerts channels are alerts-only and won't
# yield useful profiles.
DEFAULT_PROFILE_CHANNELS = [
    "💬-stonks-yapping-💬",
    "₿-crypto-yapping-₿",
]

# Default fallbacks — actual values pulled from settings
MIN_MESSAGES_FOR_PROFILE_FALLBACK = 100
MESSAGES_PER_PROFILE_SAMPLE_FALLBACK = 500
GEMINI_CONCURRENCY = 5  # parallel calls per batch


PROFILE_PROMPT = """\
Profile this Discord user from their recent trading-chat messages. They're a
member of a private options/crypto trading group; the channel is high-volume
trader chat.

Output: 100-150 words, third person, dry tone — natural like you'd describe
a regular in a trading room to someone who hasn't met them. Cover:

- **Trading style / bias**: Mostly long? Short? Options? Spot? Crypto? Swing?
  Scalp? 0DTE? What asset class do they live in?
- **Favorite tickers / sectors / themes**: What do they post about most?
- **Recurring takes / hot buttons**: Specific calls or beliefs they keep
  returning to (Fed pivot, perma-bear on housing, always selling puts on
  $NVDA, etc.). Running jokes the room knows about them.
- **Personality / vibe**: Chaotic? Methodical? Perma-bull? Contrarian?
  Sidelines/cash-king? Always coping? Always green-pilled? Loud or
  understated?

Skip anything they didn't actually demonstrate in the messages. If they
posted mostly reactions / one-liners, say so honestly ("mostly drive-by
reactions, no clear directional bias from what's visible").

Output PROSE only — no bullets, no headers, no "Profile:" prefix. Just the
description as one or two paragraphs.

MESSAGES (oldest first, most recent last):
{messages_block}\
"""


def _format_messages_block(messages: list[dict]) -> str:
    """Render the per-user message list for the Gemini prompt.

    Each entry: timestamp + content + embed text + image markers.
    No filtering per user direction ("no filters") — short reactions
    and tickers go through too. Cap individual message length at 400
    chars to bound prompt size on the rare long rant.
    """
    out = []
    for m in messages:
        ts = m["timestamp"][:16].replace("T", " ")
        parts = []
        if m["content"]:
            parts.append(m["content"][:400])
        for embed_text in m.get("embed_texts", []):
            parts.append(f"[embed: {embed_text[:300]}]")
        for n in range(m.get("image_count", 0)):
            parts.append("[image]")
        if parts:
            out.append(f"{ts}: {' | '.join(parts)}")
        else:
            out.append(f"{ts}: [empty / sticker]")
    return "\n".join(out)


def _extract_embed_texts(message: discord.Message) -> list[str]:
    """Pull title/description text from each embed on a message."""
    out = []
    for e in message.embeds:
        bits = []
        if e.title:
            bits.append(e.title)
        if e.description:
            bits.append(e.description)
        for f in (e.fields or []):
            if f.name and f.value:
                bits.append(f"{f.name}: {f.value}")
        if bits:
            out.append(" — ".join(bits))
    return out


async def _generate_profile(
    gemini_client: genai.Client,
    display_name: str,
    messages: list[dict],
) -> str | None:
    """Run Gemini to produce one user's profile. Returns None on failure."""
    min_msgs = getattr(settings, "profile_min_messages", None) or MIN_MESSAGES_FOR_PROFILE_FALLBACK
    if len(messages) < min_msgs:
        return None
    # Sample most-recent N (Discord returns history oldest-first when we
    # ask via after=cutoff, oldest_first=True; we kept all of it in order).
    sample_size = (
        getattr(settings, "profile_sample_size", None)
        or MESSAGES_PER_PROFILE_SAMPLE_FALLBACK
    )
    sample = messages[-sample_size:]
    msgs_block = _format_messages_block(sample)
    prompt = PROFILE_PROMPT.format(messages_block=msgs_block)
    try:
        response = await gemini_client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=400,
            ),
        )
        text = (response.text or "").strip()
        return text if text else None
    except Exception as e:
        print(f"  ERROR profiling {display_name}: {e}", flush=True)
        return None


async def run(days: int, channels: list[str]) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)

    summary_lines: list[str] = []

    @client.event
    async def on_ready():
        try:
            # Find channels
            targets: list[discord.TextChannel] = []
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name in channels:
                        targets.append(ch)
            if not targets:
                print(f"ERROR: none of {channels} found", file=sys.stderr,
                      flush=True)
                return
            print(f"Channels: {[ch.name for ch in targets]}", flush=True)
            print(f"Backfilling last {days} days...", flush=True)

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Skip the analyst — handled separately via KEY USERS in prompt.
            skip_username = (settings.analyst_primary_author or "").lower().strip()

            # Per-user accumulator: user_id -> list of message dicts
            by_user: dict[int, list[dict]] = defaultdict(list)
            user_meta: dict[int, dict] = {}  # user_id -> {username, display_name}

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
                    image_count = sum(
                        1 for a in msg.attachments
                        if (a.content_type or "").lower().startswith("image/")
                    )
                    embed_texts = _extract_embed_texts(msg)
                    by_user[uid].append({
                        "timestamp": msg.created_at.isoformat(),
                        "content": (msg.content or "").strip(),
                        "image_count": image_count,
                        "embed_texts": embed_texts,
                    })
                    count += 1
                print(f"  {count} messages, {len(by_user)} unique authors so far",
                      flush=True)

            # Filter to users meeting threshold
            min_msgs = (
                getattr(settings, "profile_min_messages", None)
                or MIN_MESSAGES_FOR_PROFILE_FALLBACK
            )
            eligible = [
                (uid, msgs) for uid, msgs in by_user.items()
                if len(msgs) >= min_msgs
            ]
            eligible.sort(key=lambda t: -len(t[1]))  # most active first
            skipped_lurkers = len(by_user) - len(eligible)
            # Optional hard cap (default 0 = no cap; rely on threshold)
            max_n = settings.max_user_profiles
            if max_n > 0 and len(eligible) > max_n:
                trimmed = len(eligible) - max_n
                eligible = eligible[:max_n]
                print(f"\nCapped to top {max_n} users by message count "
                      f"(trimmed {trimmed} below the cap)", flush=True)
            print(f"\n{len(eligible)} users eligible (>= {min_msgs} msgs), "
                  f"{skipped_lurkers} skipped as lurkers", flush=True)

            summary_lines.append(
                f"# User profile backfill — last {days} days\n\n"
            )
            summary_lines.append(
                f"- **Channels:** {', '.join(ch.name for ch in targets)}\n"
            )
            summary_lines.append(
                f"- **Cutoff:** {cutoff.isoformat()}\n"
            )
            summary_lines.append(
                f"- **Threshold:** {MIN_MESSAGES_FOR_PROFILE} messages\n"
            )
            summary_lines.append(f"- **Model:** {settings.gemini_model}\n\n")

            # Parallel batches of GEMINI_CONCURRENCY
            for i in range(0, len(eligible), GEMINI_CONCURRENCY):
                batch = eligible[i:i + GEMINI_CONCURRENCY]
                tasks = []
                for uid, msgs in batch:
                    meta = user_meta[uid]
                    tasks.append(_generate_profile(
                        gemini_client, meta["display_name"], msgs
                    ))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for (uid, msgs), profile in zip(batch, results):
                    meta = user_meta[uid]
                    if isinstance(profile, Exception):
                        print(f"  ✗ {meta['display_name']}: {profile}",
                              flush=True)
                        continue
                    if not profile:
                        print(f"  ✗ {meta['display_name']}: empty response",
                              flush=True)
                        continue
                    last_seen = msgs[-1]["timestamp"]
                    db.upsert_user_profile(
                        user_id=uid,
                        username=meta["username"],
                        display_name=meta["display_name"],
                        profile_text=profile,
                        message_count_at_update=len(msgs),
                        last_seen_message_at=last_seen,
                    )
                    summary_lines.append(
                        f"## {meta['display_name']} ({meta['username']}) — "
                        f"{len(msgs)} msgs\n\n{profile}\n\n---\n\n"
                    )
                    print(
                        f"  ✓ {meta['display_name']:<25} {len(msgs):>4} msgs "
                        f"→ {len(profile)} chars",
                        flush=True,
                    )

            print(f"\nProfiled {len(eligible)} users.", flush=True)
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)

    out_name = (
        f"backfill_user_profiles_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    out_path = _REPO_ROOT / out_name
    out_path.write_text("".join(summary_lines), encoding="utf-8")
    print(f"\nSummary written to {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Days of history to scan (default 30)")
    parser.add_argument(
        "--channels", type=str, default=",".join(DEFAULT_PROFILE_CHANNELS),
        help=f"Comma-separated channel names (default: "
             f"{','.join(DEFAULT_PROFILE_CHANNELS)})"
    )
    args = parser.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels))


if __name__ == "__main__":
    main()
