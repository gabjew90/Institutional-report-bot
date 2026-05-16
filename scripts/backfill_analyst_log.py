"""One-shot backfill of the analyst-trade log.

Pulls the last N days of messages from settings.analyst_channel_name,
OCR's every image via analyst_log.ocr.extract_trade_from_image, and
writes rows into the analyst_trades table. Does NOT post announcements
to the test channel — backfilling a week of trades shouldn't spam #test
with 50 retroactive log entries.

Usage:
    py scripts/backfill_analyst_log.py --days 5

Reuses the same Gemini prompt the live watcher uses (via the shared
analyst_log.ocr module), so accuracy on backfilled rows matches the
production OCR pipeline.

Output: prints a markdown summary to stdout AND saves it to
backfill_analyst_log_<timestamp>.md in the repo root for human review.
"""

import argparse
import asyncio
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Windows console emoji-safe stdout
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import discord  # noqa: E402

import db  # noqa: E402
from analyst_log.ocr import extract_trade_from_image  # noqa: E402
from config import settings  # noqa: E402


async def run(days: int, force: bool = False) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not settings.analyst_channel_name:
        print("ERROR: ANALYST_CHANNEL_NAME not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    summary_lines: list[str] = []
    stats = {"messages": 0, "images": 0, "trades": 0, "non_trade": 0,
             "skipped_dedup": 0, "skipped_author": 0, "errors": 0}

    @client.event
    async def on_ready():
        try:
            target = None
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name == settings.analyst_channel_name:
                        target = ch
                        break
                if target:
                    break
            if target is None:
                print(f"ERROR: channel '{settings.analyst_channel_name}' "
                      f"not found", file=sys.stderr, flush=True)
                return

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            primary = (settings.analyst_primary_author or "").strip().lower()

            print(f"Channel: #{target.name} in '{target.guild.name}'", flush=True)
            print(f"Backfilling messages since {cutoff.isoformat()} "
                  f"({days} days)", flush=True)
            if primary:
                print(f"Author filter: '{primary}'", flush=True)
            print(flush=True)

            summary_lines.append(f"# Analyst log backfill — last {days} days\n")
            summary_lines.append(
                f"- **Channel:** #{target.name} "
                f"(id={target.id}, guild={target.guild.name})\n"
            )
            summary_lines.append(f"- **Cutoff:** {cutoff.isoformat()}\n")
            summary_lines.append(
                f"- **Author filter:** {primary or '(none — log all)'}\n"
            )
            summary_lines.append(f"- **Model:** {settings.gemini_model}\n\n")
            summary_lines.append("| Posted | Author | Action | Contract | Gain | Caption |\n")
            summary_lines.append("|---|---|---|---|---|---|\n")

            async for msg in target.history(limit=None, after=cutoff,
                                            oldest_first=True):
                stats["messages"] += 1
                if not msg.attachments:
                    continue

                author_username = (msg.author.name or "").lower()
                author_display = (
                    getattr(msg.author, "display_name", None)
                    or msg.author.name
                )

                if primary and author_username != primary:
                    stats["skipped_author"] += 1
                    continue

                caption = (msg.content or "").strip()
                posted_at = msg.created_at.isoformat()

                for att in msg.attachments:
                    ct = (att.content_type or "").lower()
                    if not ct.startswith("image/"):
                        continue
                    stats["images"] += 1

                    if not force and db.analyst_trade_exists(msg.id, att.id):
                        stats["skipped_dedup"] += 1
                        continue

                    try:
                        img_bytes = await att.read()
                    except Exception as e:
                        print(f"  read failed: {e}", flush=True)
                        stats["errors"] += 1
                        continue

                    extracted = await extract_trade_from_image(
                        img_bytes, ct, caption
                    )
                    if extracted is None:
                        stats["errors"] += 1
                        continue

                    is_trade = bool(extracted.get("is_trade_screenshot"))
                    if is_trade:
                        stats["trades"] += 1
                    else:
                        stats["non_trade"] += 1

                    db.record_analyst_trade(
                        discord_message_id=msg.id,
                        discord_attachment_id=att.id,
                        author=author_display,
                        posted_at=posted_at,
                        image_url=att.url,
                        caption=caption,
                        is_trade=is_trade,
                        gemini_json=extracted,
                        ticker=extracted.get("ticker") if is_trade else None,
                        contract_type=extracted.get("contract_type") if is_trade else None,
                        strike=extracted.get("strike") if is_trade else None,
                        expiry=extracted.get("expiry") if is_trade else None,
                        action=extracted.get("action") if is_trade else None,
                        gain_pct=extracted.get("gain_pct") if is_trade else None,
                        replace=force,
                    )

                    if is_trade:
                        # Live print to console
                        ticker = extracted.get("ticker") or "?"
                        ct_short = {"call": "C", "put": "P"}.get(
                            (extracted.get("contract_type") or "").lower(), ""
                        )
                        strike_val = extracted.get("strike")
                        strike_str = (
                            f"{int(strike_val) if strike_val == int(strike_val) else strike_val}"
                            if strike_val is not None else "?"
                        )
                        exp = extracted.get("expiry") or "?"
                        exp_short = exp[5:] if len(exp) >= 10 else exp
                        action = (extracted.get("action") or "?").upper()
                        gain = extracted.get("gain_pct")
                        gain_str = f"{gain:+.1f}%" if gain is not None else ""
                        cap_short = (caption.replace("|", "/").replace("\n", " ")[:60]
                                     if caption else "")
                        print(
                            f"  → {posted_at[:16]} | {action:8} "
                            f"{ticker} {strike_str}{ct_short} {exp_short:<8} "
                            f"{gain_str:>8}  {cap_short}",
                            flush=True,
                        )
                        summary_lines.append(
                            f"| {posted_at[:16]} | {author_display} | "
                            f"{action} | {ticker} {strike_str}{ct_short} "
                            f"{exp_short} | {gain_str} | "
                            f"{cap_short.replace('|','/')} |\n"
                        )

            summary_lines.append(f"\n## Summary\n\n")
            summary_lines.append(f"- Messages scanned: {stats['messages']}\n")
            summary_lines.append(f"- Image attachments seen: {stats['images']}\n")
            summary_lines.append(f"- Trade rows inserted: {stats['trades']}\n")
            summary_lines.append(f"- Non-trade images logged: {stats['non_trade']}\n")
            summary_lines.append(f"- Skipped (already in DB): {stats['skipped_dedup']}\n")
            summary_lines.append(f"- Skipped (author filter): {stats['skipped_author']}\n")
            summary_lines.append(f"- Errors: {stats['errors']}\n")
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)

    out_name = (
        f"backfill_analyst_log_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    out_path = _REPO_ROOT / out_name
    out_path.write_text("".join(summary_lines), encoding="utf-8")
    print(f"\nSummary written to {out_path}", flush=True)
    print(f"Stats: {stats}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=5,
                        help="How many days back to backfill (default 5)")
    parser.add_argument("--force", action="store_true",
                        help="Re-OCR every image and UPDATE existing rows. "
                             "Use when the OCR prompt has changed and we "
                             "want existing rows re-classified.")
    args = parser.parse_args()
    asyncio.run(run(args.days, force=args.force))


if __name__ == "__main__":
    main()
