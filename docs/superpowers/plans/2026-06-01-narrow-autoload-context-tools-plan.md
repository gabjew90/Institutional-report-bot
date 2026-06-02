# Narrow auto-load + on-demand context tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move two heavy always-injected prompt blocks (`profiles_block` + `analyst_block`) behind on-demand Gemini tool calls, narrow profile auto-load to strictly-committed users (asker + Discord @-mention + reply/forward author), and add a live-price tool. Stops the slur-laden auto-injection that tripped Gemini's hard filter on 2026-06-01 22:01:45 UTC and saves ~8-12 KB of prompt per call.

**Architecture:** Replace the existing `lookup_user_ranks` tool with a richer unified `lookup_user_profile`; add a new `lookup_trade_log` (works for callers AND members) and `lookup_market_price` (Finnhub for equities, Binance.US for crypto). Extend `search_chat_messages` so `keyword` is optional. Strip the fuzzy `find_users_mentioned_in_text` triggers from profile auto-load. Single-PR rollout.

**Tech Stack:**
- Python 3.10+ with `google.genai` SDK for tool definitions
- SQLite via project's `db.py`
- `report.market_data._fetch_finnhub_quote` + `_fetch_binance_24h` (existing) for price data
- Plain-script smoke tests in `scripts/smoke_*.py` (no pytest)

**Spec:** `docs/superpowers/specs/2026-06-01-narrow-autoload-context-tools-design.md`

---

## File Structure

**Files modified:**

| File | What changes |
|---|---|
| `db.py` | Add `kind` parameter to `format_analyst_trades_for_context`; add two new helpers: `resolve_username_to_user_id` and `get_user_profile_recent_trades_section`. |
| `discord_bot/bot.py` | Add 3 new tool defs + executors (`lookup_user_profile`, `lookup_trade_log`, `lookup_market_price`). Remove old `_build_user_ranks_tool` + `_execute_user_ranks`. Extend `_build_chat_search_tool` + `_execute_chat_search` so `keyword` is optional. Update tool-call dispatch loop. Update `_ASK_SYSTEM_INSTRUCTION`. Drop `find_users_mentioned_in_text(question)` and `find_users_mentioned_in_text(ref_content)` from `profile_ids` build in both /ask entry paths. Drop `analyst_block` assembly from `_answer_with_gemini`. |

**Files created:**

| File | Purpose |
|---|---|
| `scripts/smoke_format_analyst_trades_kind.py` | Verifies `kind` parameter on `format_analyst_trades_for_context` slices correctly. |
| `scripts/smoke_db_resolve_username.py` | Verifies `resolve_username_to_user_id` chain (profiles → chat_messages). |
| `scripts/smoke_db_recent_trades_section.py` | Verifies regex extraction of "Recent trades" section from `user_profiles.profile_text`. |
| `scripts/smoke_user_profile_tool.py` | Verifies the unified `lookup_user_profile` tool: rank modes + include_profile + error modes. |
| `scripts/smoke_trade_log_tool.py` | Verifies `lookup_trade_log` caller anchor + username anchor + kind slicing + error modes. |
| `scripts/smoke_market_price_tool.py` | Verifies `lookup_market_price`: stock route → Finnhub, crypto route → Binance, validation, session field. |
| `scripts/smoke_chat_search_keyword_optional.py` | Verifies `search_chat_messages` accepts username/channel_name-only queries. |
| `scripts/smoke_profile_scope_narrowed.py` | Static check: `find_users_mentioned_in_text` not in profile_ids build path; `analyst_block` not assembled. |
| `scripts/smoke_tools_wired.py` | Static check: all 4 tools appear in main `config.tools` and the repetition-retry `retry_config.tools`. Static check that `lookup_user_ranks` is fully gone. |

---

## Task 1: DB helper — `kind` parameter on `format_analyst_trades_for_context`

**Files:**
- Modify: `db.py:1897-2040` (the function)
- Test: `scripts/smoke_format_analyst_trades_kind.py` (create)

Today the function always emits three concatenated sub-blocks: RECENT TRADES + OPEN POSITIONS + W/L TALLY. The new `kind` parameter gates which sub-blocks emit. `kind="all"` (default) preserves today's behavior byte-for-byte so existing call sites don't break.

**Important quirk to preserve for `kind="all"`:** today the function returns `""` early if there are no recent trades (even if open positions exist). For `kind="all"` we keep this early exit. For `kind="open"` / `kind="tally"` alone, we don't — the caller asked specifically for that slice.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_format_analyst_trades_kind.py`:

```python
"""Smoke test for the `kind` parameter on format_analyst_trades_for_context.

Validates:
  1. kind="all" (default) preserves today's behavior
  2. kind="recent" returns ONLY the RECENT TRADES sub-block
  3. kind="open" returns ONLY the CURRENTLY OPEN POSITIONS sub-block
  4. kind="tally" returns ONLY the W/L TALLY sub-block
  5. kind="invalid" raises ValueError
  6. kind="all" with no recent rows returns "" (legacy quirk)
"""

import sys
from unittest.mock import patch

from db import format_analyst_trades_for_context


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Stub the three data-source functions so the test is hermetic.
FAKE_RECENT_ROWS = [
    {
        "posted_at": "2026-06-01T15:00", "action": "open", "ticker": "TSLA",
        "contract_type": "call", "strike": 445.0, "expiry": "2026-06-05",
        "price": 3.55, "gain_pct": None, "inferred_status": None,
    },
]
FAKE_POSITIONS = [
    {
        "ticker": "META", "contract_type": "call", "strike": 640.0,
        "expiry": "2026-06-05", "entry_price": 6.40,
    },
]
FAKE_WL = {"days": 30, "wins": 42, "losses": 3, "total_losses": 29,
           "decided": 71, "win_rate": 0.592, "avg_win": 104.9,
           "avg_doc_loss": -22.4, "winning_closes": [], "silent_expiries": []}
EMPTY_WL = {"days": 30, "wins": 0, "losses": 0, "total_losses": 0,
            "decided": 0, "win_rate": 0.0, "avg_win": 0.0,
            "avg_doc_loss": 0.0, "winning_closes": [], "silent_expiries": []}


def test_kind_all_returns_all_three_blocks():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="all")
    assert "ABE'S RECENT TRADES" in out, "missing RECENT block in kind=all"
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, "missing OPEN block in kind=all"
    assert "ABE'S W/L TALLY" in out, "missing TALLY block in kind=all"
    _ok("kind='all' returns all three sub-blocks")


def test_kind_recent_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="recent")
    assert "ABE'S RECENT TRADES" in out, "missing RECENT block in kind=recent"
    assert "ABE'S CURRENTLY OPEN POSITIONS" not in out, "kind=recent should not include OPEN"
    assert "ABE'S W/L TALLY" not in out, "kind=recent should not include TALLY"
    _ok("kind='recent' returns only RECENT block")


def test_kind_open_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="open")
    assert "ABE'S RECENT TRADES" not in out, "kind=open should not include RECENT"
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, "missing OPEN block in kind=open"
    assert "ABE'S W/L TALLY" not in out, "kind=open should not include TALLY"
    _ok("kind='open' returns only OPEN block")


def test_kind_tally_only():
    with (
        patch("db.get_recent_analyst_trades", return_value=FAKE_RECENT_ROWS),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="tally")
    assert "ABE'S RECENT TRADES" not in out, "kind=tally should not include RECENT"
    assert "ABE'S CURRENTLY OPEN POSITIONS" not in out, "kind=tally should not include OPEN"
    assert "ABE'S W/L TALLY" in out, "missing TALLY block in kind=tally"
    _ok("kind='tally' returns only TALLY block")


def test_kind_invalid_raises():
    try:
        format_analyst_trades_for_context(caller="abe", kind="bogus")
    except ValueError as e:
        assert "kind" in str(e).lower()
        _ok("kind='bogus' raises ValueError")
        return
    _fail("kind='bogus' should have raised ValueError")


def test_kind_all_no_recent_returns_empty():
    """Legacy quirk: kind='all' with no recent rows returns '' even if positions exist."""
    with (
        patch("db.get_recent_analyst_trades", return_value=[]),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=FAKE_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="all")
    assert out == "", f"expected '', got {out!r}"
    _ok("kind='all' with no recent rows returns '' (legacy quirk preserved)")


def test_kind_open_no_recent_still_emits_open():
    """kind='open' alone does NOT skip on empty recent — that quirk is kind='all' only."""
    with (
        patch("db.get_recent_analyst_trades", return_value=[]),
        patch("db.get_current_analyst_positions", return_value=FAKE_POSITIONS),
        patch("db.compute_caller_win_loss_summary", return_value=EMPTY_WL),
    ):
        out = format_analyst_trades_for_context(caller="abe", kind="open")
    assert "ABE'S CURRENTLY OPEN POSITIONS" in out, (
        "kind='open' alone should emit the OPEN block even without recent rows"
    )
    _ok("kind='open' alone emits even when no recent rows")


if __name__ == "__main__":
    print("=== format_analyst_trades_for_context kind smoke ===")
    test_kind_all_returns_all_three_blocks()
    test_kind_recent_only()
    test_kind_open_only()
    test_kind_tally_only()
    test_kind_invalid_raises()
    test_kind_all_no_recent_returns_empty()
    test_kind_open_no_recent_still_emits_open()
    print("\nALL KIND-PARAMETER SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_format_analyst_trades_kind.py
```

