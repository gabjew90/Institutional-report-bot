# Trader log overhaul — design

**Date:** 2026-06-02
**Owner:** trader-points + analyst-log subsystems
**Status:** Pending user review

## Background

ZHawk's `🫦-zhawk-thawghts-🗣` channel finally ingested 39 messages after the Discord permission fix, but **all 39 are text-only entry/close calls** ("PURR Leaps 12/18 $14 / $4.10 per contract", "ORCL 12/27 Leaps have officially hit a 3x from entry"). His `analyst_trades` rows are zero because the current pipeline only credits **screenshot-OCR'd structured trade rows**. Text trade narratives don't reach the points ledger; ZHawk has 0 receipt points despite actively calling trades in his own caller channel.

Today's points ladder is also more complex than it needs to be — five different point values for five event types (entry+win, entry+loss, entry+ghost, screenshot_win, screenshot_loss) with separate caller-nerf math layered on top. User wants to collapse it: **wins only, +2 each, 7-day rolling window.**

And the `trader_score` formula's `min(100, ...)` cap collapses BK (62 receipt pts in 14d) and Abe (137) to both display "100" — invisible to the user that Abe has 2× the receipts. Removing the cap makes the score additive (chatter + ledger) and the receipts dimension visible end-to-end.

## Goal

Three coupled changes:
1. Text-based entry/close classification — Gemini reads the message content + image attachments, decides "is this a trade?", extracts whatever fields are present. Writes to `analyst_trades` the same way image OCR does today.
2. Scoring overhaul — wins only at +2 each, 7d rolling, no `min(100, ...)` cap. Formula becomes `trader_score = scaled_chatter + receipt_points` (purely additive).
3. Backfill — re-process the last 30 days of caller-owned + eager-OCR channels through the new text classifier so existing text trade calls get credited retroactively.

## Architecture

```
BEFORE                                    AFTER
─────────                                 ─────────
Message in eager-OCR channel              Message in eager-OCR channel
  │                                         │
  ├─ has image? ──── yes ─→ Gemini          ├─ has image? ──┐
  │                          vision OCR     │               │
  │                          ↓              ├─ has text? ───┼─→ Gemini classifier
  │                  analyst_trades         │               │      (vision + text,
  │                  row (image,            │               │       same model,
  │                   is_trade flag)        │               │       one call per msg)
  │                                         │               ↓
  └─ no image: ignored                      │       analyst_trades row
                                            │       (extraction_source:
                                            │        'image' | 'text' | 'mixed',
                                            │        whatever fields Gemini
                                            │        could pull)
                                            │
                                            └─ neither: ignored

  Points (14d, complex ladder)              Points (7d, wins-only +2)
  +5 entry+win, +3 entry+loss               +2 wins ONLY
  +2 ghost, +2 ss_win, +1 ss_loss           0 everything else
  caller nerf overlay                       no caller nerf needed (already wins-only)

  trader_score = min(100, ...)              trader_score = scaled_chatter + receipts
                                            (no cap — top traders separate)
```

## Components

### Component 1 — Schema: add `extraction_source` column

**Location:** `db.py` schema (table `analyst_trades`).

```sql
ALTER TABLE analyst_trades
  ADD COLUMN extraction_source TEXT;
```

Values: `'image'` (legacy / image-OCR only), `'text'` (text-only classification), `'mixed'` (message had both image + text and Gemini used both). Backfill defaults legacy rows to `'image'` since they all came from image OCR.

Migration runs on boot (same pattern as the other ALTER TABLE migrations already in `db.py`).

### Component 2 — Gemini classifier: text + vision in one call

**Location:** new function `analyst_log/ocr.py:extract_trade_from_message`.

Takes the discord.Message (text content + image attachments), sends ONE Gemini call with both modalities. Prompt asks: *"is this message describing an options/crypto entry or close? If yes, extract whatever fields are clearly present. Skip the message if it's an opinion, meme, or unrelated chatter."*

Strict JSON response:
```json
{
  "is_trade": true | false,
  "action": "open" | "add" | "close" | "trim" | null,
  "ticker": "PURR" | null,
  "contract_type": "call" | "put" | "spot" | null,
  "strike": 14.0 | null,
  "expiry": "2026-12-18" | null,
  "price": 4.10 | null,
  "gain_pct": 200.0 | null,
  "extraction_source": "image" | "text" | "mixed",
  "confidence": 0.0-1.0
}
```

