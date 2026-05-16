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
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Windows console defaults to cp1252 — emoji prints crash with
# UnicodeEncodeError and discord.py silently swallows exceptions in event
# handlers. Force UTF-8 stdout/stderr before anything else runs.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# Make the repo importable when running from `scripts/`
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

from config import settings  # noqa: E402


# Abe-specific extraction prompt. Calibrated to his actual posting style:
# Robinhood notification cards showing ticker / strike / expiry / Buy-pill /
# gain%, with the Discord text caption carrying the actual open/close intent.
# He doesn't post qty or fill price — those fields are almost always null.
def _build_prompt(today_iso: str, caption: str) -> str:
    cap_block = (
        f'\n\nThe USER POSTED THIS CAPTION WITH THE IMAGE: "{caption.strip()}"\n'
        f'Use this caption as the PRIMARY signal for the `action` field — '
        f'see the caption-mapping table below.'
    ) if caption.strip() else (
        '\n\nNo text caption was posted with this image. Decide action '
        'from the image alone (see fallback rules below).'
    )
    return f"""\
You are looking at an image from a trading group chat. The user is "Abe," a \
trader who posts Robinhood screenshots with specific characteristics. Today's \
date is {today_iso}.

ABE'S TYPICAL POSTING FORMAT — a Robinhood notification card showing:
- Ticker + strike + Call/Put (e.g. "NVDA $150 Call")
- Month/day + Buy/Sell pill (e.g. "5/29 · Buy")
- Green +N% or red -N% gain pill on the right

These are NOTIFICATION cards (current P&L of an existing position), NOT order \
execution tickets. He DOES NOT post quantity, entry price, exit price, or \
spot price. The "Buy" pill is the ORIGINAL order type, not the current action.

He sometimes also posts:
- A stats/quote screen (Bid/Ask/Mark/IV) when viewing an option to buy
- A tweet screenshot or chart (not a trade — flag as is_trade_screenshot=false)
{cap_block}

CAPTION → ACTION MAPPING — IMPORTANT ASYMMETRY:

False-positive opens (saying "open" when Abe was just viewing) are MUCH \
worse than false-negative opens (saying "viewing" when he actually opened). \
A wrongly-detected open creates a phantom position in the log that the bot \
will reference forever; a missed open self-corrects when Abe posts a \
notification card later. **When in doubt about an open, default to \
"viewing".** Closes are easier to verify (gain-pill + caption usually agree), \
so be more confident classifying closes.

STRONG CLOSE signals → action="close" (these are unambiguous; trust them):
- "I'm out", "OUT", "out!", "exiting", "exit", "sold", "bing bong", "done", \
  "took it", "took profit", "thx for the bag"

STRONG OPEN signals → action="open" (clear re-entry or fresh buy language):
- "re-entering", "reload", "back in", "rinse repeat", "opened", "loading", \
  "got some", "in at"
- "add", "adding", "doubled up" → action="add"
- "trim", "trimming", "took some off" → action="trim"

AMBIGUOUS / HYPE captions on a stats_screen → action="viewing" (bias \
toward viewing, not open). Don't assume an open from excitement alone:
- "SLAM", "let's go", "fire", "🚀", "this one", "watch this", "👀"
- These could mean "I just bought" OR "look at this setup" — when on a \
  stats_screen (no execution evidence in the image), default to viewing.
- ONLY classify as open if the caption is genuinely unambiguous (per the \
  STRONG OPEN list above).

NO CAPTION:
- On a notification_card with Buy-pill + gain% → "viewing" (Abe is reviewing \
  the position; not actively opening or closing in this post)
- On a stats_screen → "viewing"
- Never default to "open" from a captionless image.

EXPIRY YEAR INFERENCE: Robinhood notification cards show only "M/D" not the \
year. Use today's date ({today_iso}) to infer the year with this priority:

1. If the M/D expiry is on or after today's M/D → CURRENT YEAR.
2. If the M/D expiry is BEFORE today, but within the last 14 days → \
   CURRENT YEAR. Abe is posting about a contract that just expired or \
   is about to expire — those are this-year contracts, not next-year.
3. Only if the M/D expiry is more than 14 days BEFORE today (so clearly \
   stale) → NEXT YEAR.
4. Never use years before today's year unless the screenshot explicitly \
   shows them.

Example: today is 2026-05-16 and screenshot shows "5/15" expiry → 2026-05-15 \
(rule 2, just expired yesterday). Same screenshot with "1/15" expiry → \
2027-01-15 (rule 3, 4 months past — stale, must be next year).

Return STRICT JSON only — no prose, no markdown wrapper.

For trade screenshots:
{{
  "is_trade_screenshot": true,
  "screenshot_type": "notification_card | stats_screen | order_ticket | other",
  "ticker": "string (e.g. NVDA, NOW, MSFT)",
  "contract_type": "call | put | stock | crypto | unclear",
  "strike": number or null,
  "expiry": "YYYY-MM-DD" or null (use the inference rule above),
  "action": "open | add | trim | close | viewing | unclear",
  "action_source": "caption | image | both",
  "gain_pct": number or null (the +/-N% pill, if shown),
  "caption_summary": "one-line plain English combining what's shown + the caption",
  "confidence": "high | medium | low",
  "notes": "anything notable — Robinhood UI ambiguity, partial info, etc"
}}

For non-trade images:
{{
  "is_trade_screenshot": false,
  "what_it_appears_to_be": "string description",
  "confidence": "high | medium | low"
}}

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

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    caption = (msg.content or "").strip()
    prompt = _build_prompt(today_iso, caption)
    try:
        response = await gemini_client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Part.from_bytes(
                    data=img_bytes,
                    mime_type=att.content_type or "image/jpeg",
                ),
                types.Part.from_text(text=prompt),
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
        # Force flush so we see output even if a later exception is swallowed
        # by discord.py's event-handler exception handling.
        print(f"[on_ready] connected as {client.user}, "
              f"{len(client.guilds)} guild(s) visible", flush=True)
        try:
            # Dump every visible channel up-front so we can diagnose name
            # mismatches deterministically — emoji normalization (NFC vs NFD)
            # can make the equality check fail invisibly.
            print(f"[on_ready] looking for channel '{channel_name}'", flush=True)
            print(f"[on_ready] channel_name bytes: "
                  f"{channel_name.encode('utf-8').hex()}", flush=True)
            all_channels = [
                (g.name, ch.name, ch.name.encode('utf-8').hex())
                for g in client.guilds for ch in g.text_channels
            ]
            print(f"[on_ready] visible text channels:", flush=True)
            for guild_name, ch_name, ch_hex in all_channels:
                print(f"   {guild_name} / #{ch_name} (utf8={ch_hex})",
                      flush=True)

            import unicodedata
            target_norm = unicodedata.normalize("NFC", channel_name).lower()
            target = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    ch_norm = unicodedata.normalize("NFC", ch.name).lower()
                    if ch_norm == target_norm:
                        target = ch
                        break
                if target:
                    break

            if target is None:
                print(f"ERROR: channel '{channel_name}' not found "
                      f"(checked {len(all_channels)} channels with NFC norm).",
                      file=sys.stderr, flush=True)
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