Expected: FAIL with `TypeError: format_analyst_trades_for_context() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Edit `db.py:1897-2040` to add the `kind` parameter**

Update the function signature (line 1897):

```python
def format_analyst_trades_for_context(
    hours: int = 168,
    limit: int = 30,
    caller: str | None = None,
    display: str | None = None,
    tracking_mode: str | None = "caller",
    kind: str = "all",
) -> str:
```

Replace the function body (lines 1903-end) with the kind-gated version. The new body:

```python
    """Render the last N hours of trade-tagged rows as a context block for /ask.

    Intentionally OMITS captions and notes — we don't want the bot to quote
    the caller verbatim. The bot gets ticker/strike/expiry/action/gain only,
    and must paraphrase if the user asks "what did he say."

    When `caller` is set, restricts rows + headers to that caller. `display`
    is the human-readable name for headers (defaults to caller.title()).
    None for both = legacy global behavior.

    `kind` ∈ {"all", "recent", "open", "tally"}:
      - "all" (default) emits RECENT + OPEN + TALLY. Preserves legacy
        early-return: if no recent rows, returns "" even if positions
        or tally would otherwise emit.
      - "recent" emits only the RECENT TRADES block.
      - "open" emits only the CURRENTLY OPEN POSITIONS block.
      - "tally" emits only the W/L TALLY block.
    Any other value raises ValueError.
    """
    if kind not in ("all", "recent", "open", "tally"):
        raise ValueError(
            f"kind must be one of: all, recent, open, tally; got {kind!r}"
        )

    out_lines: list[str] = []
    display_name = display or (caller.title() if caller else "Abe")
    header_prefix = display_name.upper()

    # RECENT TRADES block
    if kind in ("all", "recent"):
        rows = get_recent_analyst_trades(
            hours=hours, limit=limit, caller=caller, tracking_mode=tracking_mode,
        )
        if rows:
            out_lines.append(
                f"{header_prefix}'S RECENT TRADES (last {hours // 24} days, "
                f"auto-logged from his alerts channel — for context only, "
                f"don't quote captions; he didn't share them with you):"
            )
            for r in reversed(rows):
                ticker = r.get("ticker") or "?"
                ct = (r.get("contract_type") or "").lower()
                ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
                strike = r.get("strike")
                strike_str = (
                    f"{int(strike) if strike == int(strike) else strike}"
                    if strike is not None else "?"
                )
                expiry = r.get("expiry") or ""
                exp_short = expiry[5:] if len(expiry) >= 10 else expiry
                action = (r.get("action") or "?").lower()
                gain = r.get("gain_pct")
                price = r.get("price")
                try:
                    price_f = float(price) if price is not None else None
                except (TypeError, ValueError):
                    price_f = None
                try:
                    gain_f = float(gain) if gain is not None else None
                except (TypeError, ValueError):
                    gain_f = None
                suffix_str = ""
                if action in ("open", "add") and price_f and price_f != 0:
                    suffix_str = f" @{price_f:.2f}"
                elif action in ("close", "trim") and gain_f is not None and gain_f != 0:
                    suffix_str = f" ({gain_f:+.1f}%)"
                posted_at = (r.get("posted_at") or "")[:16].replace("T", " ")
                status_tag = ""
                status = r.get("inferred_status")
                if status == "expired_unknown":
                    if action in ("open", "add"):
                        status_tag = " [expired — no close alert]"
                    else:
                        status_tag = " [expired]"
                elif status == "close_without_open":
                    status_tag = " [exit only — no logged entry]"
                out_lines.append(
                    f"- {posted_at} — {action} {ticker} "
                    f"{strike_str}{ct_suffix} {exp_short}{suffix_str}{status_tag}"
                )
        elif kind == "all":
            # Legacy quirk: kind='all' returns "" when no recent rows even
            # if positions / tally would otherwise emit. Preserved so the
            # existing prompt-assembly call site (which checks `if
            # analyst_block:` before appending) keeps its behavior.
            return ""

    # CURRENTLY OPEN POSITIONS block
    if kind in ("all", "open"):
        positions = get_current_analyst_positions(
            caller=caller, tracking_mode=tracking_mode,
        )
        if positions:
            if out_lines:
                out_lines.append("")
            out_lines.append(
                f"{header_prefix}'S CURRENTLY OPEN POSITIONS "
                f"(sorted by closest expiry first):"
            )
            for p in positions[:20]:
                ticker = p.get("ticker") or "?"
                ct = (p.get("contract_type") or "").lower()
                ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
                strike = p.get("strike")
                strike_str = (
                    f"{int(strike) if strike == int(strike) else strike}"
                    if strike is not None else "?"
                )
                expiry = p.get("expiry") or ""
                exp_short = expiry[5:] if len(expiry) >= 10 else expiry
                entry_price = p.get("entry_price")
                price_str = ""
                try:
                    ep_f = float(entry_price) if entry_price is not None else None
                except (TypeError, ValueError):
                    ep_f = None
                if ep_f and ep_f != 0:
                    price_str = f" @{ep_f:.2f}"
                out_lines.append(
                    f"- {ticker} {strike_str}{ct_suffix} {exp_short}{price_str}"
                )

    # W/L TALLY block
    if kind in ("all", "tally"):
        wl = compute_caller_win_loss_summary(
            days=30, caller=caller, tracking_mode=tracking_mode,
        )
        if wl["decided"] > 0:
            if out_lines:
                out_lines.append("")
            out_lines.append(
                f"{header_prefix}'S W/L TALLY (last {wl['days']}d — "
                f"expirations-without-close counted as L):"
            )
            out_lines.append(
                f"- **{wl['wins']}W / {wl['total_losses']}L** "
                f"(documented: {wl['losses']}L, "
                f"silent expiry: {wl['total_losses'] - wl['losses']}L)"
            )
            out_lines.append(
                f"- Win rate: {wl['win_rate'] * 100:.1f}% on {wl['decided']} decided trades"
            )
            if wl.get("avg_win"):
                out_lines.append(f"- Avg win: +{wl['avg_win']:.1f}%")
            if wl.get("avg_doc_loss"):
                out_lines.append(f"- Avg documented loss: {wl['avg_doc_loss']:.1f}%")
            for label, key in (
                ("Winning closes (specific contracts):", "winning_closes"),
                ("Silent-expiry losses (opens with no close, expired):", "silent_expiries"),
            ):
                items = wl.get(key) or []
                if items:
                    out_lines.append(f"- {label}")
                    for it in items[:25]:
                        out_lines.append(f"  · {it}")

    return "\n".join(out_lines)
```

**Important:** preserve the rest of `db.py` exactly as-is. Only this one function changes.

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_format_analyst_trades_kind.py
```

Expected: `ALL KIND-PARAMETER SMOKE TESTS PASS`.

- [ ] **Step 5: Commit**

```
git add db.py scripts/smoke_format_analyst_trades_kind.py
git commit -m "db: add kind parameter to format_analyst_trades_for_context"
```

---

## Task 2: DB helper — `resolve_username_to_user_id`

**Files:**
- Modify: `db.py` (add new function near other lookup helpers, e.g. after `lookup_user_ranks`)
- Test: `scripts/smoke_db_resolve_username.py` (create)

Resolves a Discord username to a user_id by chaining:
1. `user_profiles.username` exact match (case-insensitive)
2. `chat_messages.author_username` most recent row's user_id (case-insensitive)

Returns `None` if neither hits.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_db_resolve_username.py`:

```python
"""Smoke test for db.resolve_username_to_user_id.

Validates:
  1. Username with a profile row → returns user_id from user_profiles
  2. Username with no profile but chat history → returns user_id from chat_messages
  3. Unknown username → None
  4. Empty string / None → None
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


def _make_test_conn() -> sqlite3.Connection:
    """In-memory DB with the two tables we touch."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_text TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id INTEGER UNIQUE,
            author_id INTEGER NOT NULL,
            author_username TEXT,
            posted_at TEXT NOT NULL
        );
    """)
    return conn


def test_resolves_via_user_profiles():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name) "
        "VALUES (?, ?, ?)",
        (12345, "bankerkyle", "BK"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("bankerkyle")
    assert uid == 12345, f"expected 12345, got {uid!r}"
    _ok("resolves via user_profiles (lowercase exact)")


def test_case_insensitive_via_user_profiles():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, username, display_name) "
        "VALUES (?, ?, ?)",
        (12345, "bankerkyle", "BK"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("BankerKyle")
    assert uid == 12345, f"expected 12345 for case-insensitive match, got {uid!r}"
    _ok("user_profiles match is case-insensitive")


def test_falls_back_to_chat_messages():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO chat_messages "
        "(discord_message_id, author_id, author_username, posted_at) "
        "VALUES (?, ?, ?, ?)",
        (1001, 67890, "newuser", "2026-06-01T15:00:00Z"),
    )
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("newuser")
    assert uid == 67890, f"expected 67890 (chat_messages fallback), got {uid!r}"
    _ok("falls back to chat_messages when no profile")


def test_unknown_returns_none():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        uid = db.resolve_username_to_user_id("nobody")
    assert uid is None, f"expected None for unknown user, got {uid!r}"
    _ok("unknown username → None")


def test_empty_input_returns_none():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        assert db.resolve_username_to_user_id("") is None, "empty string should return None"
        assert db.resolve_username_to_user_id(None) is None, "None should return None"
        assert db.resolve_username_to_user_id("   ") is None, "whitespace should return None"
    _ok("empty / whitespace / None input → None")


if __name__ == "__main__":
    print("=== resolve_username_to_user_id smoke ===")
    test_resolves_via_user_profiles()
    test_case_insensitive_via_user_profiles()
    test_falls_back_to_chat_messages()
    test_unknown_returns_none()
    test_empty_input_returns_none()
    print("\nALL RESOLVE-USERNAME SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_db_resolve_username.py
```

Expected: FAIL with `AttributeError: module 'db' has no attribute 'resolve_username_to_user_id'`.

- [ ] **Step 3: Add the function to `db.py`**

Insert this function in `db.py` immediately AFTER the existing `lookup_user_ranks` function (locate it via `grep -n "^def lookup_user_ranks" db.py`; find the matching `^def ` line that ends the function and insert after it):

```python
def resolve_username_to_user_id(username: str | None) -> int | None:
    """Resolve a Discord username to a user_id, trying two sources in order.

    1. user_profiles.username — LLM-canonical, lowercase. Exact match,
       case-insensitive.
    2. chat_messages.author_username — most recent message row's
       author_id. Case-insensitive. Used when a member has chat
       activity but no profile yet.

    Returns None when neither source has an exact match, when input is
    empty / None / whitespace, or when DB access fails.
    """
    if not username:
        return None
    name = username.strip().lstrip("@")
    if not name:
        return None
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT user_id FROM user_profiles "
            "WHERE LOWER(username) = LOWER(?) LIMIT 1",
            (name,),
        ).fetchone()
        if row:
            return int(row["user_id"])
        row = conn.execute(
            "SELECT author_id FROM chat_messages "
            "WHERE LOWER(author_username) = LOWER(?) "
            "ORDER BY posted_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        if row:
            return int(row["author_id"])
    except Exception as e:
        log.warning(f"resolve_username_to_user_id failed for {name!r}: {e}")
    return None
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_db_resolve_username.py
```

Expected: `ALL RESOLVE-USERNAME SMOKE TESTS PASS`.

- [ ] **Step 5: Commit**

```
git add db.py scripts/smoke_db_resolve_username.py
git commit -m "db: add resolve_username_to_user_id helper"
```

---

## Task 3: DB helper — `get_user_profile_recent_trades_section`

**Files:**
- Modify: `db.py` (add new function after `resolve_username_to_user_id`)
- Test: `scripts/smoke_db_recent_trades_section.py` (create)

Reads `user_profiles.profile_text` for a user_id and regex-extracts the markdown section starting at `**Recent trades.**` and ending at the next bold heading or end-of-string. Returns `""` if profile or section is absent.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_db_recent_trades_section.py`:

```python
"""Smoke test for db.get_user_profile_recent_trades_section.

Validates:
  1. Profile with Recent trades section → returns the body (without heading)
  2. Profile without that section → ""
  3. Profile present but profile_text is empty → ""
  4. Unknown user_id → ""
  5. Recent trades section is the LAST section in the profile → returns body
     extending to end-of-string
  6. "**Recent Trades.**" (capital T) also matches (case-insensitive)
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


PROFILE_WITH_SECTION = """\
**Personality and style.**
SV is a high-octane trader.

**Voice.**
- "Diamond cock" — recurring self-description

**Recent trades.**
- $PLTR / 145C (6/1 entry) — closed for +911.84%
- $HPE / 50C (5/29 entry) — closed at +6.90%

**Recent personal life.**
- claimed to be working on an oil rig
"""

PROFILE_WITHOUT_SECTION = """\
**Personality and style.**
Some content here.

**Voice.**
- "test" — when testing
"""

PROFILE_RECENT_TRADES_LAST = """\
**Personality and style.**
Some content.

**Recent trades.**
- $TSLA / 445C — open
- $META / 640C — open
"""


