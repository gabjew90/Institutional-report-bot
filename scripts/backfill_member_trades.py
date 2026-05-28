"""One-shot backfill: extract historical member-mode trade rows from
chat_messages into analyst_trades.

Why: the universal trade tracking dispatch (Commit 4b0a7b1, May 28)
only fires the analyst_log watcher in member-mode on NEW incoming
messages. Historical posts in eager-OCR alert channels by non-callers
(member-alerts, gain-loss-porn, kloh-alerts, zhawk-thawghts, spot-bag,
0dte-lotto, crypto-alerts) never triggered extraction — they're sitting
in chat_messages with image_ocr_text populated (for posts whose images
made it through eager-OCR) but no analyst_trades row exists.

This script walks those candidates, runs extract_trade_from_caption
against the merged (content + image_ocr_text), and writes
analyst_trades rows with tracking_mode='member' if the extraction
yields a valid trade.

Idempotent: INSERT OR IGNORE on (discord_message_id,
discord_attachment_id), same as the live watcher. Re-running is safe.

Usage:
    # Dry-run for one user (no DB writes):
    /opt/venv/bin/python scripts/backfill_member_trades.py \\
        --user-id 704361827290579084 --dry-run

    # Real run for all members (last 14 days):
    /opt/venv/bin/python scripts/backfill_member_trades.py --days 14
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import db  # noqa: E402
from analyst_log.ocr import (  # noqa: E402
    extract_trade_from_caption,
    extract_trade_from_image,
)
from analyst_log.watcher import _is_extraction_actionable  # noqa: E402
from config import settings  # noqa: E402


def _is_caller(author_id: int, caller_user_ids: set[int]) -> bool:
    return int(author_id) in caller_user_ids


async def _backfill_one(row: dict, dry_run: bool) -> dict:
    """Attempt extraction on a single chat_messages row. Returns a
    result dict {status, ticker, action, ...} for the summary log.

    status: 'wrote' | 'skipped_not_trade' | 'skipped_dedup' | 'skipped_no_content' | 'error'
    """
    msg_id = int(row["discord_message_id"])
    # synthetic attachment_id=0 for caption-only / dedup against the
    # same row's prior member-mode write
    att_id = 0
    if db.analyst_trade_exists(msg_id, att_id):
        return {"status": "skipped_dedup", "msg_id": msg_id}

    caption_parts: list[str] = []
    if row.get("content"):
        caption_parts.append(str(row["content"]).strip())
    if row.get("image_ocr_text"):
        # Prefix the OCR block so the extractor knows it's image-derived
        caption_parts.append(f"[image-OCR]\n{row['image_ocr_text']}")
    caption = "\n\n".join([c for c in caption_parts if c]).strip()
    if not caption:
        return {"status": "skipped_no_content", "msg_id": msg_id}

    try:
        extracted = await extract_trade_from_caption(
            caption,
            parent_caption=None,
            caller_name=row.get("author_display") or row.get("author_username") or "member",
        )
    except Exception as e:
        return {"status": "error", "msg_id": msg_id, "err": str(e)[:200]}

    if extracted is None:
        return {"status": "error", "msg_id": msg_id, "err": "extractor returned None"}

    is_trade = bool(extracted.get("is_trade_screenshot"))
    if is_trade and not _is_extraction_actionable(extracted):
        is_trade = False

    if not is_trade:
        return {
            "status": "skipped_not_trade",
            "msg_id": msg_id,
            "what_it_appears_to_be": extracted.get("what_it_appears_to_be"),
        }

    if dry_run:
        return {
            "status": "would_write",
            "msg_id": msg_id,
            "ticker": extracted.get("ticker"),
            "action": extracted.get("action"),
            "strike": extracted.get("strike"),
            "expiry": extracted.get("expiry"),
            "gain_pct": extracted.get("gain_pct"),
        }

    # Write the analyst_trades row
    try:
        db.record_analyst_trade(
            discord_message_id=msg_id,
            discord_attachment_id=att_id,
            author=row.get("author_display") or row.get("author_username") or "?",
            author_id=int(row["author_id"]),
            posted_at=str(row["posted_at"]),
            image_url=None,
            caption=row.get("content"),
            is_trade=True,
            gemini_json=extracted,
            ticker=extracted.get("ticker"),
            contract_type=extracted.get("contract_type"),
            strike=extracted.get("strike"),
            expiry=extracted.get("expiry"),
            action=extracted.get("action"),
            gain_pct=extracted.get("gain_pct"),
            price=extracted.get("price"),
            caller=None,
            tracking_mode="member",
        )
    except Exception as e:
        return {"status": "error", "msg_id": msg_id, "err": f"DB insert: {str(e)[:200]}"}

    return {
        "status": "wrote",
        "msg_id": msg_id,
        "ticker": extracted.get("ticker"),
        "action": extracted.get("action"),
        "strike": extracted.get("strike"),
        "expiry": extracted.get("expiry"),
        "gain_pct": extracted.get("gain_pct"),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14,
                        help="window (days) — default 14, matches points ledger")
    parser.add_argument("--user-id", type=int,
                        help="optional: backfill only this single user_id")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be written but don't insert")
    parser.add_argument("--max", type=int, default=2000,
                        help="hard cap on candidate rows processed (cost guard)")
    args = parser.parse_args()

    # Candidates: in eager-OCR alert channels, with author_id, recent enough,
    # NOT yet extracted (analyst_trade_exists checked per row).
    eager_channels = sorted(settings.resolve_chat_eager_ocr_channels())
    print(f"Eager-OCR channels ({len(eager_channels)}):")
    for c in eager_channels:
        print(f"  - {c}")

    # Exclude analyst_callers' OWN channels — those are handled in
    # caller-mode by the live watcher already. Member-mode is for
    # everyone else's posts in shared channels.
    caller_channel_names = {
        c["channel"] for c in settings.resolve_analyst_callers()
    }
    member_channels = [c for c in eager_channels if c not in caller_channel_names]
    print(f"\nMember-eligible channels ({len(member_channels)}):")
    for c in member_channels:
        print(f"  - {c}")

    # Build candidate query
    conn = db.get_connection()
    placeholders = ",".join("?" for _ in member_channels)
    user_clause = " AND author_id = ?" if args.user_id else ""
    sql = f"""
        SELECT discord_message_id, channel_name, author_id, author_username,
               author_display, posted_at, content, has_attachments,
               image_ocr_text, image_ocr_status
          FROM chat_messages
         WHERE channel_name IN ({placeholders})
           AND author_id IS NOT NULL
           AND posted_at > datetime('now', ?)
           AND (
                 (content IS NOT NULL AND length(content) > 0)
              OR image_ocr_text IS NOT NULL
           )
           {user_clause}
         ORDER BY posted_at ASC
         LIMIT {args.max}
    """
    params: list = list(member_channels)
    params.append(f"-{args.days} days")
    if args.user_id:
        params.append(int(args.user_id))
    rows = conn.execute(sql, tuple(params)).fetchall()
    rows = [dict(r) for r in rows]
    print(f"\nCandidate rows in last {args.days}d: {len(rows)}")
    if args.user_id:
        print(f"  (filtered to user_id={args.user_id})")
    if args.dry_run:
        print("  DRY RUN — no DB writes will occur")
    print()

    # Process
    counts: dict[str, int] = {}
    wrote_details: list[dict] = []
    for r in rows:
        result = await _backfill_one(r, dry_run=args.dry_run)
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        if result["status"] in ("wrote", "would_write"):
            wrote_details.append({**r, **result})
        if result["status"] == "error":
            print(f"  ERROR msg={result['msg_id']}: {result.get('err')}", flush=True)

    print("=== Summary ===")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")

    if wrote_details:
        print(f"\n=== {'Would-write' if args.dry_run else 'Wrote'} rows ===")
        for d in wrote_details[:30]:
            tag = "[DRY]" if args.dry_run else "[OK]"
            print(
                f"  {tag} {d['posted_at'][:16]} {d['channel_name']} "
                f"uid={d['author_id']} → "
                f"{d.get('action') or '?'} {d.get('ticker') or '?'} "
                f"{d.get('strike') or '?'} exp={d.get('expiry') or '?'} "
                f"gain={d.get('gain_pct') or '?'}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
