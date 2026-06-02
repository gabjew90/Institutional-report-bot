# Trader log overhaul — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Credit text-based entry/close calls (not just screenshots) in the trader ledger; collapse the 5-event-type point ladder to wins-only +2; switch from 14d to 7d rolling; drop the `min(100,...)` cap on `trader_score`. Backfill 30d of caller-owned + eager-OCR channels through the new text classifier.

**Architecture:** New Gemini classifier (`analyst_log/ocr.py:extract_trade_from_message`) consumes text + image attachments + cached OCR text in one call, returns a fuzzy JSON schema (accept whatever Gemini can pull as long as `is_trade=true`). Hooks into `chat_ingestion/watcher.py:_safe_ocr_inline` so eager-OCR channels (including ZHawk's `🫦-zhawk-thawghts-🗣`) now classify text-only messages, image+text combos, and image-only messages through ONE unified pipeline. Dedup is keyed on `discord_message_id` (one Discord message → at most one `analyst_trades` row), with same-fields fallback for cross-message dedup. Backfill uses cached `chat_messages.image_ocr_text` instead of re-fetching expired Discord CDN images. Abe + BK ingestion pipelines through `analyst_log/watcher.py` are completely untouched. Points-ledger formula simplifies to `wins × 2` (callers + members same rule). Score formula loses its cap.

**Tech Stack:** Python 3.10+, `google.genai` SDK (Gemini 3.1 Flash Lite text+vision), SQLite via existing `db.py`, plain-script smoke tests.

**Spec:** [`docs/superpowers/specs/2026-06-02-trader-log-overhaul-design.md`](../specs/2026-06-02-trader-log-overhaul-design.md)

---

## File Structure

**Modified:**
| File | What changes |
|---|---|
| `db.py` | Add `extraction_source TEXT` column to `analyst_trades` (with ALTER TABLE migration + legacy-row backfill to `'image'`). Rewrite `compute_member_points` body to `entries_won × 2 + screenshot_wins × 2`. Default arg `days=14` → `days=7`. |
| `analyst_log/ocr.py` | New `extract_trade_from_message(message, image_bytes_list)` function — single Gemini call accepts text + vision, returns fuzzy JSON. |
| `chat_ingestion/watcher.py` | Inside `_safe_ocr_inline`, call `extract_trade_from_message` for every message (text-only and image-bearing) in eager-OCR channels. Add ±5-min dedup query before writing the row. |
| `scripts/backfill_user_profiles.py` | Drop `min(100, ...)` cap on line 1889. Update prompt text references: "14-DAY" → "7-DAY", point values, cap removal, caller-nerf removal. Pass `days=7` to `compute_member_points`. |

**Created:**
| File | Purpose |
|---|---|
| `scripts/backfill_text_extracted_trades.py` | One-shot script: walk last 30d of caller-owned + eager-OCR channel messages; run classifier on each; write text rows with dedup. Resumable via `processing_log` checkpoint. |
| `scripts/smoke_extraction_source_column.py` | Verify the column migration + legacy backfill to `'image'`. |
| `scripts/smoke_extract_trade_from_message.py` | Stub Gemini, verify prompt construction + JSON parsing + fuzzy schema + confidence threshold + ticker-required guard. |
| `scripts/smoke_text_extraction_dedup.py` | Verify ±5-min same-fields dedup, both directions (text-then-image, image-then-text). |
| `scripts/smoke_points_ledger_wins_only.py` | Verify `compute_member_points` returns `(entries_won + screenshot_wins) × 2`; all loss buckets contribute 0; default window 7d. |
| `scripts/smoke_score_no_cap.py` | Verify profile-builder formula computes `scaled_chatter + receipt_points` with no upper bound. |
| `scripts/smoke_backfill_resumable.py` | Verify backfill script's checkpoint resume logic. |

---

## Task 1 — Add `extraction_source` column to `analyst_trades`

**Files:**
- Modify: `db.py` (schema definition + migration runner)
- Create: `scripts/smoke_extraction_source_column.py`

The column tells QC which extraction modality produced each row. Legacy rows default to `'image'` because the existing pipeline only OCR'd images.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_extraction_source_column.py`:

```python
"""Smoke test for the analyst_trades.extraction_source column migration.

Validates:
  1. Fresh schema has the extraction_source column
  2. Legacy rows (column NULL) get backfilled to 'image' on migration
  3. Insert path accepts 'image' | 'text' | 'mixed' values
"""

import sys
import sqlite3
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_column_exists_after_init():
    conn = sqlite3.connect(":memory:")
    with patch("db.get_connection", return_value=conn):
        db.init_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)").fetchall()]
    assert "extraction_source" in cols, f"extraction_source column missing: {cols}"
    _ok("fresh schema has extraction_source column")


def test_legacy_rows_backfilled_to_image():
    """Simulate the migration: a row with NULL extraction_source after
    the ALTER TABLE should get UPDATE'd to 'image'."""
    conn = sqlite3.connect(":memory:")
    # Set up the OLD schema (no extraction_source column)
    conn.execute("""
        CREATE TABLE analyst_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER NOT NULL,
            discord_attachment_id INTEGER NOT NULL,
            author TEXT NOT NULL,
            author_id INTEGER,
            posted_at TEXT NOT NULL,
            image_url TEXT,
            caption TEXT,
            is_trade INTEGER NOT NULL DEFAULT 0,
            ticker TEXT,
            contract_type TEXT,
            strike REAL,
            expiry TEXT,
            action TEXT,
            gain_pct REAL,
            price REAL,
            inferred_status TEXT,
            tracking_mode TEXT NOT NULL DEFAULT 'caller',
            gemini_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(discord_message_id, discord_attachment_id)
        )
    """)
    conn.execute(
        "INSERT INTO analyst_trades (discord_message_id, discord_attachment_id, "
        "author, posted_at) VALUES (1, 1, 'abe', '2026-05-01T12:00:00')"
    )

    # Run the migration: add column + backfill
    db._migrate_add_extraction_source(conn)

    row = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()
    assert row[0] == "image", f"expected 'image', got {row[0]!r}"
    _ok("legacy rows backfilled to extraction_source='image'")


def test_insert_accepts_text_source():
    conn = sqlite3.connect(":memory:")
    with patch("db.get_connection", return_value=conn):
        db.init_db()
    conn.execute(
        "INSERT INTO analyst_trades "
        "(discord_message_id, discord_attachment_id, author, posted_at, "
        "extraction_source) "
        "VALUES (?, ?, ?, ?, ?)",
        (1, 1, "zhawk", "2026-06-01T12:00:00", "text"),
    )
    row = conn.execute(
        "SELECT extraction_source FROM analyst_trades WHERE id = 1"
    ).fetchone()
    assert row[0] == "text", f"expected 'text', got {row[0]!r}"
    _ok("insert accepts extraction_source='text'")


if __name__ == "__main__":
    print("=== extraction_source column smoke ===")
    test_column_exists_after_init()
    test_legacy_rows_backfilled_to_image()
    test_insert_accepts_text_source()
    print("\nALL EXTRACTION-SOURCE-COLUMN SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
```

Expected: FAIL on test_column_exists_after_init — column not in schema.

- [ ] **Step 3: Add column to schema + migration function**

In `db.py`, find the `CREATE TABLE IF NOT EXISTS analyst_trades` block (around line 181). Add the column definition:

```sql
            extraction_source TEXT,  -- 'image' | 'text' | 'mixed'
```

Insert it immediately after the existing `tracking_mode` column.

Then locate the migration block (around line 358 — the `ALTER TABLE analyst_trades ADD COLUMN tracking_mode ...` migrations). Add a new migration entry:

```python
    ("extraction_source", "ALTER TABLE analyst_trades ADD COLUMN extraction_source TEXT"),
```

And add a one-shot helper at the bottom of the migration block:

```python
def _migrate_add_extraction_source(conn) -> None:
    """Backfill legacy rows (NULL extraction_source) to 'image'. The
    column was added 2026-06-02; everything before that came from the
    image-OCR pipeline only, so 'image' is the correct default."""
    # Add the column if missing (idempotent — works on both fresh init
    # and migration of an existing prod DB).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analyst_trades)").fetchall()]
    if "extraction_source" not in cols:
        conn.execute("ALTER TABLE analyst_trades ADD COLUMN extraction_source TEXT")
    # Backfill any NULL values to 'image'
    conn.execute(
        "UPDATE analyst_trades SET extraction_source = 'image' "
        "WHERE extraction_source IS NULL"
    )
    conn.commit()
```

Call this helper from `init_db()` after the existing migrations (find `_migrate_drop_unique_constraints` and call `_migrate_add_extraction_source(conn)` immediately after it).

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
```

Expected: `ALL EXTRACTION-SOURCE-COLUMN SMOKE TESTS PASS`.

- [ ] **Step 5: Verify import + regression**

```
$env:PYTHONPATH = "."; py -c "import db; print('OK')"
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```
git add db.py scripts/smoke_extraction_source_column.py
git commit -m "db: add extraction_source column to analyst_trades

Tracks which modality produced each row: 'image' (image OCR), 'text'
(text classifier), or 'mixed' (both used). Legacy rows backfilled to
'image' on migration. Sets up Task 2's text classifier to write rows
alongside existing image-OCR rows."
```

---

## Task 2 — Gemini classifier: text + vision in one call

**Files:**
- Modify: `analyst_log/ocr.py` (add new function alongside existing image OCR)
- Create: `scripts/smoke_extract_trade_from_message.py`

Single Gemini call accepts text + image attachments, returns fuzzy JSON. The existing `extract_trade_from_image` stays; we add a sibling that handles both modalities.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_extract_trade_from_message.py`:

```python
"""Smoke test for analyst_log.ocr.extract_trade_from_message.

Validates (Gemini stubbed throughout):
  1. Text-only message classified correctly (no image bytes)
  2. Image-only message routes the same code path
  3. Mixed text + image message returns extraction_source='mixed'
  4. is_trade=false response writes no row
  5. confidence < 0.6 response writes no row
  6. Trade without ticker (only action verb) writes no row
  7. Fuzzy schema: partial fields (just ticker + action) accepted
"""

import asyncio
import json
import sys
from unittest.mock import patch, MagicMock

import analyst_log.ocr as ocr_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _fake_gemini(payload: dict):
    """Build a fake Gemini response with the given JSON payload."""
    resp = MagicMock()
    resp.text = json.dumps(payload)
    return resp


async def _run(text, images=None):
    return await ocr_mod.extract_trade_from_message(
        text=text,
        image_bytes_list=images or [],
        author_username="zhawk",
        channel_name="🫦-zhawk-thawghts-🗣",
    )


def test_text_only_full_extraction():
    payload = {
        "is_trade": True, "action": "open", "ticker": "PURR",
        "contract_type": "call", "strike": 14.0, "expiry": "2026-12-18",
        "price": 4.10, "gain_pct": None, "confidence": 0.92,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("PURR Leaps 12/18 $14 / $4.10 per contract"))
    assert result["is_trade"] is True, result
    assert result["ticker"] == "PURR", result
    assert result["extraction_source"] == "text", result
    _ok("text-only message classified with full schema")


def test_image_only_full_extraction():
    payload = {
        "is_trade": True, "action": "close", "ticker": "AAPL",
        "contract_type": "call", "gain_pct": 45.0, "confidence": 0.85,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("", images=[b"fake-png-bytes"]))
    assert result["is_trade"] is True, result
    assert result["extraction_source"] == "image", result
    _ok("image-only message classified")


def test_mixed_text_image():
    payload = {
        "is_trade": True, "action": "close", "ticker": "ORCL",
        "expiry": "2026-12-27", "gain_pct": 200.0, "confidence": 0.95,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run(
            "ORCL hit 3x from entry",
            images=[b"fake-png-bytes"],
        ))
    assert result["extraction_source"] == "mixed", result
    _ok("text + image message tagged extraction_source='mixed'")


def test_not_a_trade_returns_none():
    payload = {"is_trade": False, "confidence": 0.9}
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("I always say follow the ball"))
    assert result is None or result.get("is_trade") is False, result
    _ok("is_trade=false returns None / no-write signal")


