"""One-shot backfill: re-classify last 30d of eager-OCR channel
messages through the text+vision classifier.

For each chat_messages row in the last 30 days in
chat_eager_ocr_channels with content OR cached image_ocr_text:
  1. Skip if analyst_trades already has a row for the
     discord_message_id (covers existing image-OCR rows; saves a
     Gemini call too).
  2. Build classifier input: message.content (text caption) +
     image_ocr_text (eager-OCR's prior screenshot extraction). Discord
     CDN URLs expire ~24h so re-fetching the original images isn't
     viable for old messages — the cached OCR is the next best signal.
  3. Run unified classifier; write analyst_trades row via
     insert_text_extracted_trade_if_not_dup (Tier 1 + Tier 2 dedup).

Resumable: writes a per-channel checkpoint to the processing_log
table; on re-run, only processes messages with
discord_message_id > checkpoint.

Usage:
    PYTHONPATH=. py scripts/backfill_text_extracted_trades.py
    PYTHONPATH=. py scripts/backfill_text_extracted_trades.py \\
        --reset-checkpoints  # start over from scratch
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Ensure imports work
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db
from config import settings


_CHECKPOINT_EVENT_TYPE = "text_backfill_checkpoint"


def _write_checkpoint(channel: str, last_msg_id: int) -> None:
    """Persist the last-processed discord_message_id for a channel."""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO processing_log (pdf_file_id, event_type, status, "
        "details, created_at) VALUES (NULL, ?, 'ok', ?, datetime('now'))",
        (
            _CHECKPOINT_EVENT_TYPE,
            json.dumps({"channel": channel, "last_msg_id": int(last_msg_id)}),
        ),
    )
    conn.commit()


def _read_checkpoint(channel: str) -> int:
    """Return the latest checkpointed discord_message_id for a channel
    (0 if none)."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT details FROM processing_log "
        "WHERE event_type = ? ORDER BY id DESC",
        (_CHECKPOINT_EVENT_TYPE,),
    ).fetchall()
    for r in rows:
        try:
            p = json.loads(r[0])
            if p.get("channel") == channel:
                return int(p.get("last_msg_id") or 0)
        except Exception:
            continue
    return 0


def _iter_messages_to_process(channel: str):
    """Yield rows from chat_messages where discord_message_id is greater
    than the checkpoint, channel matches, posted_at is within 30 days,
    and there's some content to classify (text OR cached OCR).

    Pulls image_ocr_text alongside content so the classifier can see
    what the screenshot contained even though we can't re-fetch the
    expired CDN URL."""
    last_seen = _read_checkpoint(channel)
    conn = db.get_connection()
    return conn.execute(
        "SELECT discord_message_id, author_id, author_username, content, "
        "posted_at, has_attachments, attachment_urls, image_ocr_text "
        "FROM chat_messages "
        "WHERE channel_name = ? "
        "  AND discord_message_id > ? "
        "  AND posted_at > datetime('now', '-30 days') "
        "  AND (COALESCE(content, '') != '' OR image_ocr_text IS NOT NULL) "
        "ORDER BY discord_message_id ASC",
        (channel, last_seen),
    ).fetchall()


async def _process_message(row, channel: str) -> bool:
    """Classify one chat_messages row, write analyst_trades row if it's
    a trade. Returns True if a row was written.

    Skips messages that already have an analyst_trades row for the same
    discord_message_id (covers both legacy image-OCR rows AND any text
    rows from a prior backfill run). The Tier 1 dedup in the write
    helper would catch this too, but checking here saves a Gemini call.
    """
    from analyst_log.ocr import extract_trade_from_message

    conn = db.get_connection()
    existing = conn.execute(
        "SELECT 1 FROM analyst_trades WHERE discord_message_id = ? LIMIT 1",
        (int(row["discord_message_id"]),),
    ).fetchone()
    if existing:
        return False

    text = (row["content"] or "").strip()
    cached_ocr = (row["image_ocr_text"] or "").strip()
    if not text and not cached_ocr:
        return False

    extracted = await extract_trade_from_message(
        text=text,
        image_bytes_list=[],  # backfill: no live image bytes
        cached_ocr_text=cached_ocr,
        author_username=row["author_username"] or "unknown",
        channel_name=channel,
    )
    if not extracted:
        return False
    inserted = db.insert_text_extracted_trade_if_not_dup(
        author_id=int(row["author_id"]),
        author_username=row["author_username"] or "unknown",
        discord_message_id=int(row["discord_message_id"]),
        posted_at=row["posted_at"],
        extracted=extracted,
        channel_name=channel,
    )
    return inserted


async def _run_backfill(channels: list[str], reset: bool) -> None:
    if reset:
        conn = db.get_connection()
        conn.execute(
            "DELETE FROM processing_log WHERE event_type = ?",
            (_CHECKPOINT_EVENT_TYPE,),
        )
        conn.commit()
        print("(reset_checkpoints: cleared)")
    total_written = 0
    for ch in channels:
        print(f"\nProcessing channel: {ch}")
        rows = list(_iter_messages_to_process(ch))
        print(f"  {len(rows)} messages above checkpoint to process")
        for r in rows:
            try:
                wrote = await _process_message(r, ch)
                if wrote:
                    total_written += 1
            except Exception as e:
                print(f"  ERROR on msg {r['discord_message_id']}: {e}")
            # Update checkpoint after EACH processed msg so resume is
            # tight if we crash mid-channel.
            _write_checkpoint(ch, int(r["discord_message_id"]))
        print(f"  done. text rows written so far: {total_written}")
    print(f"\nBackfill complete. Total rows written: {total_written}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--reset-checkpoints", action="store_true",
        help="Clear all backfill checkpoints, start over",
    )
    args = ap.parse_args()
    channels = sorted(settings.resolve_chat_eager_ocr_channels())
    print(f"Target channels ({len(channels)}):")
    for ch in channels:
        print(f"  - {ch}")
    asyncio.run(_run_backfill(channels, reset=args.reset_checkpoints))


if __name__ == "__main__":
    main()