def _make_test_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE user_profiles (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            profile_text TEXT NOT NULL DEFAULT ''
        )
    """)
    return conn


def test_extracts_section_body():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_WITH_SECTION),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$PLTR" in out, f"expected PLTR line in output, got: {out!r}"
    assert "$HPE" in out, f"expected HPE line in output, got: {out!r}"
    assert "Personality" not in out, "should not bleed prior section into output"
    assert "personal life" not in out.lower(), "should stop at next section heading"
    _ok("extracts Recent trades body without bleeding into adjacent sections")


def test_returns_empty_when_section_missing():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_WITHOUT_SECTION),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert out == "", f"expected '' when section absent, got: {out!r}"
    _ok("returns '' when Recent trades section is absent")


def test_returns_empty_when_user_unknown():
    conn = _make_test_conn()
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(99999)
    assert out == "", f"expected '' for unknown user, got: {out!r}"
    _ok("returns '' for unknown user_id")


def test_recent_trades_as_last_section():
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, PROFILE_RECENT_TRADES_LAST),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$TSLA" in out, f"expected TSLA in last-section case, got: {out!r}"
    assert "$META" in out, f"expected META in last-section case, got: {out!r}"
    _ok("returns body when Recent trades is the last section")


def test_case_insensitive_heading_match():
    """**Recent Trades.** (capital T) should match too."""
    cap_t = PROFILE_WITH_SECTION.replace("**Recent trades.**", "**Recent Trades.**")
    conn = _make_test_conn()
    conn.execute(
        "INSERT INTO user_profiles (user_id, profile_text) VALUES (?, ?)",
        (1, cap_t),
    )
    with patch("db.get_connection", return_value=conn):
        out = db.get_user_profile_recent_trades_section(1)
    assert "$PLTR" in out, f"capital-T heading not matched: {out!r}"
    _ok("heading match is case-insensitive (Recent Trades / recent trades)")


if __name__ == "__main__":
    print("=== get_user_profile_recent_trades_section smoke ===")
    test_extracts_section_body()
    test_returns_empty_when_section_missing()
    test_returns_empty_when_user_unknown()
    test_recent_trades_as_last_section()
    test_case_insensitive_heading_match()
    print("\nALL RECENT-TRADES-SECTION SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_db_recent_trades_section.py
```

Expected: FAIL with `AttributeError: module 'db' has no attribute 'get_user_profile_recent_trades_section'`.

- [ ] **Step 3: Add the function to `db.py`**

Insert immediately AFTER `resolve_username_to_user_id` (added in Task 2):

```python
def get_user_profile_recent_trades_section(user_id: int) -> str:
    """Extract the "Recent trades" markdown section from a user's profile.

    The profile builder lays out user profiles with bold section headers:
      **Personality and style.**
      **Voice.**
      **Retarded takes.**
      **Recent trades.**
      **Recent personal life.**

    This helper pulls the body of the Recent trades section — the bullet
    lines beneath the heading — and returns them as a single string. The
    section ends at the next bold heading of the same shape (`**Word.**`)
    OR end-of-string.

    Returns "" when:
      - the user has no profile row
      - the profile_text is empty
      - the Recent trades heading is absent
      - DB access fails

    Tied to the profile prompt template's heading format. If that template
    changes (e.g. switches to `## Recent Trades` markdown headers), update
    the regex here too.
    """
    if not user_id:
        return ""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT profile_text FROM user_profiles WHERE user_id = ? LIMIT 1",
            (int(user_id),),
        ).fetchone()
    except Exception as e:
        log.warning(
            f"get_user_profile_recent_trades_section query failed for "
            f"user_id={user_id}: {e}"
        )
        return ""
    if not row:
        return ""
    text = row["profile_text"] or ""
    if not text:
        return ""
    import re as _re
    m = _re.search(
        r"\*\*Recent\s+trades\.\*\*\s*\n(.*?)(?=\n\*\*[A-Z][^*]*?\.\*\*|\Z)",
        text,
        flags=_re.IGNORECASE | _re.DOTALL,
    )
    if not m:
        return ""
    return m.group(1).strip()
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_db_recent_trades_section.py
```

Expected: `ALL RECENT-TRADES-SECTION SMOKE TESTS PASS`.

- [ ] **Step 5: Commit**

```
git add db.py scripts/smoke_db_recent_trades_section.py
git commit -m "db: add get_user_profile_recent_trades_section helper"
```

---

## Task 4: Tool — `lookup_user_profile` (replaces `lookup_user_ranks`)

**Files:**
- Modify: `discord_bot/bot.py` — add `_build_user_profile_tool()` and `_execute_user_profile()` next to existing `_build_user_ranks_tool` / `_execute_user_ranks` (around line 1549-1730)
- Modify: `discord_bot/bot.py` — wire into the two tools lists (main config ~line 2076; retry config ~line 2291) and the tool-call dispatch loop (around line 2200, alongside other `elif fc.name == ...` branches)
- Modify: `discord_bot/bot.py` — remove old `_build_user_ranks_tool`, `_execute_user_ranks`, their wiring, and dispatch case
- Test: `scripts/smoke_user_profile_tool.py` (create)

The new tool unifies the 3 modes of `lookup_user_ranks` plus optional full-dossier fetch.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_user_profile_tool.py`:

```python
"""Smoke test for the lookup_user_profile tool.

Validates:
  1. Tool definition has correct name + parameters in schema
  2. _execute_user_profile error modes:
     - no anchor → error
     - both username and metric → error
     - leaderboard mode + include_profile=True → error
  3. _execute_user_profile success modes:
     - username only → rank + rationales
     - username + include_profile=True → above + dossier
     - metric + rank_position → user at that rank
     - metric only → top 5
  4. Old lookup_user_ranks is gone from the module
"""

import asyncio
import inspect
import sys
from unittest.mock import patch

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_definition_shape():
    tool = bot_mod._build_user_profile_tool()
    decls = tool.function_declarations
    assert len(decls) == 1, "expected exactly one FunctionDeclaration"
    decl = decls[0]
    assert decl.name == "lookup_user_profile", f"unexpected name: {decl.name}"
    props = decl.parameters.properties
    for arg in ("username", "metric", "rank_position", "include_profile"):
        assert arg in props, f"missing parameter {arg!r} in schema"
    _ok("_build_user_profile_tool: name + all 4 parameters present in schema")


def test_no_anchor_error():
    result = asyncio.run(bot_mod._execute_user_profile({}))
    assert "error" in result, f"expected error key, got {result}"
    _ok("no anchor → error")


def test_both_anchors_error():
    result = asyncio.run(bot_mod._execute_user_profile({
        "username": "foo", "metric": "trader",
    }))
    assert "error" in result, f"expected error key, got {result}"
    assert "exactly one" in result["error"].lower(), (
        f"error should mention 'exactly one anchor', got: {result['error']!r}"
    )
    _ok("both username and metric set → error")


def test_leaderboard_plus_include_profile_error():
    result = asyncio.run(bot_mod._execute_user_profile({
        "metric": "trader", "include_profile": True,
    }))
    assert "error" in result, f"expected error key, got {result}"
    assert "leaderboard" in result["error"].lower() or "profile" in result["error"].lower(), (
        f"error should mention leaderboard mode rejecting include_profile, got: {result['error']!r}"
    )
    _ok("leaderboard mode + include_profile=True → error")


def test_username_mode_returns_rank():
    fake_lookup = {
        "users": [{
            "user_id": 12345, "username": "bankerkyle", "display_name": "BK",
            "trader_rank": 1, "trader_total": 53,
            "racism_rank": 1, "racism_total": 3,
            "trader_rationale": "BK reads as a high-octane gambler",
            "racism_rationale": "BK uses racial slurs",
        }],
        "count": 1,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "bankerkyle"}))
    assert "users" in result and len(result["users"]) == 1, f"expected one user, got {result}"
    user = result["users"][0]
    assert user.get("username") == "bankerkyle", f"unexpected user: {user}"
    assert "trader_rationale" in user, f"missing trader_rationale: {user}"
    assert "profile_text" not in user, (
        "profile_text should NOT be in result when include_profile=False"
    )
    _ok("username mode returns rank + rationales (no profile_text)")


def test_username_mode_with_include_profile():
    fake_lookup = {
        "users": [{
            "user_id": 12345, "username": "bankerkyle", "display_name": "BK",
            "trader_rank": 1, "trader_total": 53,
            "racism_rank": 1, "racism_total": 3,
            "trader_rationale": "rat",
            "racism_rationale": "rat",
        }],
        "count": 1,
    }
    fake_dossier = "**Personality and style.** BK is loud."
    with (
        patch("db.lookup_user_ranks", return_value=fake_lookup),
        patch("db.format_user_profiles_for_context", return_value=fake_dossier),
    ):
        result = asyncio.run(bot_mod._execute_user_profile({
            "username": "bankerkyle", "include_profile": True,
        }))
    user = result["users"][0]
    assert "profile_text" in user, f"profile_text missing: {user}"
    assert "Personality" in user["profile_text"], "dossier not surfaced"
    _ok("username mode + include_profile=True returns dossier")


def test_metric_and_rank_position_mode():
    fake_lookup = {
        "users": [{
            "user_id": 67890, "username": "abullish_xyz", "display_name": "abe",
            "trader_rank": 2, "trader_total": 53,
            "racism_rank": 2, "racism_total": 3,
            "trader_rationale": "abe is the engine",
            "racism_rationale": "abe weaves slurs",
        }],
        "count": 1,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({
            "metric": "trader", "rank_position": 2,
        }))
    assert len(result.get("users", [])) == 1, f"expected one user, got {result}"
    _ok("metric + rank_position returns one user at that rank")


def test_metric_only_leaderboard_mode():
    fake_lookup = {
        "users": [
            {"user_id": 1, "username": "u1", "display_name": "U1",
             "trader_rank": 1, "trader_total": 53,
             "trader_rationale": "r1", "racism_rationale": "rr1"},
            {"user_id": 2, "username": "u2", "display_name": "U2",
             "trader_rank": 2, "trader_total": 53,
             "trader_rationale": "r2", "racism_rationale": "rr2"},
            {"user_id": 3, "username": "u3", "display_name": "U3",
             "trader_rank": 3, "trader_total": 53,
             "trader_rationale": "r3", "racism_rationale": "rr3"},
            {"user_id": 4, "username": "u4", "display_name": "U4",
             "trader_rank": 4, "trader_total": 53,
             "trader_rationale": "r4", "racism_rationale": "rr4"},
            {"user_id": 5, "username": "u5", "display_name": "U5",
             "trader_rank": 5, "trader_total": 53,
             "trader_rationale": "r5", "racism_rationale": "rr5"},
        ],
        "count": 5,
    }
    with patch("db.lookup_user_ranks", return_value=fake_lookup):
        result = asyncio.run(bot_mod._execute_user_profile({"metric": "trader"}))
    assert len(result.get("users", [])) == 5, f"expected 5 users, got {result}"
    _ok("metric-only mode returns top 5 leaderboard")


def test_old_lookup_user_ranks_removed():
    """The old tool def and executor should be fully gone from bot.py."""
    assert not hasattr(bot_mod, "_build_user_ranks_tool"), (
        "_build_user_ranks_tool should be removed (replaced by _build_user_profile_tool)"
    )
    assert not hasattr(bot_mod, "_execute_user_ranks"), (
        "_execute_user_ranks should be removed (replaced by _execute_user_profile)"
    )
    _ok("old _build_user_ranks_tool / _execute_user_ranks are gone")


if __name__ == "__main__":
    print("=== lookup_user_profile tool smoke ===")
    test_tool_definition_shape()
    test_no_anchor_error()
    test_both_anchors_error()
    test_leaderboard_plus_include_profile_error()
    test_username_mode_returns_rank()
    test_username_mode_with_include_profile()
    test_metric_and_rank_position_mode()
    test_metric_only_leaderboard_mode()
    test_old_lookup_user_ranks_removed()
    print("\nALL USER-PROFILE-TOOL SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_user_profile_tool.py
```

Expected: FAIL with `AttributeError: module 'discord_bot.bot' has no attribute '_build_user_profile_tool'`.

- [ ] **Step 3: Add `_build_user_profile_tool` to `discord_bot/bot.py`**

Insert this function in `discord_bot/bot.py` immediately BEFORE the existing `def _build_user_ranks_tool():` (find via `grep -n "^def _build_user_ranks_tool" discord_bot/bot.py`):

```python
def _build_user_profile_tool():
    """FunctionDeclaration for `lookup_user_profile`. Unifies the three
    modes from the legacy `lookup_user_ranks` tool and adds an
    `include_profile` flag that returns the full WHO'S TALKING dossier
    on top of rank + rationales.

    Anchors (exactly one required):
      - username: specific user
      - metric: "trader" | "racism" — leaderboard or rank_position lookup
      - metric + rank_position: the ONE user at that rank position

    include_profile=True: also include the user's full profile_text
    (Personality + Voice + Retarded Takes + Recent Personal Life +
    Recent Trades). Rejected in leaderboard mode (5 dossiers too big).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_user_profile",
                description=(
                    "Look up rank + optional full profile for a "
                    "Discord room member. Three anchor shapes "
                    "(use EXACTLY one):\n"
                    "(a) `username` set → returns that user's "
                    "trader-rank, racism-rank, and both rationales. "
                    "Use when asker names a specific user.\n"
                    "(b) `metric` ('trader' or 'racism') + "
                    "`rank_position` (positive integer, no upper "
                    "cap) → returns the ONE user at that rank. Use "
                    "for 'who's #N' questions.\n"
                    "(c) `metric` set with no rank_position → "
                    "returns the TOP 5 leaderboard. Use for "
                    "leaderboard-style asks.\n"
                    "Set `include_profile=true` on (a) or (b) to also "
                    "return the user's full personality dossier "
                    "(Personality + Voice + Retarded Takes + Recent "
                    "Personal Life). Use when the question needs "
                    "personality / voice / personal context. Rejected "
                    "in leaderboard mode (5 dossiers too big).\n"
                    "Never quote raw 0-100 scores."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description="Discord username (lowercase, no @). Mode (a).",
                        ),
                        "metric": types.Schema(
                            type=types.Type.STRING,
                            description="'trader' or 'racism'. Modes (b) and (c).",
                        ),
                        "rank_position": types.Schema(
                            type=types.Type.INTEGER,
                            description="1-based rank position. Mode (b) only.",
                        ),
                        "include_profile": types.Schema(
                            type=types.Type.BOOLEAN,
                            description=(
                                "When true, also return the user's full "
                                "profile dossier. Rejected in mode (c)."
                            ),
                        ),
                    },
                ),
            )
        ]
    )