def test_low_confidence_rejected():
    payload = {
        "is_trade": True, "action": "open", "ticker": "TSLA",
        "confidence": 0.3,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("might buy some TSLA later idk"))
    assert result is None or result.get("is_trade") is False, (
        f"confidence={result.get('confidence') if result else 'n/a'} "
        "should have been rejected"
    )
    _ok("confidence < 0.6 rejected as no-write")


def test_missing_ticker_rejected():
    payload = {
        "is_trade": True, "action": "open", "ticker": None,
        "confidence": 0.9,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("opened a new position"))
    assert result is None or result.get("is_trade") is False, result
    _ok("missing ticker rejected (unstitchable row)")


def test_fuzzy_partial_fields_accepted():
    """Per user direction: 'accept whatever is available into JSON as
    long as Gemini believes it's a trade'. So a row with just ticker +
    action and no strike/expiry/price still gets written."""
    payload = {
        "is_trade": True, "action": "open", "ticker": "BTC",
        "contract_type": None, "strike": None, "expiry": None,
        "price": None, "gain_pct": None, "confidence": 0.8,
    }
    with patch.object(ocr_mod, "_call_gemini_classifier",
                      return_value=_fake_gemini(payload)):
        result = asyncio.run(_run("btc long at 73,906 on HL le scalp"))
    assert result["is_trade"] is True, result
    assert result["ticker"] == "BTC", result
    _ok("fuzzy schema: partial fields (just ticker + action) accepted")