**Fuzzy schema (per user direction):** accept whatever Gemini can pull. A row with just `is_trade=true, ticker="PURR", action="open"` is valid and gets written. The ledger logic handles partial-information rows by matching on whatever's present.

**Hallucination guard:** prompt requires explicit textual / visual evidence for each non-null field. Generic bullish chatter ("I'm long tech") returns `is_trade: false`. Threshold: only write rows where `is_trade=true` AND `confidence >= 0.6`.

### Component 3 — Watcher hook: classify on every eager-OCR / caller-channel message

**Location:** `chat_ingestion/watcher.py:_safe_ocr_inline` (existing eager-OCR task spawner).

Today this function calls `ocr_attachments_inline` only for messages with image attachments. Modify to call `extract_trade_from_message` for **every message in:**
- Caller-owned channels (`🫦-zhawk-thawghts-🗣`, `🦉-kloh-alerts-🦉`, `🥷🏽-abe-alerts-🥷🏽`, `💅🏾-kyle-alerts-💅🏾`)
- Eager-OCR alert channels (already configured in `chat_eager_ocr_channels`)

Even text-only messages get a classification call. Most return `is_trade: false` and no row is written — cheap fast path on Gemini's side (no image to process).

Reuses the existing `_eager_ocr_semaphore` from Batch 2 so concurrency stays capped at 3 in-flight calls.

### Component 4 — Dedup: skip text rows that duplicate image rows

**Location:** inside `extract_trade_from_message` writer, before INSERT.

If a row already exists with `(author_id, ticker, expiry, strike, contract_type, action)` matching and `posted_at` within ±5 min, skip the text-extracted row. Image OCR is higher fidelity (verified screenshot) so the image row wins on conflict. Dedup applies the other direction too: if a text row exists and the image arrives later, the image row supersedes — delete the text row, write the image row.

### Component 5 — Points ledger: wins-only +2, 7d window

**Location:** `db.py:compute_member_points`.

Constants change:
- `entries_won`: +2 (was +5)
- `entries_lost`: 0 (was +3 for members, 0 for callers)
- `entries_ghosted`: 0 (was +2 for members, 0 for callers)
- `screenshot_wins`: +2 (was +2, no change)
- `screenshot_losses`: 0 (was +1 for members, 0 for callers)
- `entries_pending`: 0 (unchanged)

Caller-vs-member distinction is **no longer needed** for the points math (all loss buckets are 0 for everyone) — but the function still returns `is_official_caller` in the response for the profile builder's prompt rendering.

Default window: `days=14` → `days=7`. Same arg is plumbed through to the profile builder's call.

### Component 6 — Score formula: drop the cap

**Location:** `scripts/backfill_user_profiles.py:1310`-ish (the `min(100, scaled_chatter + receipt_points)` line).

Change to:
```python
scaled_chatter = min(50, chatter_base) * min(1, msg_count / 500)
trader_score = scaled_chatter + receipt_points  # No upper bound.
```

Profile-builder prompt updates accordingly — the rubric text needs the cap-removal explanation. The cap was the headline reason BK and Abe both display "100" today; removing it lets the score reflect the actual receipts gap.

### Component 7 — Backfill: re-classify last 30d of caller + eager-OCR channel messages

**Location:** new `scripts/backfill_text_extracted_trades.py`.

One-shot script. For each caller-owned + eager-OCR channel:
1. Read all messages from the last 30 days (`chat_messages` table).
2. For each message, run `extract_trade_from_message`.
3. Apply dedup (existing image-OCR rows take precedence).
4. Write text-extracted rows.

Run rate-limited (same semaphore as live ingestion) so it doesn't burn through the Gemini quota on a single batch run. Estimated cost: ~30 days × ~50 messages/day in those channels × $0.0005 = ~$0.75 one-time.

After backfill completes, kick off a `user_profile_refresh_job` for affected users so their `trader_score` reflects the new receipt points immediately.

### Component 8 — Profile-builder prompt updates

**Location:** `scripts/backfill_user_profiles.py` (prompt text).

Update mentions of:
- "14-DAY POINTS LEDGER" → "7-DAY POINTS LEDGER"
- Point values in the receipts rubric (+5/+3/+2/+1 → all wins-only at +2)
- `min(100, ...)` cap explanation removed
- Caller-nerf language simplified (no longer needed; wins-only applies to everyone)

## Data flow (worked example for ZHawk)