```

- [ ] **Step 4: Add `_execute_user_profile` to `discord_bot/bot.py`**

Insert this function in `discord_bot/bot.py` immediately BEFORE the existing `async def _execute_user_ranks(args: dict)` (find via `grep -n "^async def _execute_user_ranks" discord_bot/bot.py`):

```python
async def _execute_user_profile(args: dict) -> dict:
    """Run the lookup_user_profile tool call.

    Validates anchor exclusivity, delegates rank lookup to
    db.lookup_user_ranks (same query the legacy tool used), then
    optionally enriches each returned user with their full profile
    dossier via db.format_user_profiles_for_context.
    """
    username = (args.get("username") or "").strip() or None
    metric = (args.get("metric") or "").strip() or None
    rank_position = args.get("rank_position")
    include_profile = bool(args.get("include_profile"))
    from_bottom = bool(args.get("from_bottom"))

    # Validation: exactly one anchor.
    if username and metric:
        return {
            "error": (
                "Provide exactly one anchor: either `username` "
                "(specific user), or `metric` ('trader'/'racism') "
                "with optional `rank_position`."
            ),
            "users": [],
        }
    if not username and not metric:
        return {
            "error": (
                "Must provide either `username` (single user), or "
                "`metric` ('trader' / 'racism'), optionally with "
                "`rank_position` for a specific #N lookup."
            ),
            "users": [],
        }

    # Leaderboard mode rejects include_profile (5 dossiers too big).
    if metric and rank_position is None and include_profile:
        return {
            "error": (
                "include_profile is not supported in leaderboard mode "
                "(5 dossiers is too large). Ask for a specific user "
                "with `username=<...>, include_profile=true` instead, "
                "or for a single ranked user via `metric=<...>, "
                "rank_position=N, include_profile=true`."
            ),
            "users": [],
        }

    try:
        result = db.lookup_user_ranks(
            username=username,
            metric=metric,
            rank_position=rank_position,
            top_n=5,
            from_bottom=from_bottom,
        )
    except Exception as e:
        log.warning(f"lookup_user_profile rank lookup failed: {e}")
        return {"error": f"rank lookup failed: {type(e).__name__}: {e}", "users": []}

    if "error" in result:
        return result

    # If include_profile=True, enrich each returned user with their
    # full dossier. format_user_profiles_for_context handles missing
    # profiles gracefully (returns "" for users without a profile row).
    if include_profile and result.get("users"):
        for user in result["users"]:
            uid = user.get("user_id")
            if not uid:
                continue
            try:
                dossier = db.format_user_profiles_for_context([int(uid)])
            except Exception as e:
                log.warning(
                    f"lookup_user_profile dossier fetch failed for user_id={uid}: {e}"
                )
                dossier = ""
            user["profile_text"] = (dossier or "").strip()

    return result
```

- [ ] **Step 5: Wire the tool into both tools lists + dispatch loop**

In `discord_bot/bot.py`, locate the main `tools=[...]` list inside the GenerateContentConfig at around line 2076 (find via `grep -n "_build_chat_search_tool()" discord_bot/bot.py`). Add `_build_user_profile_tool()` to the list alongside the existing tools. Same for the retry_config tools list around line 2291.

Then in the tool-call dispatch loop (find via `grep -n 'elif fc.name == "lookup_user_ranks"' discord_bot/bot.py`), add a new dispatch case BEFORE the `lookup_user_ranks` case:

```python
                elif fc.name == "lookup_user_profile":
                    result = await _execute_user_profile(args)
                    tool_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"result": result},
                        )
                    )
```

- [ ] **Step 6: Remove old `_build_user_ranks_tool`, `_execute_user_ranks`, and their wiring**

- Delete the entire `def _build_user_ranks_tool():` function and body.
- Delete the entire `async def _execute_user_ranks(args: dict):` function and body.
- Remove `_build_user_ranks_tool()` from both tools lists (main config + retry_config).
- Remove the `elif fc.name == "lookup_user_ranks":` dispatch case from the tool-call loop.

- [ ] **Step 7: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_user_profile_tool.py
```

Expected: `ALL USER-PROFILE-TOOL SMOKE TESTS PASS`.

- [ ] **Step 8: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 9: Commit**

```
git add discord_bot/bot.py scripts/smoke_user_profile_tool.py
git commit -m "ask: lookup_user_profile tool — replaces lookup_user_ranks"
```

---

## Task 5: Tool — `lookup_trade_log`

**Files:**
- Modify: `discord_bot/bot.py` — add `_build_trade_log_tool()` and `_execute_trade_log()` (near the other tool builders, e.g. after the new `_build_user_profile_tool`)
- Modify: `discord_bot/bot.py` — wire into both tools lists and dispatch loop
- Test: `scripts/smoke_trade_log_tool.py` (create)

Caller anchor: queries `format_analyst_trades_for_context(caller=..., tracking_mode="caller", kind=...)` from Task 1. Username anchor: resolves via `resolve_username_to_user_id` (Task 2), queries `format_analyst_trades_for_context(caller=None, tracking_mode=None, ...)` filtered by `author_id` (requires new lookup path through `get_recent_analyst_trades` etc.), plus extracts the profile snippet via `get_user_profile_recent_trades_section` (Task 3).

**Implementation note for username path:** the existing `format_analyst_trades_for_context` takes `caller`, not `author_id`. For the username path, we'll instead call the underlying primitives directly (`get_recent_analyst_trades`, `get_current_analyst_positions`, `compute_caller_win_loss_summary`) with the resolved `author_id` as a new filter. **OR** simpler: skip the structured-trades branch for username anchor in this first cut, and only return the profile snippet. The spec says both should ideally surface, but a working v1 ships the profile snippet only; we can extend the underlying queries to accept `author_id` in a follow-up if QC shows members do have rows.

For this plan we go with the simpler v1: caller anchor uses the existing path with kind; username anchor returns only the profile snippet. This honors the spec's intent ("most members don't post screenshots in eager-OCR channels") while keeping the change small.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_trade_log_tool.py`:

```python
"""Smoke test for the lookup_trade_log tool.

Validates:
  1. Tool definition has correct name + parameters
  2. _execute_trade_log error modes:
     - no anchor → error
     - both caller and username → error
     - invalid kind → error
  3. Caller anchor:
     - kind="open" calls format_analyst_trades_for_context with kind="open"
     - kind="all" → kind="all" passed through
     - empty data → status="no_logged_trades"
  4. Username anchor:
     - resolves username → user_id, returns profile snippet
     - unresolved → error
     - empty profile section → status="no_logged_trades"
"""

import asyncio
import sys
from unittest.mock import patch, MagicMock

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_definition_shape():
    tool = bot_mod._build_trade_log_tool()
    decls = tool.function_declarations
    assert len(decls) == 1
    decl = decls[0]
    assert decl.name == "lookup_trade_log", f"unexpected name: {decl.name}"
    props = decl.parameters.properties
    for arg in ("caller", "username", "kind", "days"):
        assert arg in props, f"missing parameter {arg!r}"
    _ok("_build_trade_log_tool: name + all 4 parameters present")


def test_no_anchor_error():
    result = asyncio.run(bot_mod._execute_trade_log({}))
    assert "error" in result, f"expected error, got {result}"
    _ok("no anchor → error")


def test_both_anchors_error():
    result = asyncio.run(bot_mod._execute_trade_log({
        "caller": "abe", "username": "bankerkyle",
    }))
    assert "error" in result, f"expected error, got {result}"
    assert "exactly one" in result["error"].lower(), result["error"]
    _ok("both caller and username set → error")


def test_invalid_kind_error():
    result = asyncio.run(bot_mod._execute_trade_log({
        "caller": "abe", "kind": "bogus",
    }))
    assert "error" in result, f"expected error, got {result}"
    assert "kind" in result["error"].lower(), result["error"]
    _ok("invalid kind → error")


def test_caller_anchor_kind_passthrough():
    """The executor passes kind directly through to format_analyst_trades_for_context."""
    fake_text = "ABE'S CURRENTLY OPEN POSITIONS:\n- META 640C 06-05 @6.40"
    with (
        patch(
            "db.format_analyst_trades_for_context",
            return_value=fake_text,
        ) as fmt,
        patch("config.settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    assert fmt.called, "format_analyst_trades_for_context not called"
    call_kwargs = fmt.call_args.kwargs
    assert call_kwargs.get("caller") == "abe", call_kwargs
    assert call_kwargs.get("kind") == "open", call_kwargs
    assert result.get("data_quality") == "caller", result
    assert result.get("trades_text"), result
    _ok("caller anchor passes kind='open' through; data_quality='caller'")


def test_caller_anchor_empty_returns_status():
    with (
        patch("db.format_analyst_trades_for_context", return_value=""),
        patch("config.settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    assert result.get("status") == "no_logged_trades", result
    _ok("caller anchor with empty data → status=no_logged_trades")


def test_username_anchor_returns_profile_snippet():
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch(
            "db.get_user_profile_recent_trades_section",
            return_value="- $PLTR / 145C — closed +911%",
        ),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "username": "theorb_18574", "kind": "recent",
        }))
    assert result.get("data_quality") == "member", result
    assert "PLTR" in (result.get("profile_recent_trades") or ""), result
    _ok("username anchor returns profile snippet, data_quality='member'")


def test_username_unresolved_error():
    with patch("db.resolve_username_to_user_id", return_value=None):
        result = asyncio.run(bot_mod._execute_trade_log({"username": "nobody"}))
    assert "error" in result, f"expected error, got {result}"
    assert "not found" in result["error"].lower(), result["error"]
    _ok("username not found → error")


def test_username_anchor_empty_returns_status():
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch("db.get_user_profile_recent_trades_section", return_value=""),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({"username": "newjoiner"}))
    assert result.get("status") == "no_logged_trades", result
    _ok("username anchor with empty data → status=no_logged_trades")


if __name__ == "__main__":
    print("=== lookup_trade_log tool smoke ===")
    test_tool_definition_shape()
    test_no_anchor_error()
    test_both_anchors_error()
    test_invalid_kind_error()
    test_caller_anchor_kind_passthrough()
    test_caller_anchor_empty_returns_status()
    test_username_anchor_returns_profile_snippet()
    test_username_unresolved_error()
    test_username_anchor_empty_returns_status()
    print("\nALL TRADE-LOG-TOOL SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_trade_log_tool.py
```

