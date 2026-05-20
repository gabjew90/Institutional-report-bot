"""Smoke test: replay last N days of every registered caller's channel
through the new multi-caller extraction pipeline (image OCR +
caption-only + reply-chain) and post results to #test-channel.

Pure dry-run — does NOT write to the analyst_trades table. The Abe
live pipeline is untouched; this is purely offline analysis.

Per-message output to #test-channel:
  📝 [SMOKE] **{display}** {ACTION} **{TICKER STRIKE(C/P) MM-DD}** @price
    _evidence_
    <jump_url>

Header + final per-caller summary also posted.

Run: railway ssh '/opt/venv/bin/python -m scripts._smoke_caller_replay'
"""
import asyncio
import io
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import aiohttp
import discord
from PIL import Image as PILImage

from analyst_log.ocr import (
    extract_trade_from_caption,
    extract_trade_from_image,
)
from config import settings


log = logging.getLogger(__name__)

LOOKBACK_DAYS = 7
OUTPUT_CHANNEL_NAME = "test-channel"
THROTTLE_SECONDS = 1.5


def _normalize_image_bytes(raw: bytes) -> bytes | None:
    try:
        img = PILImage.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        max_side = 1600
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return None


async def _fetch_parent_caption(
    message: discord.Message,
) -> str | None:
    ref = getattr(message, "reference", None)
    if not ref:
        return None
    parent_id = getattr(ref, "message_id", None)
    if not parent_id:
        return None
    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return (resolved.content or "").strip() or None
    try:
        parent = await message.channel.fetch_message(parent_id)
    except Exception:
        return None
    return (parent.content or "").strip() or None


def _format_smoke_line(
    display_name: str,
    msg: discord.Message,
    extracted: dict,
    parent_caption: str | None,
) -> str:
    """One-line summary for posting to test-channel."""
    is_trade = bool(extracted.get("is_trade_screenshot"))
    if not is_trade:
        reason = extracted.get("what_it_appears_to_be") or "not a trade"
        line = (
            f"⏭️ [SMOKE] **{display_name}** skipped — {reason}\n"
            f"  caption: {(msg.content or '(empty)')[:120]!r}"
        )
        if parent_caption:
            line += f"\n  parent: {parent_caption[:100]!r}"
        line += f"\n  <{msg.jump_url}>"
        return line[:1900]

    ticker = (extracted.get("ticker") or "?").upper()
    strike = extracted.get("strike")
    ctype = (extracted.get("contract_type") or "").lower()
    expiry = extracted.get("expiry") or ""
    action = (extracted.get("action") or "?").upper()
    price = extracted.get("price")
    gain = extracted.get("gain_pct")
    notes = (extracted.get("notes") or "").strip()
    summary = (extracted.get("caption_summary") or "").strip()

    type_suffix = {"call": "C", "put": "P"}.get(ctype, "")
    strike_s = ""
    if strike is not None:
        try:
            strike_s = (
                f"{int(strike)}"
                if float(strike) == int(float(strike))
                else f"{strike}"
            )
        except Exception:
            strike_s = str(strike)
    expiry_short = expiry[5:] if len(expiry) >= 10 else expiry
    contract = f"{ticker} {strike_s}{type_suffix} {expiry_short}".strip()

    line = f"📝 [SMOKE] **{display_name}** {action} **{contract}**"
    is_open = action in ("OPEN", "ADD")
    is_exit = action in ("CLOSE", "TRIM")
    try:
        price_f = float(price) if price is not None else None
    except Exception:
        price_f = None
    try:
        gain_f = float(gain) if gain is not None else None
    except Exception:
        gain_f = None
    if is_open and price_f and price_f != 0:
        line += f" @{price_f}"
    elif is_exit and gain_f is not None and gain_f != 0:
        line += f" ({gain_f:+.1f}%)"

    if summary:
        line += f"\n  _{summary[:200]}_"
    if parent_caption:
        line += f"\n  ↩️ reply-parent: {parent_caption[:120]!r}"
    if notes:
        line += f"\n  note: {notes[:120]}"
    line += f"\n  <{msg.jump_url}>"
    return line[:1900]