ZHawk posts in `🫦-zhawk-thawghts-🗣`:
> "PURR Leaps 12/18 $14 / $4.10 per contract"

1. `chat_ingestion.watcher` ingests the message into `chat_messages` (as today).
2. `_safe_ocr_inline` fires for the message (zhawk-thawghts is eager-OCR channel).
3. `extract_trade_from_message` calls Gemini with the text content + zero attachments.
4. Gemini responds: `{is_trade: true, action: "open", ticker: "PURR", contract_type: "call", strike: 14, expiry: "2026-12-18", price: 4.10, extraction_source: "text", confidence: 0.92}`.
5. Dedup check: no prior row for (ZHawk, PURR, 2026-12-18, 14, call, open) within ±5 min → write.
6. Row appears in `analyst_trades` with `extraction_source='text'`, `author_id=ZHawk`, `is_trade=1`.

Later ZHawk posts:
> "PURR closed for +200%"

Gemini extracts: `{is_trade: true, action: "close", ticker: "PURR", gain_pct: 200, ...}`. Same dedup applies. Row gets written. Existing pair-stitching logic (in the daily expire-sweep cron) matches the close to the open by `(ticker, expiry, strike, contract_type)` → tally counts ZHawk as +1 win → his 7d ledger gains +2 points → his `trader_score` rises by 2.

## Error handling

| Failure | Handling |
|---|---|
| Gemini returns malformed JSON | Skip the row, log warning, continue with next message. No retry — text classification is cheap; missed rows can be re-classified on backfill. |
| Gemini says `is_trade=true` but no ticker | Skip the row. A trade without a ticker is unmatched/unusable for stitching. |
| Confidence < 0.6 | Skip the row. Threshold tunable via env var `TEXT_EXTRACTION_MIN_CONFIDENCE`. |
| Dedup collision (image row exists) | Skip the text row silently. Image wins. |
| Backfill mid-script crash | Resume-able via `processing_log` checkpoint: backfill records last-processed discord_message_id per channel, resumes from there on next run. |
| Token budget hit (`BudgetExceeded`) | Stop the backfill, log clearly, prompt for `--continue` flag with `--reset-budget`. Live classification path raises BudgetExceeded → user sees fewer trade rows that day until budget rolls over. |

## Testing

Six new smoke test files:

1. **`scripts/smoke_extract_trade_from_message.py`** — stub Gemini, verify the prompt construction + JSON parsing + fuzzy-schema acceptance + confidence threshold + ticker-required guard.
2. **`scripts/smoke_text_extraction_dedup.py`** — verify the ±5-min same-fields dedup rule both directions (text-then-image, image-then-text).
3. **`scripts/smoke_points_ledger_wins_only.py`** — verify `compute_member_points` returns wins × 2 only; all loss buckets contribute 0; window defaults to 7d.
4. **`scripts/smoke_score_no_cap.py`** — verify the profile builder's score = `scaled_chatter + receipt_points` with no upper bound. Pass scaled_chatter=50 + receipts=120 → expect 170 (not 100).
5. **`scripts/smoke_extraction_source_column.py`** — verify the migration adds the column and legacy rows are backfilled to `'image'`.
6. **`scripts/smoke_backfill_resumable.py`** — verify the backfill script's checkpoint resume logic.

End-to-end manual after deploy:
- Confirm a fresh text-only trade post in `🫦-zhawk-thawghts-🗣` produces an `analyst_trades` row with `extraction_source='text'` within ~10 seconds.
- Confirm ZHawk's `trader_score` rises after the backfill completes.
- Spot-check 10 random text-extracted rows against the source message — verify Gemini's extraction is faithful (not hallucinating trades from opinion posts).

## Shipping

Single-PR rollout. Same approach as the /ask refactor:
- All commits go to `claude/financial-pdf-discord-bot-mDpbk`.
- `git push` deferred to the final task so production stays on current code through all intermediate commits.
- After push, watch Railway logs for ~10 min to confirm no boot errors, then trigger the backfill.

## Out of scope

- Stitching text-extracted closes to image-extracted opens (the daily expire-sweep cron already does cross-row matching by `(ticker, expiry, strike, contract_type)` — modality-agnostic by construction).
- Crypto perp trades with leverage / liquidation prices (Gemini extracts whatever's there; the points ladder treats them like options for now).
- Web UI for QC reviewing text-extracted rows.
- Auto-tuning `TEXT_EXTRACTION_MIN_CONFIDENCE` based on hit-rate.