Expected: FAIL with `AttributeError: module 'discord_bot.bot' has no attribute '_build_trade_log_tool'`.

- [ ] **Step 3: Add `_build_trade_log_tool` to `discord_bot/bot.py`**

Insert immediately after `_build_user_profile_tool` (added in Task 4):

```python
def _build_trade_log_tool():
    """FunctionDeclaration for `lookup_trade_log`. Works for two anchors:

    - `caller`: a registered analyst caller (e.g. 'abe', 'bankerkyle').
      Queries analyst_trades caller-mode rows. High fidelity — daily
      cron stitches open/close pairs. Returns ONLY the log data.

    - `username`: any Discord username. Resolves to user_id and pulls
      the 'Recent trades' section from that user's profile. Member-mode
      data quality — no per-trade stitching. (Member-mode rows in
      analyst_trades are not surfaced in this v1; add when member-row
      lookup is needed.)

    Exactly one anchor must be provided. `kind` ∈ {open, recent, tally, all}
    slices the response on the caller path. `days` overrides defaults
    (7 for kind=recent, 30 for kind=tally; ignored for kind=open).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_trade_log",
                description=(
                    "Look up trade history. Two anchors (use EXACTLY one):\n"
                    "(a) `caller`: registered analyst caller name "
                    "('abe', 'bankerkyle', ...). Returns structured "
                    "trade log with daily-cron-stitched W/L. Use for "
                    "Abe / BK specifically — their data is high "
                    "fidelity.\n"
                    "(b) `username`: any other Discord username. "
                    "Returns the user's 'Recent trades' snippet from "
                    "their profile. Member-mode fidelity (no W/L "
                    "stitching). Use for non-caller users.\n"
                    "`kind` ∈ {'open','recent','tally','all'} (default "
                    "'all'). 'open' = current open positions only. "
                    "'recent' = last N days of trade events. 'tally' = "
                    "W/L summary. 'all' = everything.\n"
                    "`days` overrides defaults (7 for kind=recent, 30 "
                    "for kind=tally; ignored for kind=open / kind=all)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "caller": types.Schema(
                            type=types.Type.STRING,
                            description="Registered caller: 'abe', 'bankerkyle', ...",
                        ),
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description="Any other Discord username.",
                        ),
                        "kind": types.Schema(
                            type=types.Type.STRING,
                            description="open | recent | tally | all (default all)",
                        ),
                        "days": types.Schema(
                            type=types.Type.INTEGER,
                            description="Window override in days (1..180).",
                        ),
                    },
                ),
            )
        ]
    )
```

- [ ] **Step 4: Add `_execute_trade_log` to `discord_bot/bot.py`**

Insert immediately after `_execute_user_profile` (added in Task 4):

```python
async def _execute_trade_log(args: dict) -> dict:
    """Run the lookup_trade_log tool call.

    Caller path: queries db.format_analyst_trades_for_context with the
    requested kind, returns the rendered block.

    Username path: resolves username → user_id, pulls the Recent trades
    snippet from the user's profile. (Member-mode rows in analyst_trades
    are not joined in v1; defer until QC shows it's worth the SQL
    layer change.)
    """
    caller = (args.get("caller") or "").strip() or None
    username = (args.get("username") or "").strip() or None
    kind = (args.get("kind") or "all").strip().lower()
    days_arg = args.get("days")

    # Validation: anchor exclusivity.
    if caller and username:
        return {
            "error": (
                "Provide exactly one of `caller` or `username` "
                "(got both)."
            ),
        }
    if not caller and not username:
        return {
            "error": (
                "Provide exactly one of `caller` (registered "
                "analyst — 'abe', 'bankerkyle', ...) or `username` "
                "(any other Discord username)."
            ),
        }
    if kind not in ("all", "open", "recent", "tally"):
        return {
            "error": f"`kind` must be one of: all, open, recent, tally; got {kind!r}.",
        }

    # Default windows per kind.
    if kind == "recent":
        days = int(days_arg) if days_arg else 7
    elif kind == "tally":
        days = int(days_arg) if days_arg else 30
    else:
        days = 7  # placeholder for kind=open/all (ignored by the formatter)
    days = max(1, min(180, days))

    # --- CALLER ANCHOR ---
    if caller:
        # Resolve display name from the configured caller registry.
        display = None
        try:
            for c in settings.resolve_analyst_callers():
                if c.get("name", "").lower() == caller.lower():
                    display = c.get("display") or caller.title()
                    break
        except Exception as e:
            log.warning(f"lookup_trade_log caller registry lookup failed: {e}")
        display = display or caller.title()
        try:
            text = db.format_analyst_trades_for_context(
                hours=days * 24,
                caller=caller.lower(),
                display=display,
                tracking_mode="caller",
                kind=kind,
            )
        except ValueError as e:
            return {"error": str(e)}
        except Exception as e:
            log.warning(f"lookup_trade_log caller path failed: {e}")
            return {"error": f"{type(e).__name__}: {e}"}
        if not text:
            return {
                "anchor": {"type": "caller", "name": caller},
                "kind": kind,
                "data_quality": "caller",
                "status": "no_logged_trades",
            }
        return {
            "anchor": {"type": "caller", "name": caller, "display": display},
            "kind": kind,
            "data_quality": "caller",
            "trades_text": text,
        }

    # --- USERNAME ANCHOR ---
    user_id = db.resolve_username_to_user_id(username)
    if user_id is None:
        return {"error": f"username {username!r} not found in profiles or chat history."}
    try:
        profile_snippet = db.get_user_profile_recent_trades_section(user_id)
    except Exception as e:
        log.warning(f"lookup_trade_log profile snippet fetch failed: {e}")
        profile_snippet = ""
    if not profile_snippet:
        return {
            "anchor": {"type": "username", "name": username, "user_id": user_id},
            "kind": kind,
            "data_quality": "member",
            "status": "no_logged_trades",
        }
    return {
        "anchor": {"type": "username", "name": username, "user_id": user_id},
        "kind": kind,
        "data_quality": "member",
        "profile_recent_trades": profile_snippet,
    }
```

- [ ] **Step 5: Wire into both tools lists + dispatch loop**

In `discord_bot/bot.py`:

1. Locate the main config `tools=[...]` list (around line 2076). Add `_build_trade_log_tool()` to the list.
2. Same in the retry_config `tools=[...]` list (around line 2291).
3. Locate the tool-call dispatch loop (around line 2200, alongside `elif fc.name == "..."` branches). Add this case AFTER the `lookup_user_profile` case:

```python
                elif fc.name == "lookup_trade_log":
                    result = await _execute_trade_log(args)
                    tool_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"result": result},
                        )
                    )
```

- [ ] **Step 6: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_trade_log_tool.py
```

Expected: `ALL TRADE-LOG-TOOL SMOKE TESTS PASS`.

- [ ] **Step 7: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 8: Commit**

```
git add discord_bot/bot.py scripts/smoke_trade_log_tool.py
git commit -m "ask: lookup_trade_log tool — caller + username anchors"
```

---

## Task 6: Tool — `lookup_market_price`

**Files:**
- Modify: `discord_bot/bot.py` — add `_build_market_price_tool()` and `_execute_market_price()`
- Modify: `discord_bot/bot.py` — wire into both tools lists and dispatch loop
- Test: `scripts/smoke_market_price_tool.py` (create)

Reuses existing private fetchers in `report/market_data.py`.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_market_price_tool.py`:

```python
"""Smoke test for the lookup_market_price tool.

Validates:
  1. Tool definition shape
  2. Routing: known crypto → Binance; everything else → Finnhub
  3. Validation: empty symbols list → error
  4. Validation: > 10 symbols → truncate with warning
  5. Session field populated from report.market_data._session_label
  6. Per-symbol error doesn't sink the batch
"""

import asyncio
import sys
from unittest.mock import patch

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_definition_shape():
    tool = bot_mod._build_market_price_tool()
    decl = tool.function_declarations[0]
    assert decl.name == "lookup_market_price", f"unexpected name: {decl.name}"
    assert "symbols" in decl.parameters.properties
    _ok("_build_market_price_tool: name + symbols param present")


def test_empty_symbols_error():
    result = asyncio.run(bot_mod._execute_market_price({"symbols": []}))
    assert "error" in result, f"expected error, got {result}"
    _ok("empty symbols → error")


def test_missing_symbols_error():
    result = asyncio.run(bot_mod._execute_market_price({}))
    assert "error" in result, f"expected error, got {result}"
    _ok("missing symbols → error")


def test_finnhub_route_for_stock():
    finnhub_data = {"price": 598.42, "prev_close": 596.40, "change_pct": 0.34}
    with (
        patch("report.market_data._fetch_finnhub_quote", return_value=finnhub_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["SPY"]}))
    assert result.get("session") == "OPEN", result
    quote = result["quotes"][0]
    assert quote["symbol"] == "SPY", quote
    assert quote.get("source") == "finnhub", quote
    assert quote.get("price") == 598.42, quote
    _ok("stock symbol routes to Finnhub")


def test_binance_route_for_crypto():
    binance_data = {"price": 109423.10, "change_24h_rolling": -1.2}
    with (
        patch("report.market_data._fetch_binance_24h", return_value=binance_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({"symbols": ["BTC"]}))
    quote = result["quotes"][0]
    assert quote["symbol"] == "BTC", quote
    assert quote.get("source") == "binance", quote
    assert quote.get("price") == 109423.10, quote
    _ok("crypto symbol routes to Binance.US")


def test_per_symbol_error_does_not_sink_batch():
    finnhub_data = {"price": 598.42, "prev_close": 596.40, "change_pct": 0.34}
    with (
        # Finnhub returns None for unknown symbol
        patch(
            "report.market_data._fetch_finnhub_quote",
            side_effect=lambda sym: finnhub_data if sym == "SPY" else None,
        ),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({
            "symbols": ["SPY", "ASDF"],
        }))
    quotes = result["quotes"]
    assert len(quotes) == 2, quotes
    assert quotes[0]["symbol"] == "SPY" and "price" in quotes[0]
    assert quotes[1]["symbol"] == "ASDF" and "error" in quotes[1]
    _ok("unknown symbol gets per-symbol error; other symbols still resolve")


def test_truncate_over_ten():
    finnhub_data = {"price": 1.0, "prev_close": 1.0, "change_pct": 0.0}
    with (
        patch("report.market_data._fetch_finnhub_quote", return_value=finnhub_data),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({
            "symbols": [f"SYM{i}" for i in range(15)],
        }))
    assert len(result["quotes"]) == 10, f"expected 10 quotes, got {len(result['quotes'])}"
    assert result.get("truncated_to") == 10, result
    _ok("> 10 symbols → truncated to 10 with warning field")


if __name__ == "__main__":
    print("=== lookup_market_price tool smoke ===")
    test_tool_definition_shape()
    test_empty_symbols_error()
    test_missing_symbols_error()
    test_finnhub_route_for_stock()
    test_binance_route_for_crypto()
    test_per_symbol_error_does_not_sink_batch()
    test_truncate_over_ten()
    print("\nALL MARKET-PRICE-TOOL SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_market_price_tool.py
```

Expected: FAIL with `AttributeError: module 'discord_bot.bot' has no attribute '_build_market_price_tool'`.

- [ ] **Step 3: Add `_build_market_price_tool` and `_execute_market_price` to `discord_bot/bot.py`**

