"""Probe: fetch recent messages from a Discord channel and run Gemini vision
on each image attachment, dumping the extracted structured data so we can
evaluate OCR accuracy before committing to the full trade-log watcher.

Usage:
    py scripts/test_analyst_ocr.py --channel-name trades --limit 20

Output: writes `analyst_ocr_probe_<timestamp>.md` to the repo root containing
one block per image:
  - timestamp + author + text caption (if any)
  - Gemini's JSON extraction
  - the Discord CDN URL so you can eyeball the original

Uses the bot's existing `settings.discord_bot_token` and `settings.google_api_key`
from the .env / Railway env. No new credentials needed.

The Gemini call uses temperature=0 + structured-output mode for deterministic
extraction. Same model the bot uses for /ask (`settings.gemini_model`).

When you've reviewed the output, that becomes the ground-truth check for
whether the OCR pipeline is reliable enough to wire up the analyst trade log.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the repo importable when running from `scripts/`
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from config import settings  # noqa: E402


# Structured-extraction prompt. Asks Gemini to return JSON with the fields
# the trade log would need. Tolerates non-trade images (memes, charts, etc.)
# via the is_trade_screenshot=false branch.
GEMINI_PROMPT = """\
You are looking at an image posted in a trading group chat. Determine what \
the image is and extract structured data if it's a trade screenshot.

Return STRICT JSON only — no prose, no markdown wrapper. Schema:

If this IS a trade screenshot (Robinhood, ThinkOrSwim, Tastytrade, Webull, IBKR, \
crypto exchange, etc.):
{
  "is_trade_screenshot": true,
  "broker": "Robinhood | Tastytrade | ThinkOrSwim | Webull | IBKR | crypto | unknown",
  "action": "open | add | trim | close | unclear",
  "ticker": "string (e.g. NVDA, BTC, SOL)",
  "contract_type": "call | put | stock | crypto | future | unclear",
  "strike": number or null (NULL for stock/crypto),
  "expiry": "YYYY-MM-DD" or null (NULL for stock/crypto),
  "qty": number,
  "entry_or_exit_price": number,
  "total_cost_or_proceeds": number or null,
  "caption": "one-line plain-English summary, e.g. 'opened $NVDA 150C 5/30 x10 @ $4.20'",
  "confidence": "high | medium | low",
  "notes": "ambiguities, partial info, P&L visible, anything noteworthy"
}

If this is NOT a trade screenshot (chart, meme, news headline, random image):
{
  "is_trade_screenshot": false,
  "what_it_appears_to_be": "string — chart of XYZ, meme, news headline, etc.",
  "confidence": "high | medium | low"
}

Output the JSON object ONLY.\
"""


def _fmt_msg_block(idx: int, msg: discord.Message, att: discord.Attachment,
                   gemini_json: str | None, error: str | None) -> str:
    """Render one image's extraction as a markdown block."""
    ts = msg.created_at.strftime("%Y-%m-%d %H:%M UTC")
    author = getattr(msg.author, "display_name", None) or msg.author.name
    caption = (msg.content or "").strip()
    parts = [
        f"### Image {idx} — {ts} — {author}",
        "",
    ]
    if caption:
        parts.append(f"**Caption posted with image:** {caption}")
        parts.append("")
    parts.append(f"**Discord URL:** {att.url}")
    parts.append(f"**File:** {att.filename} ({att.size} bytes, {att.content_type})")
    parts.append("")
    if error:
        parts.append(f"**Gemini error:** `{error}`")
    else:
        parts.append("**Gemini extraction:**")
        parts.append("```json")
        parts.append(gemini_json or "(empty)")
        parts.append("```")
    parts.append("")
    parts.append("---")
    parts.append("")
    return "\n".join(parts)


async def _process_image(gemini_client, msg: discord.Message,
                         att: discord.Attachment) -> tuple[str | None, str | None]:
    """Return (gemini_json_text, error) for a single image attachment."""
    try:
        img_bytes = await att.read()
    except Exception as e:
        return None, f"Failed to download image: {e}"

    if len(img_bytes) > 10 * 1024 * 1024:
        return None, f"Image too large ({len(img_bytes)} bytes — capped at 10MB)"

    try:
        response = await gemini_client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Part.from_bytes(
                    data=img_bytes,
                    mime_type=att.content_type or "image/jpeg",
                ),
                types.Part.from_text(text=GEMINI_PROMPT),
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=600,
                response_mime_type="application/json",
            ),
        )
        return (response.text or "").strip(), None
    except Exception as e:
        return None, f"Gemini call failed: {e}"


async def run(channel_name: str, limit: int) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set in env/.env", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY not set in env/.env", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)

    output_blocks: list[str] = []
    msg_count = 0
    image_count = 0

    @client.event
    async def on_ready():
        nonlocal msg_count, image_count
        try:
            target = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name.lower() == channel_name.lower():
                        target = ch
                        break
                if target:
                    break

            if target is None:
                visible = sorted({
                    ch.name for g in client.guilds for ch in g.text_channels
                })
                print(f"ERROR: channel '{channel_name}' not found.", file=sys.stderr)
                print(f"Visible channels: {visible}", file=sys.stderr)
                return

            print(f"Channel: #{target.name} in '{target.guild.name}' "
                  f"(id={target.id})", flush=True)
            print(f"Pulling last {limit} messages, OCR'ing every image...\n", flush=True)

            output_blocks.append(f"# Analyst OCR probe — #{target.name}\n")
            output_blocks.append(f"- **Channel:** #{target.name} "
                                 f"(id={target.id}, guild={target.guild.name})\n")
            output_blocks.append(f"- **Limit:** {limit} messages\n")
            output_blocks.append(f"- **Generated:** "
                                 f"{datetime.now(timezone.utc).isoformat()}\n")
            output_blocks.append(f"- **Model:** {settings.gemini_model}\n\n---\n\n")

            async for msg in target.history(limit=limit):
                msg_count += 1
                for att in msg.attachments:
                    ct = (att.content_type or "").lower()
                    if not ct.startswith("image/"):
                        continue
                    image_count += 1
                    print(f"  → image {image_count} from "
                          f"{getattr(msg.author, 'display_name', msg.author.name)} "
                          f"at {msg.created_at.strftime('%m-%d %H:%M')}",
                          flush=True)
                    gem_json, err = await _process_image(gemini_client, msg, att)
                    output_blocks.append(
                        _fmt_msg_block(image_count, msg, att, gem_json, err)
                    )

            footer = (
                f"\n## Summary\n\n"
                f"- Processed **{msg_count}** messages\n"
                f"- Found and OCR'd **{image_count}** image attachments\n"
            )
            output_blocks.append(footer)
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)

    out_name = (
        f"analyst_ocr_probe_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    out_path = _REPO_ROOT / out_name
    out_path.write_text("".join(output_blocks), encoding="utf-8")
    print(f"\nWrote {len(output_blocks)} blocks to {out_path}")
    print(f"Open it with: code {out_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--channel-name", required=True,
        help="Channel name to probe (without '#'). Case-insensitive."
    )
    parser.add_argument(
        "--limit", type=int, default=20,
        help="How many recent messages to scan (default 20)."
    )
    args = parser.parse_args()
    asyncio.run(run(args.channel_name, args.limit))


if __name__ == "__main__":
    main()