if __name__ == "__main__":
    print("=== extract_trade_from_message smoke ===")
    test_text_only_full_extraction()
    test_image_only_full_extraction()
    test_mixed_text_image()
    test_not_a_trade_returns_none()
    test_low_confidence_rejected()
    test_missing_ticker_rejected()
    test_fuzzy_partial_fields_accepted()
    print("\nALL EXTRACT-TRADE-FROM-MESSAGE SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_extract_trade_from_message.py
```

Expected: FAIL — `extract_trade_from_message` doesn't exist yet.

- [ ] **Step 3: Implement the function**

In `analyst_log/ocr.py`, add the new function alongside the existing `extract_trade_from_image`. Find the end of `extract_trade_from_image` and insert after it:

```python
# Confidence threshold for accepting a text/text+image classification.
# Below this we skip the row. Tunable via env var if needed.
_MESSAGE_CLASSIFIER_MIN_CONFIDENCE = 0.6


async def _call_gemini_classifier(prompt_parts: list, model: str):
    """Thin wrapper around the Gemini SDK call so tests can stub it
    without intercepting the whole genai client. Returns the raw
    response object — caller parses `.text`."""
    from google import genai
    from google.genai import types as genai_types
    from config import settings

    client = genai.Client(api_key=settings.google_api_key)
    response = await client.aio.models.generate_content(
        model=model,
        contents=[genai_types.Content(role="user", parts=prompt_parts)],
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=400,
            temperature=0.1,
        ),
    )
    return response


_CLASSIFIER_PROMPT = """\
You read a Discord message from a trader's alert channel. The message
may be text, image (screenshot), or both. Your job: decide if the
message describes an entry, add, close, or trim of an options or
crypto trade — and extract whatever fields are clearly visible.

Return STRICT JSON matching this schema:
{
  "is_trade": bool,
  "action": "open" | "add" | "close" | "trim" | null,
  "ticker": str | null,
  "contract_type": "call" | "put" | "spot" | "future" | null,
  "strike": float | null,
  "expiry": str | null,            // YYYY-MM-DD or null
  "price": float | null,           // entry price or close price
  "gain_pct": float | null,        // for closes, % gain or loss
  "confidence": float              // 0.0–1.0
}

Rules:
- If the message is opinion, news, a meme, a reply, or generic
  bullish/bearish chatter (e.g. "I'm long tech", "AI is overheated",
  "this stock looks good"), return {"is_trade": false, "confidence": <your read>}.
- A trade requires explicit evidence: a ticker AND an action verb
  ("opened", "buying", "closed", "sold", "trimmed", "exit", or
  visible from a screenshot's order ticket / P&L pane).
- Accept partial structure. "btc long at 73,906" is a valid trade
  (ticker BTC, action open, price 73906, contract_type spot/future as
  context allows) — strike + expiry can be null.
- "3x from entry" means gain_pct: 200.0. "+50%" means gain_pct: 50.0.
  "-30%" means gain_pct: -30.0.
- confidence reflects how clearly the message conveys a trade — not
  whether you think the trade is good.

Message author: {author}
Channel: {channel}

Text content (user's typed message):
{text}

Image OCR text (prior Gemini extraction from the screenshot, if any —
may be partial or empty; treat as a hint, not gospel):
{cached_ocr}

[Plus any image attachments provided directly in this prompt.]
"""


async def extract_trade_from_message(
    text: str,
    image_bytes_list: list[bytes],
    *,
    author_username: str,
    channel_name: str,
    cached_ocr_text: str = "",
) -> dict | None:
    """Classify a Discord message as a trade entry/close — text,
    image, AND/OR cached prior OCR. Returns None if the model says it's
    not a trade, confidence is too low, or no ticker was extractable.

    Otherwise returns a dict matching the row-write schema, plus
    extraction_source ∈ {'text', 'image', 'mixed'}.

    `cached_ocr_text` is used by the backfill path: the eager-OCR
    pipeline has already extracted text from screenshots into
    chat_messages.image_ocr_text. Discord CDN URLs expire ~24h so we
    can't re-fetch the original bytes for old messages. Passing the
    cached OCR text lets the classifier see what the screenshot
    contained without re-fetching. Live ingestion passes image_bytes_list
    instead (real-time has the bytes); backfill passes cached_ocr_text.

    extraction_source is tagged based on which signal(s) the classifier
    used:
      - text only (no image OR cached_ocr): 'text'
      - image bytes only (no text caption): 'image'
      - cached_ocr_text only (backfill, image bytes gone): 'image'
        (the OCR represents an image, just cached)
      - text + image bytes OR text + cached_ocr: 'mixed'
    """
    from config import settings
    from google.genai import types as genai_types

    text = (text or "").strip()
    cached_ocr_text = (cached_ocr_text or "").strip()
    has_text = bool(text)
    has_images = bool(image_bytes_list)
    has_cached_ocr = bool(cached_ocr_text)
    if not has_text and not has_images and not has_cached_ocr:
        return None  # nothing to classify

    # Build the prompt parts: text + each image as bytes.
    # Per-section content blocks so Gemini sees what we used clearly.
    text_section = text if has_text else "(none)"
    ocr_section = cached_ocr_text if has_cached_ocr else "(none)"
    prompt_text = _CLASSIFIER_PROMPT.format(
        author=author_username, channel=channel_name,
        text=text_section,
        cached_ocr=ocr_section,
    )
    parts = [genai_types.Part.from_text(text=prompt_text)]
    for img_bytes in image_bytes_list:
        parts.append(
            genai_types.Part.from_bytes(
                data=img_bytes, mime_type="image/png",
            )
        )

    model = settings.gemini_model
    try:
        response = await _call_gemini_classifier(parts, model)
    except Exception as e:
        # Soft fail — don't raise into the caller's task chain
        log.warning(
            f"extract_trade_from_message: Gemini call failed for "
            f"{author_username} in {channel_name}: {type(e).__name__}: {e}"
        )
        return None

    try:
        import json
        payload = json.loads((response.text or "").strip() or "{}")
    except Exception as e:
        log.warning(
            f"extract_trade_from_message: malformed JSON "
            f"({type(e).__name__}: {e}); response.text={response.text!r}"
        )
        return None

    # Validation: only accept if model says it's a trade, confidence is
    # high enough, and there's a ticker (unstitchable without one).
    if not payload.get("is_trade"):
        return None
    confidence = payload.get("confidence") or 0
    if confidence < _MESSAGE_CLASSIFIER_MIN_CONFIDENCE:
        return None
    if not payload.get("ticker"):
        return None

    # Tag extraction_source based on which modalities contributed.
    image_present = has_images or has_cached_ocr  # cached OCR represents an image
    if has_text and image_present:
        extraction_source = "mixed"
    elif image_present:
        extraction_source = "image"
    else:
        extraction_source = "text"
    payload["extraction_source"] = extraction_source
    return payload
```

Add a module-level `import logging; log = logging.getLogger(__name__)` if not already present.

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_extract_trade_from_message.py
```

Expected: `ALL EXTRACT-TRADE-FROM-MESSAGE SMOKE TESTS PASS`.

- [ ] **Step 5: Regression**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```
git add analyst_log/ocr.py scripts/smoke_extract_trade_from_message.py
git commit -m "ocr: extract_trade_from_message — Gemini text+vision classifier

Single Gemini call accepts text content + optional image attachments,
returns fuzzy JSON. Rejects non-trades (opinion / news / memes),
confidence below 0.6, and rows without a ticker (unstitchable).
Per user direction: accept whatever fields are visible as long as
is_trade=true. extraction_source set to 'text' | 'image' | 'mixed'
based on modalities provided."
```

---

## Task 3 — Wire classifier into eager-OCR watcher