Insert immediately after `_execute_trade_log` (added in Task 5):

```python
# Hardcoded crypto symbol allowlist. Extensible — append symbols here
# as crypto questions surface them. Anything not in this set routes
# to Finnhub (stocks/ETFs/indices).
_CRYPTO_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "ADA", "AVAX", "MATIC", "XRP", "BNB", "LINK",
})


def _build_market_price_tool():
    """FunctionDeclaration for `lookup_market_price`. Routes symbols
    to Finnhub (stocks) or Binance.US (crypto) based on a hardcoded
    allowlist. Returns a session-labeled snapshot."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_market_price",
                description=(
                    "Get live prices for stocks / ETFs / indices "
                    "(via Finnhub) and crypto (via Binance.US). Pass "
                    "a list of symbols. Response includes per-symbol "
                    "price, change_pct, source, plus a session label "
                    "('OPEN' | 'PRE-MARKET' | 'AFTER-HOURS' | "
                    "'WEEKEND-CLOSED') so you phrase the move "
                    "correctly. Crypto trades 24/7 — its move is "
                    "always today's. Cap of 10 symbols per call. "
                    "Use for 'what's TSLA at', 'how's BTC doing', "
                    "'is SPY green today'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbols": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Symbol tickers, e.g. ['TSLA', 'BTC'].",
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_market_price(args: dict) -> dict:
    """Run the lookup_market_price tool call.

    Routes each symbol to Finnhub or Binance.US, collects per-symbol
    responses, prepends a session label so the model can phrase
    correctly. Per-symbol failures don't sink the batch.
    """
    from datetime import datetime
    import pytz
    from report import market_data as _md

    symbols_in = args.get("symbols")
    if not symbols_in or not isinstance(symbols_in, list):
        return {"error": "symbols list cannot be empty"}

    symbols = [str(s).strip().upper() for s in symbols_in if isinstance(s, str) and str(s).strip()]
    if not symbols:
        return {"error": "symbols list cannot be empty"}

    truncated_to = None
    if len(symbols) > 10:
        symbols = symbols[:10]
        truncated_to = 10

    # Session label from existing market_data helper.
    et = pytz.timezone("America/New_York")
    now_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(et)
    try:
        session_code, _note = _md._session_label(now_et)
    except Exception:
        session_code = "UNKNOWN"
    timestamp = now_et.strftime("%Y-%m-%d %H:%M %Z")

    quotes: list[dict] = []
    for sym in symbols:
        if sym in _CRYPTO_SYMBOLS:
            try:
                data = _md._fetch_binance_24h(f"{sym}USDT")
            except Exception as e:
                quotes.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
                continue
            if not data:
                quotes.append({"symbol": sym, "error": "no quote returned"})
                continue
            quotes.append({
                "symbol": sym,
                "price": data.get("price"),
                "change_pct": data.get("change_24h_rolling"),
                "prev_close": None,
                "source": "binance",
            })
        else:
            try:
                data = _md._fetch_finnhub_quote(sym)
            except Exception as e:
                quotes.append({"symbol": sym, "error": f"{type(e).__name__}: {e}"})
                continue
            if not data:
                quotes.append({"symbol": sym, "error": "symbol not found"})
                continue
            quotes.append({
                "symbol": sym,
                "price": data.get("price"),
                "change_pct": data.get("change_pct"),
                "prev_close": data.get("prev_close"),
                "source": "finnhub",
            })

    result = {
        "session": session_code,
        "timestamp": timestamp,
        "quotes": quotes,
    }
    if truncated_to is not None:
        result["truncated_to"] = truncated_to
    return result
```

- [ ] **Step 4: Wire into both tools lists + dispatch loop**

1. Add `_build_market_price_tool()` to the main config `tools=[...]` list.
2. Add to the retry_config `tools=[...]` list.
3. Add this dispatch case to the tool-call loop after `lookup_trade_log`:

```python
                elif fc.name == "lookup_market_price":
                    result = await _execute_market_price(args)
                    tool_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"result": result},
                        )
                    )
```

- [ ] **Step 5: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_market_price_tool.py
```

Expected: `ALL MARKET-PRICE-TOOL SMOKE TESTS PASS`.

- [ ] **Step 6: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 7: Commit**

```
git add discord_bot/bot.py scripts/smoke_market_price_tool.py
git commit -m "ask: lookup_market_price tool — stocks via Finnhub, crypto via Binance"
```

---

## Task 7: Extend `search_chat_messages` — `keyword` is optional

**Files:**
- Modify: `discord_bot/bot.py` — update `_build_chat_search_tool` description (around line 1317)
- Modify: `discord_bot/bot.py` — update `_execute_chat_search` to allow keyword-omitted calls when `username` or `channel_name` is set (around line 1436)
- Test: `scripts/smoke_chat_search_keyword_optional.py` (create)

Today the executor errors when both `keyword` and `(start_iso, end_iso)` are absent. We add a third shape: username + days (with no keyword) returns that user's recent messages within the window.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_chat_search_keyword_optional.py`:

```python
"""Smoke test for the keyword-optional extension of search_chat_messages.

Validates:
  1. Username + days (no keyword, no time window) is now accepted
  2. Channel + days (no keyword, no username) is accepted
  3. Username + keyword still works as before
  4. start_iso + end_iso shape still works as before
  5. Nothing at all (no keyword, no username, no channel, no window) → error
"""

import asyncio
import sys
from unittest.mock import patch

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_username_days_no_keyword_works():
    """The new shape: 'what has Kyle been crying about today' pattern."""
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "bankerkyle",
         "channel": "stonks-yapping", "content": "fk me"},
    ]
    with patch("db.search_chat_messages", return_value=fake_matches) as mock_search:
        result = asyncio.run(bot_mod._execute_chat_search({
            "username": "bankerkyle", "days": 1,
        }))
    assert mock_search.called, "db.search_chat_messages not called"
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs.get("username") == "bankerkyle", call_kwargs
    # keyword should be empty/None to signal username-only retrieval
    assert not call_kwargs.get("keyword"), (
        f"expected empty keyword, got {call_kwargs.get('keyword')!r}"
    )
    assert "matches" in result and len(result["matches"]) == 1, result
    _ok("username + days (no keyword) is accepted and queries by user")


def test_channel_days_no_keyword_works():
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "user1",
         "channel": "test", "content": "msg"},
    ]
    with patch("db.search_chat_messages", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "channel_name": "test", "days": 1,
        }))
    assert "matches" in result, result
    _ok("channel_name + days (no keyword, no username) is accepted")


def test_keyword_still_works():
    """Regression: existing shape A (keyword search) still works."""
    fake_matches = [
        {"posted_at": "2026-06-01T15:00", "author_username": "bankerkyle",
         "channel": "stonks-yapping", "content": "TSLA looks good"},
    ]
    with patch("db.search_chat_messages", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "keyword": "TSLA", "username": "bankerkyle", "days": 30,
        }))
    assert "matches" in result and len(result["matches"]) == 1, result
    _ok("regression: keyword+username shape still works")


def test_time_window_still_works():
    """Regression: existing shape B (time window) still works."""
    fake_matches = [{"posted_at": "2026-06-01T15:30", "content": "msg"}]
    with patch("db.search_chat_messages_window", return_value=fake_matches), \
         patch("db.search_chat_messages", return_value=fake_matches):
        result = asyncio.run(bot_mod._execute_chat_search({
            "start_iso": "2026-06-01T15:00:00Z",
            "end_iso": "2026-06-01T16:00:00Z",
        }))
    assert "matches" in result, result
    _ok("regression: start_iso + end_iso time-window shape still works")


def test_nothing_at_all_still_errors():
    """No filter of any kind → error (prevents full-table scan)."""
    result = asyncio.run(bot_mod._execute_chat_search({}))
    assert "error" in result, f"expected error for no-filter call, got {result}"
    _ok("no keyword + no window + no username + no channel → error")


if __name__ == "__main__":
    print("=== search_chat_messages keyword-optional smoke ===")
    test_username_days_no_keyword_works()
    test_channel_days_no_keyword_works()
    test_keyword_still_works()
    test_time_window_still_works()
    test_nothing_at_all_still_errors()
    print("\nALL CHAT-SEARCH-KEYWORD-OPTIONAL SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_chat_search_keyword_optional.py
```

Expected: FAIL on `test_username_days_no_keyword_works` — the executor's `if not keyword and not has_window` guard returns an error today.

- [ ] **Step 3: Update `_execute_chat_search` validation in `discord_bot/bot.py`**

Locate the validation block in `_execute_chat_search` (around line 1436) and update it. Find:

```python
    if not keyword and not has_window:
        return {
            "error": (
                "Provide either `keyword` (shape A) or BOTH "
                "`start_iso` AND `end_iso` (shape B). To summarize "
                "a time window with no specific keyword, pass just "
                "the two ISO timestamps."
            ),
            "matches": [],
        }
```

Replace with:

```python
    # Accept three shapes:
    #   A: keyword (optionally + username/channel/days) — keyword search
    #   B: start_iso + end_iso (optionally + keyword/channel) — time window
    #   C: username OR channel_name (no keyword, no window) — recent messages
    #      filtered by user or channel. Closes the "what has Kyle been
    #      crying about today" gap that needed keyword-invention before.
    username = (args.get("username") or "").strip() or None
    channel_name = (args.get("channel_name") or "").strip() or None
    if not keyword and not has_window and not username and not channel_name:
        return {
            "error": (
                "Provide at least one of: `keyword` (shape A), BOTH "
                "`start_iso` AND `end_iso` (shape B), or `username`/"
                "`channel_name` (shape C — recent messages by user / "
                "in channel)."
            ),
            "matches": [],
        }
```

