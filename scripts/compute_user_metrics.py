"""Weekly job — compute the two hidden hierarchies in user_metrics.

Run separately or via the scheduler. Two parts in one pass over the
last 30 days of yapping-channel messages:

1. SLUR COUNT — per-user count of regex-matched slur occurrences in
   the user's OWN messages. Pattern definitions in slur_patterns.py.
   Noisy on purpose — feeds ordinal comparisons only.

2. TRADER SKILL — per-user 0-100 score assessed by Gemini using the
   user's profile_text plus signals from their recent messages
   (mentions of wins/losses, conviction patterns, specific trades).
   Stored alongside an ordinal `trader_rank` (1 = best).

Both metrics live in `user_metrics`. The /ask context block surfaces
them under a "PRIVATE METRICS" header with instructions that they're
for comparative answers only — NEVER enumerated unsolicited, NEVER
quoted as raw counts to users.

Usage:
    railway ssh -- "/opt/venv/bin/python scripts/compute_user_metrics.py"
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

import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

import db  # noqa: E402
from config import settings  # noqa: E402
from scripts.slur_patterns import count_slurs_in_text  # noqa: E402

DEFAULT_CHANNELS = [
    "💬-stonks-yapping-💬",
    "₿-crypto-yapping-₿",
    "🏃-fitness-yapping-🏋",
]


TRADER_SCORE_PROMPT = """\
You're scoring trading skill for one user in a private options-trading discord.

Below: their profile dossier + a sample of their recent chat messages.

Output a single integer score 0-100 reflecting their trading skill as best
you can assess from the available signal:

- 90-100: Consistent winners. Clear edge. Specific entries / exits with
  reasoned thesis. Documented gains across multiple trades. Others tail them.
- 75-89: Solid traders. Mostly green, occasional misses they own. Process
  visible. Strong on a specific style or sector.
- 60-74: Competent but uneven. Some wins, some losses, no clear edge yet.
  Trade discussion shows thought but execution is mixed.
- 40-59: Mid pack. Equal-parts hits and bag-holding. Often chases rather
  than initiates. No documented sustained green streak.
- 20-39: Below average. More documented losses than wins. Chases bag-holds
  shows tilt patterns repeatedly.
- 0-19: Cautionary tales. The room references them as exit-liquidity
  examples.

Output STRICT JSON only, no prose:

{{
  "score": <integer 0-100>,
  "rationale": "<one sentence explaining the score>"
}}

USER: {display_name} ({username})

PROFILE:
{profile_text}

RECENT MESSAGES (last 30 days, oldest first, last 200 chars per msg):
{chat_sample}
"""


async def _score_trader(
    client: genai.Client,
    display_name: str,
    username: str,
    profile_text: str,
    chat_sample: str,
) -> tuple[int | None, str | None]:
    prompt = TRADER_SCORE_PROMPT.format(
        display_name=display_name,
        username=username,
        profile_text=profile_text or "(no profile)",
        chat_sample=chat_sample or "(no recent chat)",
    )
    try:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=300,
                response_mime_type="application/json",
            ),
        )
        text = (response.text or "").strip()
        if not text:
            return None, None
        data = json.loads(text)
        score = int(data.get("score"))
        rationale = data.get("rationale") or None
        score = max(0, min(100, score))
        return score, rationale
    except Exception as e:
        print(f"  ✗ trader score failed for {display_name}: {e}", flush=True)
        return None, None


async def run(days: int = 30, channels: list[str] = None) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    channels = channels or [
        c.strip() for c in (settings.profile_channels or "").split(",") if c.strip()
    ] or DEFAULT_CHANNELS

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)

    @client.event
    async def on_ready():
        try:
            targets: list[discord.TextChannel] = []
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name in channels:
                        targets.append(ch)
            if not targets:
                print(f"ERROR: none of {channels} found", flush=True)
                return
            print(f"Channels: {[ch.name for ch in targets]}", flush=True)
            print(f"Scanning last {days} days for slur counts + chat sample...", flush=True)

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            # Per-user accumulators
            slur_counts: dict[int, int] = defaultdict(int)
            msg_counts: dict[int, int] = defaultdict(int)
            chat_samples: dict[int, list[str]] = defaultdict(list)
            user_meta: dict[int, dict] = {}

            for ch in targets:
                print(f"  Scanning #{ch.name}...", flush=True)
                async for msg in ch.history(limit=None, after=cutoff,
                                            oldest_first=True):
                    if msg.author.bot:
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
                    content = (msg.content or "").strip()
                    if not content:
                        continue
                    msg_counts[uid] += 1
                    slur_counts[uid] += count_slurs_in_text(content)
                    # Keep last 60 chars per message for the chat sample so
                    # the trader-score LLM gets recent signal without a
                    # huge token bill.
                    chat_samples[uid].append(content[:200])

            print(f"  Scanned {sum(msg_counts.values())} messages from {len(user_meta)} users",
                  flush=True)

            # Pull existing profiles (only profiled users get a trader_score —
            # lurkers aren't worth the LLM call)
            profiles = db.get_profiles_for_users(list(user_meta.keys()))
            print(f"\n{len(profiles)} profiled users — scoring trader skill...",
                  flush=True)

            scored = 0
            for uid, meta in user_meta.items():
                slur_n = slur_counts.get(uid, 0)
                total_n = msg_counts.get(uid, 0)

                trader_score: int | None = None
                trader_rationale: str | None = None
                if uid in profiles:
                    profile = profiles[uid]
                    sample_msgs = chat_samples.get(uid, [])[-150:]
                    sample_block = "\n".join(sample_msgs)
                    trader_score, trader_rationale = await _score_trader(
                        gemini_client,
                        meta["display_name"],
                        meta["username"],
                        profile.get("profile_text", ""),
                        sample_block,
                    )
                    if trader_score is not None:
                        scored += 1

                db.upsert_user_metrics(
                    user_id=uid,
                    slur_count_30d=slur_n,
                    total_messages_30d=total_n,
                    trader_score=trader_score,
                    trader_rationale=trader_rationale,
                )
                if trader_score is not None:
                    print(
                        f"  ✓ {meta['display_name']:<25} "
                        f"slur={slur_n:>3} / msgs={total_n:>4} | "
                        f"trader={trader_score}",
                        flush=True,
                    )

            # Compute ordinal trader_rank — recompute on the whole table
            # so additions/deletions stay consistent.
            db.recompute_trader_ranks()
            print(f"\nScored {scored} traders, recomputed ordinal ranks.", flush=True)

        finally:
            await client.close()

    await client.start(settings.discord_bot_token)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--channels", type=str,
        default=",".join(DEFAULT_CHANNELS),
    )
    args = parser.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels))


if __name__ == "__main__":
    main()