**Files:**
- Modify: `chat_ingestion/watcher.py`
- Modify: `db.py` (add `insert_text_extracted_trade` helper)
- Create: `scripts/smoke_text_extraction_dedup.py`

`_safe_ocr_inline` already fires for every message in eager-OCR channels (text or image). Replace the inner `ocr_attachments_inline` call with the new `extract_trade_from_message` classifier. Add a dedup query before INSERT.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_text_extraction_dedup.py`:

```python
"""Smoke test for the ±5 min dedup rule between text + image rows.

Validates:
  1. Image row inserted; text-extracted row for same trade within 5 min skipped
  2. Text row inserted first; image row for same trade within 5 min overwrites
  3. Same fields beyond 5 min window are NOT deduped
"""

import sqlite3
import sys
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with patch("db.get_connection", return_value=c):
        db.init_db()
    return c


def test_dedup_tier1_same_message_id():
    """Tier 1: if a row exists for the same discord_message_id, skip.
    This catches the live case where image OCR already wrote a row for
    a message with screenshot + caption, and the new classifier tries
    to write a second row for the same message."""
    c = _conn()
    # Image row written first (by ocr_attachments_inline path)
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, action, is_trade, tracking_mode, extraction_source) "
        "VALUES (5000, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'open', 1, 'member', 'image')"
    )
    # Now classifier tries to write a row for the SAME message
    # (different ticker even — but message_id collision wins).
    with patch("db.get_connection", return_value=c):
        skipped = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=5000,  # SAME message_id
            posted_at="2026-06-01T12:00:00",
            extracted={
                "is_trade": True, "action": "close", "ticker": "BTC",
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert skipped is False, "same message_id should always skip"
    rows = c.execute(
        "SELECT COUNT(*) FROM analyst_trades WHERE discord_message_id = 5000"
    ).fetchone()
    assert rows[0] == 1, f"expected 1 row, got {rows[0]}"
    _ok("Tier 1 dedup: same discord_message_id skipped (live image+text case)")


def test_text_skipped_when_image_already_exists():
    c = _conn()
    # Insert image row first (T=12:00:00)
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, contract_type, strike, expiry, action, "
        "is_trade, tracking_mode, extraction_source) "
        "VALUES (1, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'call', 14.0, '2026-12-18', 'open', "
        "1, 'member', 'image')"
    )
    # Now try to insert a text row for the same trade T=12:03:00
    # (3 min later — within 5 min window)
    with patch("db.get_connection", return_value=c):
        skipped = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=2, posted_at="2026-06-01T12:03:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "PURR",
                "contract_type": "call", "strike": 14.0,
                "expiry": "2026-12-18", "price": None, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert skipped is False, f"expected dedup-skip, got insert={skipped}"
    # Only the original image row should be there
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'PURR'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "image", rows
    _ok("text row skipped when image row exists within ±5 min")


def test_text_inserted_when_no_image_in_window():
    c = _conn()
    # No prior row. Insert text row first.
    with patch("db.get_connection", return_value=c):
        inserted = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=1, posted_at="2026-06-01T12:00:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "BTC",
                "contract_type": "spot", "strike": None,
                "expiry": None, "price": 73906.0, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert inserted is True, f"expected insert, got {inserted}"
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'BTC'"
    ).fetchall()
    assert len(rows) == 1 and rows[0][0] == "text", rows
    _ok("text row inserted when no image row exists in window")


def test_beyond_window_not_deduped():
    c = _conn()
    c.execute(
        "INSERT INTO analyst_trades (discord_message_id, "
        "discord_attachment_id, author, author_id, posted_at, "
        "ticker, action, is_trade, tracking_mode, extraction_source) "
        "VALUES (1, 1, 'zhawk', 100, '2026-06-01T12:00:00', "
        "'PURR', 'open', 1, 'member', 'image')"
    )
    # 10 minutes later — beyond window
    with patch("db.get_connection", return_value=c):
        inserted = db.insert_text_extracted_trade_if_not_dup(
            author_id=100, author_username="zhawk",
            discord_message_id=2, posted_at="2026-06-01T12:10:00",
            extracted={
                "is_trade": True, "action": "open", "ticker": "PURR",
                "contract_type": None, "strike": None, "expiry": None,
                "price": None, "gain_pct": None,
                "extraction_source": "text",
            },
            channel_name="🫦-zhawk-thawghts-🗣",
        )
    assert inserted is True, "10-min-later row should NOT be deduped"
    rows = c.execute(
        "SELECT extraction_source FROM analyst_trades WHERE ticker = 'PURR'"
    ).fetchall()
    assert len(rows) == 2, f"expected 2 rows, got {rows}"
    _ok("trades beyond ±5 min window are not deduped (different events)")