Then below in the same function, locate where `username` and `channel_name` are extracted (they're already used). Important: do not double-extract — find the existing variable assignments and ensure they're above the new check. The clean structure: extract `username` and `channel_name` near the top alongside the other args, then validate.

In practice the function already does these extractions further down. Move them up so they're available for the validation. Net effect: read the current function and make sure the variable bindings happen before the new shape-C validation.

Also update the tool description in `_build_chat_search_tool` to document shape C. Find the description string starting around line 1326 and add a new paragraph describing shape C after the shape-B paragraph:

```python
                    "(C) USER / CHANNEL retrieval — pass `username` "
                    "OR `channel_name` (no `keyword`, no `start_iso`/"
                    "`end_iso`). Returns recent messages from that "
                    "user or in that channel within the trailing "
                    "`days` window (default 30, max 180). Use for "
                    "'what has Kyle been crying about today' / "
                    "'recap recent messages in #stonks-yapping' — "
                    "questions about a person or channel's recent "
                    "activity that don't have a specific keyword.\n"
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_chat_search_keyword_optional.py
```

Expected: `ALL CHAT-SEARCH-KEYWORD-OPTIONAL SMOKE TESTS PASS`.

- [ ] **Step 5: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```
git add discord_bot/bot.py scripts/smoke_chat_search_keyword_optional.py
git commit -m "ask: search_chat_messages keyword is now optional"
```

---

## Task 8: Drop `find_users_mentioned_in_text` profile triggers

**Files:**
- Modify: `discord_bot/bot.py` — slash command /ask path (around line 3500-3510)
- Modify: `discord_bot/bot.py` — @mention handler path (around line 3625-3635)
- Modify: `discord_bot/bot.py` — reply-parent name-mention scan (around lines 3698-3713)
- Test: `scripts/smoke_profile_scope_narrowed.py` (create)

Narrows `profile_ids` to strictly asker + Discord @-mention + reply/forward author. `mentioned_ids` for the subject-verbatim block stays untouched — only the propagation into `profile_ids` is removed.

- [ ] **Step 1: Write the smoke test**

Create `scripts/smoke_profile_scope_narrowed.py`:

```python
"""Static smoke test that the narrowed profile-auto-load scope is in effect.

Validates:
  1. The slash command /ask path no longer pipes
     find_users_mentioned_in_text results into profile_ids
  2. The @mention handler path no longer pipes
     find_users_mentioned_in_text results into profile_ids
  3. The reply-parent name-mention scan no longer mutates profile_ids
  4. mentioned_ids extraction is still present (subject-verbatim block
     still gets to use literal name matches)
"""

import inspect
import sys
import re

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Pull the entire bot.py source so we can scan it as text.
SRC = inspect.getsource(bot_mod)


def test_slash_path_no_name_mention_in_profile_ids():
    """The slash command handler builds profile_ids without the
    find_users_mentioned_in_text result."""
    # The post-change profile_ids assembly should match: [user_id] +
    # message.mentions + ([ref_uid] if ref_uid). Look for any line
    # that combines mentioned_ids INTO profile_ids in either /ask path.
    # We scan for the deliberate pattern + assert absence of merge.
    profile_id_assigns = re.findall(
        r"profile_ids\s*=\s*list\(set\([^)]+\)\)", SRC
    )
    assert profile_id_assigns, "no profile_ids = list(set(...)) assignment found"
    for assign in profile_id_assigns:
        # mentioned_ids should NOT appear in any of these unions
        assert "mentioned_ids" not in assign, (
            f"profile_ids assignment still pulls mentioned_ids: {assign!r}"
        )
    _ok("no profile_ids = list(set(...)) assignment pulls mentioned_ids in")


def test_reply_parent_name_scan_no_longer_mutates_profile_ids():
    """The reply-parent ref_content scan should NOT add to profile_ids."""
    # Look for the specific pattern that was removed: a profile_ids =
    # list(set(profile_ids + [uid])) call inside a ref_content loop.
    # If present, fail.
    bad_pattern = re.search(
        r"ref_content[^=]+?find_users_mentioned_in_text\([^)]+\)"
        r"[\s\S]*?profile_ids\s*=\s*list\(set\(profile_ids",
        SRC,
    )
    assert bad_pattern is None, (
        "reply-parent name scan still mutates profile_ids "
        f"(matched: {bad_pattern.group(0)[:200] if bad_pattern else None!r})"
    )
    _ok("reply-parent ref_content scan does NOT propagate into profile_ids")


def test_mentioned_ids_still_extracted():
    """Subject-verbatim should still work — mentioned_ids extraction
    via find_users_mentioned_in_text(question) stays."""
    assert (
        "find_users_mentioned_in_text(question)" in SRC
    ), "find_users_mentioned_in_text(question) call is gone — would break subject-verbatim"
    _ok("find_users_mentioned_in_text(question) still called (for subject-verbatim)")


if __name__ == "__main__":
    print("=== profile-scope-narrowed static smoke ===")
    test_slash_path_no_name_mention_in_profile_ids()
    test_reply_parent_name_scan_no_longer_mutates_profile_ids()
    test_mentioned_ids_still_extracted()
    print("\nALL PROFILE-SCOPE SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
```

Expected: FAIL on `test_slash_path_no_name_mention_in_profile_ids` — current code passes `mentioned_ids` into the union.

- [ ] **Step 3: Edit slash command path (~line 3504)**

Locate via `grep -n "profile_ids = list(set(" discord_bot/bot.py`. The first hit (around line 3507) is the slash command path. Today it reads:

```python
            profile_ids = list(set(
                ([user_id] if user_id else []) + mentioned_ids
            ))
```

Replace with:

```python
            # Profile auto-load scope: asker only at the slash command
            # entry. Discord @-mentions and reply/forward authors are
            # added in the @mention handler path; slash command has
            # neither.
            profile_ids = list(set([user_id] if user_id else []))
```

- [ ] **Step 4: Edit @mention handler path (~line 3633)**

The second `profile_ids = list(set(` hit (around line 3633) is the @mention handler. Today:

```python
                profile_ids = list(set(
                    [message.author.id] + mentioned_ids
                ))
```

Replace with:

```python
                # Profile auto-load scope: asker + Discord first-class
                # @-mentions + reply/forward author. Literal name
                # matches in question text or reply-parent text no
                # longer trigger profile load — questions about other
                # users go through lookup_user_profile tool calls.
                profile_ids = list(set(
                    [message.author.id]
                    + [u.id for u in (message.mentions or []) if not u.bot]
                ))
```

- [ ] **Step 5: Remove the reply-parent name-mention scan (~lines 3698-3713)**

Locate via `grep -n "FIX A: Subject-detection from reply-parent content" discord_bot/bot.py`. The block runs ~14 lines. Replace the whole block with:

```python
                # FIX A history: previously this block scanned the
                # reply-parent text for literal name matches and merged
                # any hits into both `mentioned_ids` (for subject-verbatim
                # injection) AND `profile_ids` (for WHO'S TALKING).
                # Under the narrowed-scope design (2026-06-01), reply-parent
                # name matches no longer trigger profile auto-load — they
                # go through lookup_user_profile tool calls. We still let
                # them inform mentioned_ids for the cheap subject-verbatim
                # block.
                if ref_content:
                    try:
                        ref_mentioned = db.find_users_mentioned_in_text(
                            ref_content
                        )
                        for uid in ref_mentioned:
                            if uid != message.author.id and (
                                bot.user is None or uid != bot.user.id
                            ):
                                if uid not in mentioned_ids:
                                    mentioned_ids.append(uid)
                    except Exception as e:
                        log.warning(
                            f"Reply-parent name-mention lookup failed: {e}"
                        )
```

The key change: the `profile_ids = list(set(profile_ids + [uid]))` line is gone. `mentioned_ids` still picks up name-mentions for subject-verbatim.

Also drop the very next block (around line 3684) that adds `ref_uid` profile to context but ONLY through the existing pattern — actually that block stays (the @mention handler does want the reply-parent author auto-loaded). Find:

```python
                # Add the original author to profile context so the bot
                # has their personality dossier (e.g. forwarding someone
                # crying → bot can address them by name + voice).
                if ref_uid and ref_uid != message.author.id:
                    profile_ids = list(set(profile_ids + [ref_uid]))
```

Keep this block as-is. The reply/forward AUTHOR is one of the strictly-committed three.

- [ ] **Step 6: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
```

Expected: `ALL PROFILE-SCOPE SMOKE TESTS PASS`.

- [ ] **Step 7: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 8: Commit**

```
git add discord_bot/bot.py scripts/smoke_profile_scope_narrowed.py
git commit -m "ask: narrow profile auto-load to asker + @-tag + reply/forward author"
```

---

## Task 9: Drop `analyst_block` from `_answer_with_gemini` prompt assembly

**Files:**
- Modify: `discord_bot/bot.py` — `_answer_with_gemini` (around lines 2110-2138)

The analyst trade context now comes via `lookup_trade_log` tool call.

- [ ] **Step 1: Extend the existing smoke test in `scripts/smoke_profile_scope_narrowed.py`**

Add this test function near the others (insert before `if __name__ == "__main__":`):

```python
def test_analyst_block_not_assembled():
    """_answer_with_gemini should no longer build the analyst_block."""
    src = inspect.getsource(bot_mod._answer_with_gemini)
    # The old assembly had this exact loop structure
    assert "analyst_blocks: list[str] = []" not in src, (
        "_answer_with_gemini still builds analyst_blocks — should be removed"
    )
    assert "analyst_block = " not in src or "analyst_block = " in src.split("\"\"\"")[1], (
        "_answer_with_gemini still assigns analyst_block — should be removed"
    )
    # And the sections list should no longer include analyst_block
    assert "sections.append(analyst_block)" not in src, (
        "_answer_with_gemini still appends analyst_block to sections"
    )
    _ok("_answer_with_gemini no longer assembles analyst_block")
```

And add the call inside the `if __name__ == "__main__":` block:

```python
    test_analyst_block_not_assembled()
```

- [ ] **Step 2: Run test to verify it fails**

```
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
```

Expected: FAIL on `test_analyst_block_not_assembled` (analyst_block still in prompt assembly).

- [ ] **Step 3: Edit `_answer_with_gemini` in `discord_bot/bot.py` (~lines 2110-2138)**

Locate via `grep -n "analyst_blocks: list\[str\] = \[\]" discord_bot/bot.py`. Find the entire `analyst_block` construction starting at:

```python
        # Multi-caller analyst context — emit one separated block per
        # configured caller so /ask sees hard-separated trade logs (no
        # bleeding of one caller's positions into questions about
        # another caller). Empty registry = no analyst context.
        analyst_blocks: list[str] = []
        try:
            for c in settings.resolve_analyst_callers():
                try:
                    block = db.format_analyst_trades_for_context(
                        hours=168,
                        caller=c["name"],
                        display=c["display"],
                    )
                    if block:
                        analyst_blocks.append(block)
                except Exception as e:
                    log.warning(
                        f"Analyst log fetch failed for caller={c.get('name')!r} "
                        f"(non-fatal): {e}"
                    )
        except Exception as e:
            log.warning(f"Analyst caller registry resolve failed (non-fatal): {e}")
        analyst_block = "\n\n".join(analyst_blocks)
```

Delete that entire block.

Then locate the section assembly a few lines below:

```python
        sections: list[str] = []
        if profiles_block:
            sections.append(profiles_block)
        if analyst_block:
            sections.append(analyst_block)
        if fetched_urls:
            sections.append(fetched_urls)
        if chat_context:
            sections.append(chat_context)
```

Remove the `if analyst_block: ... sections.append(analyst_block)` lines:

```python
        sections: list[str] = []
        if profiles_block:
            sections.append(profiles_block)
        if fetched_urls:
            sections.append(fetched_urls)
        if chat_context:
            sections.append(chat_context)
```

- [ ] **Step 4: Run test to verify it passes**

```
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
```

Expected: `ALL PROFILE-SCOPE SMOKE TESTS PASS`.

- [ ] **Step 5: Confirm bot.py still imports clean**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`.

- [ ] **Step 6: Commit**

```
git add discord_bot/bot.py scripts/smoke_profile_scope_narrowed.py
git commit -m "ask: drop analyst_block auto-injection — lookup_trade_log replaces it"
```

---

## Task 10: Wiring static smoke + update `_ASK_SYSTEM_INSTRUCTION`

**Files:**
- Test: `scripts/smoke_tools_wired.py` (create)
- Modify: `discord_bot/bot.py` — update `_ASK_SYSTEM_INSTRUCTION` (line 47+, large prompt string)

The wiring already happened across Tasks 4-7 (each task added its own dispatch case + tools list entries). This task verifies the final state with a single static smoke and updates the system instruction prompt to teach the model when to call each new tool.

- [ ] **Step 1: Write the wiring smoke test**

Create `scripts/smoke_tools_wired.py`:

```python
"""Static smoke test that all 4 new/changed tools are wired into the
tools list of both the main config and the repetition-retry config,
and that lookup_user_ranks is fully gone.

Validates:
  1. _answer_with_gemini source contains all 4 tool-builder calls in
     the main config tools list
  2. Repetition-retry config tools list also contains all 4
  3. lookup_user_ranks references are fully gone from bot.py
  4. The dispatch loop has elif branches for all 4 new tool names
"""

import inspect
import sys
import re

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


SRC = inspect.getsource(bot_mod)


def test_main_config_has_all_tools():
    """The main GenerateContentConfig tools list contains all 4 tools."""
    src = inspect.getsource(bot_mod._answer_with_gemini)
    for builder in (
        "_build_chat_search_tool",
        "_build_user_profile_tool",
        "_build_trade_log_tool",
        "_build_market_price_tool",
    ):
        assert builder in src, f"main config tools list missing {builder}()"
    _ok("main config tools list contains all 4 builders")


def test_no_lookup_user_ranks_anywhere():
    """lookup_user_ranks should be fully removed."""
    # Allow occurrences in db.py call sites (db.lookup_user_ranks still
    # exists as the underlying DB helper — only the tool surface is
    # renamed). So the check is: no _build_user_ranks_tool, no
    # _execute_user_ranks, no "lookup_user_ranks" as a tool name in
    # the dispatch loop.
    assert "_build_user_ranks_tool" not in SRC, (
        "_build_user_ranks_tool still defined / called"
    )
    assert "_execute_user_ranks" not in SRC, (
        "_execute_user_ranks still defined / called"
    )
    # The dispatch loop's quoted tool name should not include the old.
    assert "'lookup_user_ranks'" not in SRC and '"lookup_user_ranks"' not in SRC, (
        "lookup_user_ranks tool name still referenced in dispatch loop"
    )
    _ok("lookup_user_ranks tool surface fully removed")


def test_dispatch_loop_has_all_new_tools():
    """The tool-call dispatch loop should have elif branches for the new tools."""
    for tool_name in (
        "lookup_user_profile",
        "lookup_trade_log",
        "lookup_market_price",
    ):
        pattern = rf'elif fc\.name == "{tool_name}"'
        assert re.search(pattern, SRC), f"dispatch loop missing case for {tool_name!r}"
    _ok("dispatch loop has cases for all 3 new tools")


if __name__ == "__main__":
    print("=== tools-wired static smoke ===")
    test_main_config_has_all_tools()
    test_no_lookup_user_ranks_anywhere()
    test_dispatch_loop_has_all_new_tools()
    print("\nALL TOOLS-WIRED SMOKE TESTS PASS")
```

- [ ] **Step 2: Run the wiring smoke test**

```
$env:PYTHONPATH = "."; py scripts/smoke_tools_wired.py
```

Expected: `ALL TOOLS-WIRED SMOKE TESTS PASS`. (Tasks 4-7 should have already produced this state. If anything fails, fix the wiring in the corresponding earlier task before proceeding.)

- [ ] **Step 3: Update `_ASK_SYSTEM_INSTRUCTION` in `discord_bot/bot.py`**

Locate via `grep -n "_ASK_SYSTEM_INSTRUCTION = " discord_bot/bot.py`. This is a multi-page Python string at line 47+. The prompt has a numbered tool section (look for `### 2.` and `### 3.`). We need three edits:

**Edit 3A:** Find the `### 3. \`lookup_user_ranks(...)\`` section header (around line 89) and rename it to `### 3. \`lookup_user_profile(...)\``. Update the description bullet by bullet so the modes match the new schema:

```markdown
### 3. `lookup_user_profile(username? | metric? + rank_position? | include_profile?)`

Look up a user's rank, rationales, and (optionally) full personality dossier. Three anchor shapes (use exactly one):

**Mode A — named user.** `lookup_user_profile(username="bankerkyle")` returns trader-rank (#N/M), racism-rank (#N/M), trader_rationale, and racism_rationale. Use when the asker names a specific user.

**Mode B — user at a specific rank.** `lookup_user_profile(metric="trader" | "racism", rank_position=N)` returns the ONE user at that rank position (no upper cap on N). Use for "who's #N" questions.

**Mode C — top-5 leaderboard.** `lookup_user_profile(metric="trader" | "racism")` returns the TOP 5 users with names + rationales. Use for leaderboard-style asks: "top 5 traders," "show me the leaderboard," "top racists." `include_profile=true` is REJECTED here (5 dossiers is too big).

**Set `include_profile=true` on Mode A or Mode B** to also receive the user's full WHO'S TALKING dossier (Personality + Voice samples + Retarded Takes + Recent Personal Life). Use this when the question needs personality/voice context that the rank rationale doesn't cover:
- *"What does BK think of TSLA?"* → Mode A + `include_profile=true`, then `search_chat_messages(username="bankerkyle", keyword="TSLA", days=30)`
- *"Why is BK so loud today?"* → Mode A + `include_profile=true`
- *"What has Kyle been crying about today?"* → Mode A + `include_profile=true`, then `search_chat_messages(username="bankerkyle", days=1)` (no keyword)
```

**Edit 3B:** Find the existing reference to "YOU ALREADY HAVE ABE'S RECENT TRADES IN YOUR CONTEXT" / `ABE'S RECENT TRADES` / analyst-block injection mentions. Search for `analyst_block` or `analyst log` in `_ASK_SYSTEM_INSTRUCTION`. Wherever it says the bot has caller trades auto-injected, replace with a `lookup_trade_log` tool reference. Add a new tool section:

```markdown
### 4. `lookup_trade_log(caller? | username?, kind?, days?)`

Look up trade history. Two anchors (use EXACTLY one):

**Caller anchor.** `lookup_trade_log(caller="abe" | "bankerkyle", kind="open" | "recent" | "tally" | "all", days?)` returns the structured trade log for a registered analyst caller. High fidelity — daily cron stitches open/close pairs. `data_quality: "caller"` on the response.

**Username anchor.** `lookup_trade_log(username="theorb_18574", kind="recent", days?)` returns the user's "Recent trades" snippet from their profile. Member-mode fidelity (no W/L stitching). `data_quality: "member"` on the response — hedge the W/L numbers as approximate.

**`kind` modes:**
- `"open"` — current open positions only
- `"recent"` — last N days of trade events (default 7)
- `"tally"` — W/L summary (default 30-day window)
- `"all"` — everything

**Examples:**
- *"What's BK's open book?"* → `lookup_trade_log(caller="bankerkyle", kind="open")`
- *"Did Abe close NVDA today?"* → `lookup_trade_log(caller="abe", kind="recent", days=1)`
- *"Did Abe close Tesla?"* → `lookup_trade_log(caller="abe", kind="recent", days=7)` (scan response for TSLA close events)
- *"How's Abe's win rate?"* → `lookup_trade_log(caller="abe", kind="tally")`
- *"How's Sam's win rate?"* → `lookup_trade_log(username="theorb_18574", kind="tally")` (member-mode — hedge)
- *"How's Terlin's trading lately?"* → `lookup_trade_log(username=".terlin", kind="recent", days=14)`

Hard rule: when the response has `data_quality: "member"`, mention in your answer that the W/L is approximate (small sample, self-reported screenshots).
```

**Edit 3C:** Add a new section for `lookup_market_price`:

```markdown
### 5. `lookup_market_price(symbols)`

Live price + change % for stocks (Finnhub) and crypto (Binance.US). Pass a list of symbols, max 10 per call. Response includes a `session` label so you phrase the move correctly: `"OPEN"` (mid-session), `"PRE-MARKET"` (before 9:30 ET), `"AFTER-HOURS"` (after 4 PM ET), `"WEEKEND-CLOSED"`.

**Examples:**
- *"What's $TSLA at?"* → `lookup_market_price(symbols=["TSLA"])`
- *"How's BTC and ETH today?"* → `lookup_market_price(symbols=["BTC", "ETH"])`
- *"Is $SPY green today?"* → `lookup_market_price(symbols=["SPY"])`
- *"How's $LMT trading?"* → `lookup_market_price(symbols=["LMT"])`

Crypto trades 24/7 — its move is always today's. For traditional markets, the `session` label tells you whether the % is session-to-date (OPEN), yesterday's close left this gap (PRE-MARKET), today's full session (AFTER-HOURS), or Friday's close (WEEKEND-CLOSED). Phrase accordingly.
```

**Edit 3D:** Update the `### 2.` `search_chat_messages` section to mention shape C. Find the section header and the (A)/(B) shape descriptions, then add (C):

```markdown
**Shape C — user / channel retrieval.** Pass `username` OR `channel_name` (no `keyword`, no `start_iso`/`end_iso`). Returns recent messages from that user or in that channel within the trailing `days` window (default 30, max 180). Use when the question is about a person's recent activity but there's no specific keyword to anchor on:
- *"What has Kyle been crying about today?"* → `search_chat_messages(username="bankerkyle", days=1)`
- *"Recap recent messages in #stonks-yapping"* → `search_chat_messages(channel_name="stonks-yapping", days=1)`
```

- [ ] **Step 4: Verify bot.py still imports clean after the prompt edits**

```
$env:PYTHONPATH = "."; py -c "import discord_bot.bot; print('OK')"
```

Expected: `OK`. (Prompt edits are inside a Python string literal — wrong quoting would break the import.)

- [ ] **Step 5: Commit**

```
git add discord_bot/bot.py scripts/smoke_tools_wired.py
git commit -m "ask: update system instruction for 4 new/changed tools"
```

---

## Task 11: Final integration — full smoke suite + manual verification

**Files:** none new. Run existing smoke tests + manual log check.

- [ ] **Step 1: Run the full /ask smoke suite**

```
$env:PYTHONPATH = "."; py scripts/smoke_arch_leak_retry.py
$env:PYTHONPATH = "."; py scripts/smoke_prompt_block_retry.py
$env:PYTHONPATH = "."; py scripts/smoke_batch2.py
$env:PYTHONPATH = "."; py scripts/smoke_format_analyst_trades_kind.py
$env:PYTHONPATH = "."; py scripts/smoke_db_resolve_username.py
$env:PYTHONPATH = "."; py scripts/smoke_db_recent_trades_section.py
$env:PYTHONPATH = "."; py scripts/smoke_user_profile_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_trade_log_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_market_price_tool.py
$env:PYTHONPATH = "."; py scripts/smoke_chat_search_keyword_optional.py
$env:PYTHONPATH = "."; py scripts/smoke_profile_scope_narrowed.py
$env:PYTHONPATH = "."; py scripts/smoke_tools_wired.py
```

Expected: every script ends with `ALL ... PASS`.

- [ ] **Step 2: Run the pulse regression suite to confirm pulse-side wasn't disturbed**

```
$env:PYTHONPATH = "."; py tests/pulse_regression/run.py
```

Expected: `PASS synthetic-contrarian-buried`, `PASS synthetic-sibling-dup`.

- [ ] **Step 3: Final commit (if anything was fixed in Step 1)**

If all smokes passed without changes, skip this. If any required a fix, commit it:

```
git add -A
git commit -m "ask: final smoke fixes"
```

- [ ] **Step 4: Push the branch**

```
git push
```

- [ ] **Step 5: Manual QC after deploy**

After Railway redeploys, watch the next ~24h of /ask interactions in QC logs (push to `pulse-data` ask-logs/ branch via the existing publish job). Look for these question patterns and confirm the model uses the right tools:

| Question | Expected tool calls |
|---|---|
| "what's BK's open book" | `lookup_trade_log(caller="bankerkyle", kind="open")` |
| "did Abe close Tesla" | `lookup_trade_log(caller="abe", kind="recent")` |
| "how's Terlin's trading lately" | `lookup_trade_log(username=".terlin", kind="recent")` |
| "what's SV's win rate" | `lookup_trade_log(username="sv77788", kind="tally")` |
| "what has Kyle been crying about today" | `lookup_user_profile(username="bankerkyle", include_profile=True)` + `search_chat_messages(username="bankerkyle", days=1)` |
| "what's $TSLA at" | `lookup_market_price(symbols=["TSLA"])` |
| "how's BTC today" | `lookup_market_price(symbols=["BTC"])` |
| "who's the most annoying" | `lookup_user_profile(metric="racism")` |
| "what's BK's rank" | `lookup_user_profile(username="bankerkyle")` |

Also confirm:
- The `meta-narration` lint-hit log warnings should drop substantially (the slur-heavy auto-injection that triggered the prompt-block recovery from `1f78b75` is gone, so the rewrite-retry should rarely fire).
- The 22:01:45-style hard-filter blocks should no longer happen for benign questions (e.g., "top 3 laggard names from Trump folio" should now answer without going through the prompt-block recovery).