async def process_caller(
    bot: discord.Client,
    http_session: aiohttp.ClientSession,
    out_channel: discord.TextChannel,
    caller: dict,
) -> dict:
    """Replay one caller's channel. Returns a stats dict."""
    src_channel: discord.TextChannel | None = None
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name == caller["channel"]:
                src_channel = ch
                break
        if src_channel:
            break

    if src_channel is None:
        print(f"  channel #{caller['channel']} NOT FOUND in any guild")
        await out_channel.send(
            f"⚠️ Smoke test: channel #{caller['channel']} for {caller['display']} "
            f"NOT FOUND — skipping"
        )
        return {"caller": caller["name"], "channel_found": False}

    expected_username = caller["username"].lower()
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    msgs: list[discord.Message] = []
    async for m in src_channel.history(
        limit=None, after=cutoff, oldest_first=True
    ):
        if m.author.bot:
            continue
        if m.author.name.lower() != expected_username:
            continue
        msgs.append(m)

    await out_channel.send(
        f"\n🧪 **SMOKE TEST: {caller['display']}** — last {LOOKBACK_DAYS} days from "
        f"#{caller['channel']}\n"
        f"Found {len(msgs)} non-bot messages from `{caller['username']}`"
    )
    await asyncio.sleep(THROTTLE_SECONDS)

    stats = {
        "caller": caller["name"],
        "channel_found": True,
        "msgs_total": len(msgs),
        "trades_logged": 0,
        "skipped_not_trade": 0,
        "errors": 0,
        "image_messages": 0,
        "caption_only_messages": 0,
        "replies": 0,
    }

    for m in msgs:
        caption = (m.content or "").strip()
        parent_caption = await _fetch_parent_caption(m)
        if parent_caption:
            stats["replies"] += 1

        image_attachments = [
            a for a in m.attachments
            if (a.content_type or "").lower().startswith("image/")
        ]

        try:
            if image_attachments:
                stats["image_messages"] += 1
                # Use the first image (typical case — multi-image posts rare)
                att = image_attachments[0]
                blob = None
                try:
                    blob = await att.read()
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  attachment read fail: {e}")
                    continue
                # Note: extract_trade_from_image handles raw bytes — no
                # PIL re-encode (it does its own thing internally if needed).
                extracted = await extract_trade_from_image(
                    blob, att.content_type or "image/jpeg",
                    caption, parent_caption=parent_caption,
                    caller_name=caller["display"],
                )
            elif caption:
                stats["caption_only_messages"] += 1
                extracted = await extract_trade_from_caption(
                    caption, parent_caption=parent_caption,
                    caller_name=caller["display"],
                )
            else:
                # Empty + no attachments — skip
                continue
        except Exception as e:
            stats["errors"] += 1
            print(f"  extraction exception: {e}")
            continue

        if extracted is None:
            stats["errors"] += 1
            continue

        if extracted.get("is_trade_screenshot"):
            stats["trades_logged"] += 1
        else:
            stats["skipped_not_trade"] += 1

        line = _format_smoke_line(
            caller["display"], m, extracted, parent_caption
        )
        try:
            await out_channel.send(line)
        except Exception as e:
            stats["errors"] += 1
            print(f"  post failed: {e}")
        await asyncio.sleep(THROTTLE_SECONDS)

    # Per-caller summary
    summary = (
        f"🏁 **{caller['display']} smoke complete**\n"
        f"- Messages scanned: {stats['msgs_total']}\n"
        f"  · with image: {stats['image_messages']}\n"
        f"  · caption-only: {stats['caption_only_messages']}\n"
        f"  · reply to prior post: {stats['replies']}\n"
        f"- Trades logged (would-be): {stats['trades_logged']}\n"
        f"- Skipped as non-trade: {stats['skipped_not_trade']}\n"
        f"- Errors: {stats['errors']}"
    )
    await out_channel.send(summary)
    print(summary)
    return stats


async def main():
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN missing", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            # Find the output channel
            out_channel: discord.TextChannel | None = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name == OUTPUT_CHANNEL_NAME:
                        out_channel = ch
                        break
                if out_channel:
                    break
            if out_channel is None:
                print(f"ERROR: #{OUTPUT_CHANNEL_NAME} not found")
                return

            await out_channel.send(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🧪 **Multi-caller smoke test** — replay last {LOOKBACK_DAYS} days "
                f"per caller through the new pipeline (image OCR + caption-only "
                f"+ reply-chain). Dry-run — does NOT touch analyst_trades.\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )

            callers = settings.resolve_analyst_callers()
            print(f"Replaying {len(callers)} callers: "
                  f"{[c['name'] for c in callers]}")

            async with aiohttp.ClientSession() as http_session:
                for caller in callers:
                    print(f"\n=== Processing {caller['display']} "
                          f"(#{caller['channel']}) ===")
                    await process_caller(client, http_session, out_channel, caller)
                    await asyncio.sleep(THROTTLE_SECONDS)

            await out_channel.send(
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "🏁 **Smoke test complete.**"
            )

        except Exception as e:
            log.error(f"Smoke test failed: {e}", exc_info=True)
            print(f"ERROR: {e}")
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