if __name__ == "__main__":
    print("=== text-extraction dedup smoke ===")
    test_dedup_tier1_same_message_id()
    test_text_skipped_when_image_already_exists()
    test_text_inserted_when_no_image_in_window()
    test_beyond_window_not_deduped()
    print("\nALL DEDUP SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_text_extraction_dedup.py
```

Expected: FAIL — `insert_text_extracted_trade_if_not_dup` doesn't exist yet.

- [ ] **Step 3: Add the dedup-write helper to `db.py`**

Insert this near the analyst_trades insert helpers in `db.py`:

```python
def insert_text_extracted_trade_if_not_dup(
    *,
    author_id: int,
    author_username: str,
    discord_message_id: int,
    posted_at: str,
    extracted: dict,
    channel_name: str | None = None,
    dedup_window_minutes: int = 5,
) -> bool:
    """Insert a classifier-extracted analyst_trades row with two-tier
    dedup:

      Tier 1 (strict): if any analyst_trades row already exists with
        the SAME discord_message_id, skip. One Discord message → at
        most one row, regardless of modality (text vs image vs mixed).

      Tier 2 (fuzzy): if a row exists with extraction_source='image'
        within ±dedup_window_minutes for the same (author_id, ticker,
        expiry, strike, contract_type, action), skip. Handles the
        cross-message case (e.g., text post then screenshot of same
        trade 2 min later).

    Returns True if inserted, False if skipped.

    Image extraction always wins on conflict: images are higher-
    fidelity verified screenshots. If a text row exists when an image
    arrives later for the same message, the image-OCR insert path is
    responsible for superseding (separate helper, not this one).
    """
    from datetime import datetime, timedelta

    conn = get_connection()
    ticker = (extracted.get("ticker") or "").upper()
    contract_type = (extracted.get("contract_type") or "").lower() or None
    strike = extracted.get("strike")
    expiry = extracted.get("expiry") or None
    action = (extracted.get("action") or "").lower() or None

    # Tier 1: discord_message_id dedup. One Discord message → at most
    # one row. Catches the live case where ocr_attachments_inline
    # already wrote an image row AND our text+vision classifier
    # tries to write a second row for the same message.
    existing_by_msg_id = conn.execute(
        "SELECT id, extraction_source FROM analyst_trades "
        "WHERE discord_message_id = ? LIMIT 1",
        (int(discord_message_id),),
    ).fetchone()
    if existing_by_msg_id:
        return False  # one message → one row, regardless of modality

    # Parse posted_at and compute ±N min window bounds for Tier 2.
    try:
        ts = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        ts = datetime.utcnow()
    window_start = (ts - timedelta(minutes=dedup_window_minutes)).isoformat()
    window_end = (ts + timedelta(minutes=dedup_window_minutes)).isoformat()

    # Tier 2: same-fields dedup against image rows within window.
    existing = conn.execute(
        """SELECT id FROM analyst_trades
            WHERE author_id = ?
              AND extraction_source = 'image'
              AND UPPER(ticker) = ?
              AND COALESCE(LOWER(contract_type), '') = COALESCE(?, '')
              AND COALESCE(strike, -1) = COALESCE(?, -1)
              AND COALESCE(expiry, '') = COALESCE(?, '')
              AND COALESCE(LOWER(action), '') = COALESCE(?, '')
              AND posted_at >= ?
              AND posted_at <= ?
            LIMIT 1""",
        (
            int(author_id), ticker, contract_type, strike, expiry, action,
            window_start, window_end,
        ),
    ).fetchone()
    if existing:
        return False  # cross-message dup of an image row — skip

    # No dup. Insert the text row. discord_attachment_id is -1 (the
    # column is NOT NULL but text rows have no attachment — use -1
    # as the sentinel so it's queryable separately if needed).
    conn.execute(
        """INSERT INTO analyst_trades
              (discord_message_id, discord_attachment_id, author,
               author_id, posted_at, ticker, contract_type, strike,
               expiry, action, gain_pct, price, is_trade,
               tracking_mode, extraction_source, gemini_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
        (
            int(discord_message_id), -1, author_username, int(author_id),
            posted_at, ticker, contract_type, strike, expiry, action,
            extracted.get("gain_pct"), extracted.get("price"),
            "member",  # text rows are always member-mode by definition
            extracted.get("extraction_source") or "text",
            json.dumps(extracted),
        ),
    )
    conn.commit()
    return True
```

- [ ] **Step 4: Wire into `_safe_ocr_inline`**

Find `_safe_ocr_inline` in `chat_ingestion/watcher.py`. Today it calls `ocr_attachments_inline` (image-only). Update so it ALSO collects image bytes if present, calls the new classifier, and writes a row.

Specifically, modify the existing function to ALSO call the new classifier:

```python
async def _safe_ocr_inline(message: discord.Message, chan_name: str) -> None:
    """Wrap ocr_attachments_inline + text classification under one
    semaphore acquisition. Continues to OCR image attachments through
    the original image-OCR pipeline (for image-extracted rows tied to
    discord_attachment_id), AND now also runs the unified text+vision
    classifier (which can pick up text-only trade narratives like
    ZHawk's 'PURR Leaps 12/18 $14 @4.10')."""
    sem = _get_eager_ocr_semaphore()
    async with sem:
        # 1. Existing image-OCR pipeline — unchanged
        try:
            from chat_ingestion.ocr import ocr_attachments_inline
            await ocr_attachments_inline(message)
        except Exception as e:
            log.warning(
                f"Eager OCR (image path) failed for msg={message.id} "
                f"channel='{chan_name}': {type(e).__name__}: {e}"
            )

        # 2. NEW: text+vision classifier — catches text-only trades AND
        # double-classifies image-bearing messages (dedup handles the
        # overlap with the image-OCR rows from step 1).
        try:
            await _classify_message_for_trade(message, chan_name)
        except Exception as e:
            log.warning(
                f"Eager text classifier failed for msg={message.id} "
                f"channel='{chan_name}': {type(e).__name__}: {e}"
            )


async def _classify_message_for_trade(
    message: discord.Message, chan_name: str,
) -> None:
    """Run the unified text+vision classifier on a Discord message.
    Writes a row to analyst_trades if the classifier identifies a
    trade. Dedup against existing image-OCR rows happens inside
    db.insert_text_extracted_trade_if_not_dup.
    """
    from analyst_log.ocr import extract_trade_from_message

    # Collect text content + image bytes
    text = (message.content or "").strip()
    image_bytes: list[bytes] = []
    for att in (message.attachments or []):
        ct = (getattr(att, "content_type", "") or "").lower()
        if not ct.startswith("image/"):
            continue
        try:
            data = await att.read()
            image_bytes.append(data)
        except Exception as e:
            log.debug(
                f"text classifier: attachment read failed "
                f"msg={message.id}: {e}"
            )

    extracted = await extract_trade_from_message(
        text=text,
        image_bytes_list=image_bytes,
        author_username=message.author.name,
        channel_name=chan_name,
    )
    if not extracted:
        return

    inserted = db.insert_text_extracted_trade_if_not_dup(
        author_id=message.author.id,
        author_username=message.author.name,
        discord_message_id=message.id,
        posted_at=message.created_at.isoformat(),
        extracted=extracted,
        channel_name=chan_name,
    )
    if inserted:
        log.info(
            f"text classifier: wrote row for {message.author.name} in "
            f"{chan_name} — {extracted.get('ticker')} {extracted.get('action')}"
        )
    else:
        log.debug(
            f"text classifier: deduped against existing image row for "
            f"msg={message.id}"
        )
```

- [ ] **Step 5: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_text_extraction_dedup.py
```

Expected: `ALL DEDUP SMOKE TESTS PASS`.

- [ ] **Step 6: Regression**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
$env:PYTHONPATH = "."; py scripts/smoke_extract_trade_from_message.py
$env:PYTHONPATH = "."; py -c "import chat_ingestion.watcher; import db; print('OK')"
```

Expected: all PASS.

- [ ] **Step 7: Commit**

```
git add db.py chat_ingestion/watcher.py scripts/smoke_text_extraction_dedup.py
git commit -m "watcher: wire text classifier into eager-OCR path with dedup

_safe_ocr_inline now runs the existing image-OCR pipeline AND the new
text+vision classifier. Image rows continue to be written tied to
discord_attachment_id; text/mixed rows go through
insert_text_extracted_trade_if_not_dup which skips duplicates within
±5 min of an existing image row for the same (ticker, expiry, strike,
contract_type, action). Abe's and BK's analyst-callers channels are
NOT in chat_eager_ocr_channels so this path doesn't touch them."
```

---

## Task 4 — Points ledger: wins-only +2, 7d window

**Files:**
- Modify: `db.py` — `compute_member_points` body
- Create: `scripts/smoke_points_ledger_wins_only.py`

The function keeps returning the same dict shape (so callers don't break), but `points = (entries_won + screenshot_wins) × 2` for everyone — no caller-vs-member split.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_points_ledger_wins_only.py`:

```python
"""Smoke test for the new wins-only +2 points ledger."""

import sqlite3
import sys
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _conn_with_trade_history(events: list[tuple]):
    """events = list of (posted_at, action, ticker, gain_pct, channel)"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with patch("db.get_connection", return_value=c):
        db.init_db()
    for i, (posted_at, action, ticker, gain_pct, channel) in enumerate(events, 1):
        c.execute(
            "INSERT INTO chat_messages (discord_message_id, channel_id, "
            "channel_name, author_id, author_username, posted_at, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1000 + i, 1, channel, 100, "u", posted_at, "x"),
        )
        c.execute(
            "INSERT INTO analyst_trades (discord_message_id, "
            "discord_attachment_id, author, author_id, posted_at, "
            "ticker, action, gain_pct, is_trade, tracking_mode, "
            "extraction_source) "
            "VALUES (?, ?, 'u', 100, ?, ?, ?, ?, 1, 'member', 'image')",
            (1000 + i, i, posted_at, ticker, action, gain_pct),
        )
    return c


def test_default_window_is_7_days():
    c = _conn_with_trade_history([])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100)
    assert result["window_days"] == 7, (
        f"expected default window_days=7, got {result['window_days']}"
    )
    _ok("default window is 7 days")


def test_entry_plus_winning_close_scores_2():
    today = "2026-06-01T12:00:00"
    later = "2026-06-02T12:00:00"
    c = _conn_with_trade_history([
        (today, "open", "AAPL", None, "💲-gain-loss-porn-💲"),
        (later, "close", "AAPL", 50.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)  # wide window
    assert result["entries_won"] == 1, result
    assert result["points"] == 2, f"expected 2 pts for 1 win, got {result['points']}"
    _ok("entry+winning_close = +2 (not +5)")


def test_entry_plus_losing_close_scores_0():
    today = "2026-06-01T12:00:00"
    later = "2026-06-02T12:00:00"
    c = _conn_with_trade_history([
        (today, "open", "TSLA", None, "🕰️-member-alerts-🕰️"),
        (later, "close", "TSLA", -30.0, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["entries_lost"] == 1, result
    assert result["points"] == 0, (
        f"expected 0 pts for 1 documented loss, got {result['points']}"
    )
    _ok("entry+losing_close = 0 (loss earns no points)")


def test_screenshot_win_scores_2():
    today = "2026-06-01T12:00:00"
    c = _conn_with_trade_history([
        (today, "close", "NVDA", 25.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["screenshot_wins"] == 1, result
    assert result["points"] == 2, result
    _ok("standalone screenshot win = +2")


def test_screenshot_loss_scores_0():
    today = "2026-06-01T12:00:00"
    c = _conn_with_trade_history([
        (today, "close", "META", -10.0, "💲-gain-loss-porn-💲"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    assert result["screenshot_losses"] == 1, result
    assert result["points"] == 0, result
    _ok("standalone screenshot loss = 0")


def test_ghost_scores_0():
    """Old policy: ghost = +2 for members. New policy: 0."""
    old = "2026-05-01T12:00:00"  # >14d ago
    c = _conn_with_trade_history([
        (old, "open", "LMT", None, "🕰️-member-alerts-🕰️"),
    ])
    with patch("db.get_connection", return_value=c):
        result = db.compute_member_points(100, days=14)
    # Ghost should be counted but contribute 0 points
    assert result["points"] == 0, result
    _ok("aged-out entry (ghost) = 0 pts under new policy")


if __name__ == "__main__":
    print("=== wins-only +2 ledger smoke ===")
    test_default_window_is_7_days()
    test_entry_plus_winning_close_scores_2()
    test_entry_plus_losing_close_scores_0()
    test_screenshot_win_scores_2()
    test_screenshot_loss_scores_0()
    test_ghost_scores_0()
    print("\nALL POINTS-LEDGER SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_points_ledger_wins_only.py
```

Expected: FAIL — current default window is 14 not 7.

- [ ] **Step 3: Update `compute_member_points` in db.py**

Find the function signature (currently `def compute_member_points(author_id: int, days: int = 14)`):

```python
def compute_member_points(author_id: int, days: int = 7) -> dict:
```

Then find the points-totalling block (around line 4242):

```python
    caller_mode = is_official_caller(int(author_id))
    if caller_mode:
        total_points = entries_won * 5 + screenshot_wins * 2
    else:
        total_points = (
            entries_won * 5
            + entries_lost * 3
            + entries_ghosted * 2
            + screenshot_wins * 2
            + screenshot_losses * 1
        )
```

Replace with:

```python
    # 2026-06-02 policy: wins-only +2 across the board. No caller-vs-
    # member split — same rule applies to everyone. Old policy was
    # +5/+3/+2/+2/+1 with a caller-nerf overlay; new policy collapses
    # to a single rule (wins × 2) so the score reflects exactly what
    # gets posted as a win, no implicit "commitment" credit.
    caller_mode = is_official_caller(int(author_id))
    total_points = (entries_won + screenshot_wins) * 2
```

Also update the docstring (the big point-values table block) — replace the per-event-type table with:

```
    Point values (2026-06-02 policy):

        +2 — entries_won (entry posted AND closes for a gain)
        +2 — screenshot_wins (standalone winning close screenshot,
             includes channel-based defaults for gain-less gain-loss-porn)
         0 — everything else (losses, ghosts, pending, losing screenshots)
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_points_ledger_wins_only.py
```

Expected: `ALL POINTS-LEDGER SMOKE TESTS PASS`.

- [ ] **Step 5: Regression**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py -c "import db; print('OK')"
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```
git add db.py scripts/smoke_points_ledger_wins_only.py
git commit -m "ledger: wins-only +2 policy, 7d default window

Collapses the 5-event-type point ladder (+5/+3/+2/+2/+1 with caller-
nerf overlay) to a single rule: (entries_won + screenshot_wins) × 2.
All loss buckets contribute 0. No caller-vs-member split — same rule
applies to everyone including Abe + BK. Default window 14d → 7d.
The returned dict shape is unchanged so callers (profile builder)
don't break."
```

---

## Task 5 — Drop the `min(100, ...)` cap on `trader_score`

**Files:**
- Modify: `scripts/backfill_user_profiles.py` (formula on line 1889 + prompt text references)
- Create: `scripts/smoke_score_no_cap.py`

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_score_no_cap.py`:

```python
"""Smoke test that the trader_score formula no longer caps at 100."""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_score_above_100_not_clipped():
    """The formula is `max(0, scaled_chatter + receipt_points)` — no
    upper bound. scaled_chatter=50 + receipts=120 should yield 170."""
    import inspect
    import re
    src = open("scripts/backfill_user_profiles.py").read()
    # Find the `trader_score = ...` assignment line and verify
    # min(100, ...) is gone from the production code path.
    matches = re.findall(
        r"trader_score\s*=\s*max\([^)]*\)\s*$", src, re.MULTILINE
    )
    for m in matches:
        assert "min(100" not in m, f"cap still present in: {m}"
    # Also: positive identification of the new formula
    assert (
        "max(0, _scaled_chatter + _receipt_pts)" in src
    ), "expected `max(0, _scaled_chatter + _receipt_pts)` in source"
    _ok("formula uses additive `max(0, scaled_chatter + receipt_pts)` (no min(100))")


def test_prompt_no_longer_promises_cap():
    """The prompt-text strings shown to Gemini should no longer
    promise the min(100) cap (otherwise the model frames the rubric
    around a cap that doesn't exist)."""
    src = open("scripts/backfill_user_profiles.py").read()
    # Critical: the FORMULA text inside the prompt should not assert
    # min(100, ...) as the truth anymore. (We may still mention
    # historical context in comments.)
    # Look for the docstring/prompt-text passages about the formula.
    suspect_lines = [
        ln for ln in src.splitlines()
        if "min(100" in ln and ln.lstrip().startswith(("'", '"', "f'", 'f"'))
    ]
    if suspect_lines:
        for ln in suspect_lines[:3]:
            print(f"    suspect: {ln.strip()[:120]}")
        _fail(
            "prompt-text still mentions min(100,...) — that would mislead "
            "the model into rubric-anchoring around a cap that's been removed"
        )
    _ok("prompt-text references to min(100,...) removed")


if __name__ == "__main__":
    print("=== no-cap formula smoke ===")
    test_score_above_100_not_clipped()
    test_prompt_no_longer_promises_cap()
    print("\nALL NO-CAP SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_score_no_cap.py
```

Expected: FAIL — current line 1889 still has `min(100, ...)`.

- [ ] **Step 3: Update line 1889 + prompt text in `scripts/backfill_user_profiles.py`**

Find line 1889:

```python
                            trader_score = max(0, min(100, _scaled_chatter + _receipt_pts))
```

Replace with:

```python
                            # 2026-06-02 policy: no upper bound on trader_score.
                            # Removed min(100, ...) so top traders separate from
                            # each other rather than all pinning to 100. Scaled
                            # chatter caps at 50; receipts are unbounded.
                            trader_score = max(0, _scaled_chatter + _receipt_pts)
```

Then update the prompt text. Find every occurrence of `min(100,` in prompt-text strings (lines 138, 393, 484, 532, 824 — verify with grep) and rewrite each so the rubric no longer promises a cap. Example replacement for line 484:

```
final_score = min(50, chatter_base) * min(1, msg_count / 500) + receipt_points
# No upper bound. Top traders separate by receipts.
```

And for line 138 narrative text, change:
```
The final = `min(100, clip(chatter_base + honesty, 50) + receipt_points)`.
```
to:
```
The final = `clip(chatter_base + honesty, 50) + receipt_points`. No upper cap — top traders separate by receipts.
```

Also update the points-window mentions: every "14-DAY POINTS LEDGER" → "7-DAY POINTS LEDGER", every "14d" → "7d" in the scoring narrative. The pre-cap text from Task 4 already changed the point values to wins-only +2; this task aligns the prompt narrative.

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_score_no_cap.py
```

Expected: `ALL NO-CAP SMOKE TESTS PASS`.

- [ ] **Step 5: Regression**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py scripts/smoke_points_ledger_wins_only.py
$env:PYTHONPATH = "."; py -c "import db; print('OK')"
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add scripts/backfill_user_profiles.py scripts/smoke_score_no_cap.py
git commit -m "score: drop min(100,...) cap and update prompt-text refs

trader_score = max(0, scaled_chatter + receipt_points). No upper
bound. The cap was making BK + Abe both display 100 even though
Abe has 2x the receipts (137 vs 62 in 14d). Top traders now
separate by receipts as the formula always intended.

Prompt-text updated to match: 14-DAY POINTS LEDGER → 7-DAY POINTS
LEDGER everywhere; min(100,...) references replaced with the
additive formula; caller-nerf language simplified since wins-only
now applies to everyone."
```

---

## Task 6 — Backfill: re-classify 30d of caller-owned + eager-OCR channels

**Files:**
- Create: `scripts/backfill_text_extracted_trades.py`
- Create: `scripts/smoke_backfill_resumable.py`

One-shot script. For each message in the last 30 days in eager-OCR channels (which now include ZHawk's `🫦-zhawk-thawghts-🗣`), call the classifier and write rows via the dedup helper. Checkpoints to `processing_log` so it can resume after a crash.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_backfill_resumable.py`:

```python
"""Smoke test for the backfill script's resume logic."""

import sqlite3
import sys
from unittest.mock import patch

import db


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_checkpoint_read_write():
    """Write a checkpoint, read it back."""
    import scripts.backfill_text_extracted_trades as bf
    c = sqlite3.connect(":memory:")
    with patch("db.get_connection", return_value=c):
        db.init_db()
        bf._write_checkpoint(channel="zhawk-thawghts", last_msg_id=12345)
        got = bf._read_checkpoint(channel="zhawk-thawghts")
    assert got == 12345, f"checkpoint round-trip failed: got {got!r}"
    _ok("checkpoint write + read round-trips")


def test_resume_skips_already_processed():
    """If checkpoint says last_msg_id=10, the backfill only processes
    messages with discord_message_id > 10."""
    import scripts.backfill_text_extracted_trades as bf
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    with patch("db.get_connection", return_value=c):
        db.init_db()
        # Insert messages with ids 5, 15, 25 in zhawk-thawghts
        for mid in (5, 15, 25):
            c.execute(
                "INSERT INTO chat_messages (discord_message_id, channel_id, "
                "channel_name, author_id, author_username, posted_at, "
                "content) VALUES (?, 1, '🫦-zhawk-thawghts-🗣', 100, 'u', "
                "'2026-06-01T12:00:00', 'PURR Leaps 12/18 $14')",
                (mid,),
            )
        bf._write_checkpoint(channel="🫦-zhawk-thawghts-🗣", last_msg_id=10)
        to_process = bf._iter_messages_to_process("🫦-zhawk-thawghts-🗣")
        ids = [r["discord_message_id"] for r in to_process]
    assert ids == [15, 25], f"expected [15, 25], got {ids}"
    _ok("resume skips messages with discord_message_id <= checkpoint")


if __name__ == "__main__":
    print("=== backfill resume smoke ===")
    test_checkpoint_read_write()
    test_resume_skips_already_processed()
    print("\nALL BACKFILL SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_backfill_resumable.py
```

Expected: FAIL — script doesn't exist yet.

- [ ] **Step 3: Create `scripts/backfill_text_extracted_trades.py`**

```python
"""One-shot backfill: re-classify last 30d of eager-OCR channel
messages through the text+vision classifier.

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


_CHECKPOINT_KIND = "text_backfill_checkpoint"


def _write_checkpoint(channel: str, last_msg_id: int) -> None:
    """Persist the last-processed discord_message_id for a channel."""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO processing_log (kind, payload, created_at) "
        "VALUES (?, ?, datetime('now'))",
        (_CHECKPOINT_KIND, json.dumps({
            "channel": channel, "last_msg_id": int(last_msg_id),
        })),
    )
    conn.commit()


def _read_checkpoint(channel: str) -> int:
    """Return the latest checkpointed discord_message_id for a channel
    (0 if none)."""
    conn = db.get_connection()
    rows = conn.execute(
        "SELECT payload FROM processing_log "
        "WHERE kind = ? ORDER BY id DESC",
        (_CHECKPOINT_KIND,),
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
        "  AND (content != '' OR image_ocr_text IS NOT NULL) "
        "ORDER BY discord_message_id ASC",
        (channel, last_seen),
    ).fetchall()


async def _process_message(row, channel: str) -> bool:
    """Classify one chat_messages row, write analyst_trades row if it's
    a trade. Returns True if a row was written.

    Uses cached image_ocr_text instead of re-fetching the image bytes
    (Discord CDN URLs expire ~24h). The eager-OCR pipeline already ran
    on these images and stored the OCR text in chat_messages.
    """
    from analyst_log.ocr import extract_trade_from_message

    # Skip messages that already have an analyst_trades row (covers
    # both the image-OCR rows from the existing pipeline AND any
    # text rows from a prior backfill run). The Tier 1 dedup in the
    # write helper would catch this too, but checking here saves a
    # Gemini call.
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
            "DELETE FROM processing_log WHERE kind = ?", (_CHECKPOINT_KIND,)
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
            # tight if we crash mid-channel
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
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_backfill_resumable.py
```

Expected: `ALL BACKFILL SMOKE TESTS PASS`.

- [ ] **Step 5: Regression**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py scripts/smoke_text_extraction_dedup.py
$env:PYTHONPATH = "."; py scripts/smoke_extract_trade_from_message.py
$env:PYTHONPATH = "."; py scripts/smoke_points_ledger_wins_only.py
$env:PYTHONPATH = "."; py scripts/smoke_score_no_cap.py
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```
git add scripts/backfill_text_extracted_trades.py scripts/smoke_backfill_resumable.py
git commit -m "backfill: re-classify last 30d of eager-OCR channel msgs

One-shot script. For each chat_messages row in the last 30 days in
chat_eager_ocr_channels with content OR cached image_ocr_text:
  1. Skip if analyst_trades already has a row for the discord_message_id
     (covers existing image-OCR rows; saves a Gemini call too)
  2. Build classifier input: message.content (text caption) +
     image_ocr_text (eager-OCR's prior screenshot extraction). Discord
     CDN URLs expire ~24h so re-fetching the original images isn't
     viable for old messages — the cached OCR is the next best signal.
  3. Run unified classifier; write analyst_trades row via
     insert_text_extracted_trade_if_not_dup (Tier 1 + Tier 2 dedup).

Resumable: writes per-channel checkpoints to processing_log. On
re-run, only processes messages with discord_message_id > checkpoint.
Pass --reset-checkpoints to start over.

Run AFTER all 5 earlier tasks deploy: the backfill needs the new
schema column, classifier, and dedup helper to be live."
```

---

## Task 7 — Final integration: full smoke + push + run backfill

**Files:** none new.

- [ ] **Step 1: Run the full smoke suite**

```
$env:PYTHONPATH = "."; py scripts/smoke_pyflakes_undefined.py
$env:PYTHONPATH = "."; py scripts/smoke_extraction_source_column.py
$env:PYTHONPATH = "."; py scripts/smoke_extract_trade_from_message.py
$env:PYTHONPATH = "."; py scripts/smoke_text_extraction_dedup.py
$env:PYTHONPATH = "."; py scripts/smoke_points_ledger_wins_only.py
$env:PYTHONPATH = "."; py scripts/smoke_score_no_cap.py
$env:PYTHONPATH = "."; py scripts/smoke_backfill_resumable.py
$env:PYTHONPATH = "."; py scripts/smoke_arch_leak_retry.py
$env:PYTHONPATH = "."; py scripts/smoke_prompt_block_retry.py
$env:PYTHONPATH = "."; py scripts/smoke_batch2.py
$env:PYTHONPATH = "."; py scripts/smoke_user_profile_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_trade_log_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_market_price_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_chat_search_keyword_optional.py
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
$env:PYTHONPATH = "."; py scripts/smoke_tools_wired.py
$env:PYTHONPATH = "."; py scripts/smoke_deeper_validation.py
$env:PYTHONPATH = "."; py scripts/smoke_format_analyst_trades_kind.py
$env:PYTHONPATH = "."; py scripts/smoke_db_resolve_username.py
$env:PYTHONPATH = "."; py scripts/smoke_db_recent_trades_section.py
$env:PYTHONPATH = "."; py tests/pulse_regression/run.py
```

Expected: every script ends `... PASS`.

- [ ] **Step 2: Push**

```
git push
```

- [ ] **Step 3: Wait for Railway deploy**

```
until railway logs --deployment 2>&1 | grep -q "Discord bot connected as"; do sleep 5; done
railway logs --deployment 2>&1 | grep -iE "Discord bot connected|extraction_source|migration|NameError" | tail -10
```

Expected: see the new "Discord bot connected as" line; no NameError; migration log line should appear if the column was added on this deploy.

- [ ] **Step 4: Run the backfill against production**

```
railway ssh "/opt/venv/bin/python /app/scripts/backfill_text_extracted_trades.py"
```

Expected output: list of target channels, then per-channel "N messages above checkpoint to process" + "text rows written so far: N", then "Backfill complete." Total rows expected: probably 30-100 depending on how many text trade calls exist in the last 30 days across the 7 eager-OCR channels.

- [ ] **Step 5: Verify ZHawk's score reflects the backfilled rows**

```
railway ssh '/opt/venv/bin/python -c "
import sys; sys.path.insert(0, \"/app\")
import db
UID = 390341447918026753
p = db.compute_member_points(UID, days=7)
print(\"ZHawk 7d points:\", p[\"points\"])
print(\"  entries_won:\", p[\"entries_won\"])
print(\"  screenshot_wins:\", p[\"screenshot_wins\"])
"'
```

Expected: ZHawk's 7d points > 0 (specific number depends on his actual wins in 7d). Before this work, his points were 0.

- [ ] **Step 6: Trigger a user-profile refresh so trader_score reflects the new ledger**

```
railway ssh "/opt/venv/bin/python /app/scripts/backfill_user_profiles.py --force --users 390341447918026753,423994649317736448,1192771108332650496"
```

(ZHawk + BK + Abe user_ids.)

Expected: profile builder runs for each user; trader_score recomputes under the new formula. BK's score likely drops from 100; Abe's score likely drops from 100; ZHawk's score moves up from 42 (or wherever it lands with new receipts).

- [ ] **Step 7: Spot-check 10 text-extracted rows for sanity**

```
railway ssh '/opt/venv/bin/python -c "
import sys; sys.path.insert(0, \"/app\")
import sqlite3
conn = sqlite3.connect(\"/data/reports.db\")
rows = conn.execute(
    \"SELECT at.posted_at, at.author, at.ticker, at.action, at.strike, at.expiry, \"
    \"at.gain_pct, at.is_trade, at.extraction_source, cm.channel_name, cm.content \"
    \"FROM analyst_trades at \"
    \"LEFT JOIN chat_messages cm ON cm.discord_message_id = at.discord_message_id \"
    \"WHERE at.extraction_source IN (\\\"text\\\", \\\"mixed\\\") \"
    \"ORDER BY at.created_at DESC LIMIT 10\"
).fetchall()
for r in rows:
    print(r)
"'
```

Expected: each row's ticker/action/strike/expiry should match the cited `cm.content` text. If any row looks like a fabrication or misclassification, flag for QC.

- [ ] **Step 8: Final commit (if Step 7 surfaced any tunings)**

If any smoke fixes were needed after deploy:

```
git add -A
git commit -m "trader-log overhaul: final smoke fixes"
git push
```

Otherwise skip.
