# Narrow auto-load + on-demand context tools — design

**Date:** 2026-06-01
**Owner:** /ask subsystem
**Status:** Approved — ready for implementation plan

## Background

The /ask path currently auto-injects two heavy context blocks into every Gemini call:

1. **`profiles_block`** — WHO'S TALKING dossiers (Personality + Voice + Retarded Takes + Recent Personal Life + Recent Trades) for users active in the conversation.
2. **`analyst_block`** — Full trade log for every registered caller (today: Abe + BK), with three subsections each: open positions + recent trades + W/L tally.

Both blocks are heavy (combined ~8-15 KB per call) and only sometimes useful. Two problems have surfaced:

- **2026-06-01 22:01:45 UTC incident.** Sam asked the benign question *"what are the top 3 laggard names from Trump folio"*. The bot returned "Gemini bounced this one — its hard filter blocked the prompt." Root cause: Sam's question contained the literal string "Abe" ("I will have Abe unplug your shit boi"), which triggered the profile name-mention rule and pulled abe's WHO'S TALKING dossier into the prompt. That dossier includes verbatim slur quotes ("Monigga is probably the more appropriate nickname", etc.), which trip Gemini's unconfigurable hard filter (CSAM + severe policy — the categories not under `BLOCK_NONE`). A profiles-strip retry was shipped in `1f78b75` as a one-off recovery, but the underlying issue — auto-loading personality dossiers on fuzzy name matches — persists.

- **Always-injected analyst trade context.** Most /ask questions don't reference Abe or BK's trades. The ~8-12 KB of trade log per call is pure cost on those questions: more input tokens, more material for the model to wade through, more surface area for the hard filter to trip on slurs embedded in chat-context fragments.

## Goal

Move both heavy blocks behind tool calls. Auto-load only what is strictly committed (asker + Discord @-mention + reply/forward parent author). Expose a small set of robust tools that let the model fetch profiles, trade logs, recent chat by user, and live market prices on demand.

This narrows the always-injected prompt, removes the fuzzy-name-match attack surface that triggered the 22:01:45 block, and unlocks new question types (member trade lookups, live price quotes) that the current always-injected approach couldn't serve.

## Architecture

```
BEFORE                                    AFTER
─────────                                 ─────────
profiles_block  (asker + @-tag            profiles_block  (asker + @-tag
   + reply/forward + names found             + reply/forward author ONLY)
   in question/reply-text)
                                          [name-mention triggers REMOVED]
analyst_block   (Abe full + BK full
   always injected — open + recent +      [analyst_block REMOVED entirely]
   tally per caller)
                                          NEW TOOL: lookup_user_profile
                                              (replaces lookup_user_ranks)
                                          NEW TOOL: lookup_trade_log
                                              (callers AND members)
                                          NEW TOOL: lookup_market_price
                                          EXTENDED: search_chat_messages
                                              (keyword now optional)
```

## Components

### Component 1 — Entry-point trigger logic (strict scope)

**Location:** `discord_bot/bot.py` — both /ask entry paths (slash command at ~line 3504, @mention handler at ~line 3627).

**Change:** drop these triggers from the `profile_ids` build:
- `find_users_mentioned_in_text(question)` — the literal-string name match in the question itself
- `find_users_mentioned_in_text(ref_content)` — the literal-string name match in the reply-parent text (lines ~3698-3713)

**Keep these triggers:**
- `[user_id]` — the asker, always
- `mentioned_ids` from Discord's first-class `message.mentions` — real @-mentions only, not text matches
- `ref_uid` — the reply-parent or forward-snapshot author

**`mentioned_ids` for subject-verbatim block stays unchanged.** The literal-name-match still informs the `_format_subject_verbatim_block` injection (cheap, useful for accurate quoting). Only its use for profile auto-load is removed.

**Resulting `profile_ids` set:**
```python
profile_ids = list(set(
    [user_id]
    + [m.id for m in message.mentions if not m.bot]
    + ([ref_uid] if ref_uid and ref_uid != user_id else [])
))
```

### Component 2 — Drop `analyst_block` from prompt assembly

**Location:** `discord_bot/bot.py` — `_answer_with_gemini`, lines ~2110-2138.

**Change:** delete the `analyst_block` assembly block and the corresponding `sections.append(analyst_block)` line. Caller trade data now comes via `lookup_trade_log` tool call when the model asks for it.

### Component 3 — `lookup_user_profile` tool (replaces `lookup_user_ranks`)

**Location:** `discord_bot/bot.py` — new tool def + executor; removes existing `_build_user_ranks_tool` / `_execute_user_ranks`.

**Signature:**

```python
lookup_user_profile(
    # Anchor — exactly one of these must be provided
    username: str | None = None,        # specific user
    metric: str | None = None,          # "trader" | "racism"
    rank_position: int | None = None,   # used with metric for "user at rank N"

    # Output control
    include_profile: bool = False,      # also return full dossier (Personality + Voice
                                        # + Retarded Takes + Recent Personal Life)
)
```

**Mode resolution:**

| Args | Returns |
|---|---|
| `username="bankerkyle"` | `{rank: "1/53 trader, 1/3 racism", trader_rationale: "...", racism_rationale: "..."}` |
| `username="bankerkyle", include_profile=True` | Above + full WHO'S TALKING dossier |
| `metric="trader", rank_position=3` | The user at trader-rank #3 + their rationales |
| `metric="trader", rank_position=3, include_profile=True` | Above + that user's full dossier |
| `metric="racism"` (no rank_position) | Top 5 leaderboard, each with rationales — no profiles (5 dossiers too big) |
| `metric="racism", include_profile=True` (no rank_position) | **Error**: `"include_profile not supported for leaderboard mode — ask for a specific user instead"` |
| No anchor (no username, no metric) | **Error**: `"must provide username, or metric, or metric+rank_position"` |
| `username` + `metric` together | **Error**: `"provide exactly one anchor"` |

**Executor:** `_execute_user_profile(args)` resolves the anchor via existing `db.get_global_trader_ranks()` / `db.get_global_racism_ranks()` (same source as `lookup_user_ranks` today), and when `include_profile=True` also calls `db.format_user_profiles_for_context([resolved_user_id])` for the dossier text.

**Disclosure rules** (carried forward from `lookup_user_ranks`): name + ordinal rank + rationale are shareable; raw 0-100 scores are not. Profile body uses the same "don't surface that there's a profile somewhere" rule that already applies to the auto-injected `WHO'S TALKING` block.

### Component 4 — `lookup_trade_log` tool

**Location:** `discord_bot/bot.py` — new tool def + executor.

**Signature:**

```python
lookup_trade_log(
    # Anchor — exactly one of these must be provided
    caller: str | None = None,       # registered caller name: "abe" | "bankerkyle" | ...
    username: str | None = None,     # any other user's Discord username

    # Slicing
    kind: str = "all",               # "open" | "recent" | "tally" | "all"
    days: int | None = None,         # default 7 for recent, 30 for tally; ignored for open
)
```

**Mode resolution — caller anchor:**

`caller="abe"` queries `analyst_trades` filtered to that caller (existing `format_analyst_trades_for_context(caller=..., tracking_mode="caller")` path). Returns ONLY the log data. Response includes `data_quality: "caller"` — caller-mode rows have W/L stitching done daily by cron.

**Mode resolution — username anchor:**

`username="terlin"` resolves username → `author_id` (new `db.resolve_username_to_user_id` helper, walks `user_profiles.username` first then `chat_messages.author_username`). Then:

1. Queries `analyst_trades WHERE author_id = ?` with NO `tracking_mode` filter — returns both `caller` mode and `member` mode rows.
2. Also calls `db.get_user_profile_recent_trades_section(user_id)` to extract the "Recent trades" markdown section from `user_profiles.profile_text`.
3. Returns BOTH sources combined.

**`data_quality` for username anchor:** `"caller"` if any rows returned have `tracking_mode = 'caller'` (rare — only happens if a registered caller is queried by their Discord username instead of by `caller=`). Otherwise `"member"`. This signals the model whether the W/L numbers reflect cron-stitched open/close pairs (caller) or raw OCR snapshots without pair-stitching (member).

**Why both for members:** caller logs (Abe, BK) have clean per-trade OCR + daily W/L stitching, so the log is authoritative — no need for the profile snippet. Members (Terlin, SV, etc.) often have spotty or zero `analyst_trades` rows because they don't post structured screenshots in eager-OCR channels; the "Recent trades" section of their profile is the only summary that exists. Surface both, let the model reconcile.

**Response shape (caller anchor):**

```json
{
  "anchor": {"type": "caller", "name": "abe"},
  "kind": "open",
  "data_quality": "caller",
  "trades_text": "ABE'S CURRENTLY OPEN POSITIONS (sorted by closest expiry first):\n- META 640C 06-05 @6.40\n- ..."
}
```

**Response shape (username anchor):**

```json
{
  "anchor": {"type": "username", "name": "terlin", "user_id": 123456789},
  "kind": "recent",
  "data_quality": "member",
  "analyst_trades_text": "(structured rows from analyst_trades, if any)",
  "profile_recent_trades": "(text snippet from user_profiles.profile_text Recent trades section, if any)"
}
```

**Empty-result handling:**

| Scenario | Returns |
|---|---|
| `caller` anchor, no rows | `{"anchor": ..., "kind": ..., "trades_text": "", "data_quality": "caller", "status": "no_logged_trades"}` |
| `username` anchor, no analyst rows AND no profile section | `{"anchor": ..., "status": "no_logged_trades", "username": "..."}` |
| `username` not resolvable | `{"error": "username '...' not found"}` |
| Both `caller` and `username` set | `{"error": "provide exactly one of caller or username"}` |
| Neither set | `{"error": "provide exactly one of caller or username"}` |
| `kind` not one of valid values | `{"error": "kind must be one of: open, recent, tally, all"}` |

**Member-mode W/L caveat:** today's daily-cron W/L stitching runs only for caller-mode rows. A `username` anchor with `kind="tally"` returns whatever's stitchable from member rows (approximate at best) plus the profile snippet. The `data_quality: "member"` flag signals the model to hedge in its answer ("small sample, mostly self-reported"). Adding member-mode stitching to the cron is out of scope for this design — track separately if usage warrants.

**Executor:** `_execute_trade_log(args)` validates anchor exclusivity, dispatches to the caller path (existing `format_analyst_trades_for_context`) or the username path (new combined query), and returns the JSON shape above.

### Component 5 — `lookup_market_price` tool

**Location:** `discord_bot/bot.py` — new tool def + executor. Reuses `report/market_data.py`'s existing private fetchers.

**Signature:**

```python
lookup_market_price(
    symbols: list[str],     # ["TSLA"], ["SPY","QQQ"], ["BTC","ETH","SOL"], etc.
)
```

**Routing logic:** hardcoded crypto allowlist (BTC, ETH, SOL, DOGE, ADA, AVAX, MATIC, XRP, BNB, LINK — easy to extend) routes to `report.market_data._fetch_binance_24h(<SYM>USDT)`. Everything else routes to `report.market_data._fetch_finnhub_quote(<SYM>)`. Symbol not found or API failure → per-symbol `error` field.

**Response shape:**

```json
{
  "session": "OPEN" | "PRE-MARKET" | "AFTER-HOURS" | "WEEKEND-CLOSED",
  "timestamp": "2026-06-01 16:30 ET",
  "quotes": [
    {"symbol": "TSLA", "price": 433.18, "change_pct": -1.8, "prev_close": 441.10, "source": "finnhub"},
    {"symbol": "BTC",  "price": 109423.10, "change_pct": -1.2, "prev_close": null, "source": "binance"},
    {"symbol": "ASDF", "error": "symbol not found"}
  ]
}
```

The `session` field tells the model how to phrase ("session-to-date" mid-day, "yesterday's close" pre-market, "live 24/7" for crypto) — same logic the daily pulse RECAP uses.

**Rate limits:** Finnhub free tier = 60 calls/min. With /ask capped at 20/user/day and each call making at most a handful of price lookups, the global rate is comfortably under. If we ever hit 429 from Finnhub, the per-symbol response is `{"error": "rate-limited, retry in a few seconds"}` and the model handles.

**Caveats noted in the tool description (so the model surfaces them honestly):**
- Finnhub free tier has ~15-min delay on some asset classes — tool isn't tick-level
- Binance.US may differ from Coinbase/global crypto venues by a few bps

**Executor:** `_execute_market_price(args)` walks the symbol list, routes each, collects results, prepends `session` and `timestamp` from `report.market_data._session_label`.

**Validation:**

| Failure | Returns |
|---|---|
| `symbols` empty or missing | `{"error": "symbols list cannot be empty"}` |
| `symbols` over 10 items | Truncate to first 10; include warning in response (`"truncated_to": 10`). Prevents accidental fan-out. |
| `symbols` contains non-string items | Per-symbol error; other valid symbols still resolve. |

**Cost:** $0. No new API keys; both `finnhub_api_key` and Binance.US (no key needed) are already in use.

### Component 6 — `search_chat_messages` extension (keyword optional)

**Location:** `discord_bot/bot.py` — existing `_build_chat_search_tool` + `_execute_chat_search` (find by name).

**Change:** make the `keyword` parameter optional. When omitted, the query returns recent messages matching the other filters (`username` + `days` + `channel_name`), ordered by recency, capped at the same result limit as today.

This closes the gap for the *"what has Kyle been crying about today?"* pattern — the model wants Kyle's recent messages but has no specific keyword.

**Validation:** if `keyword` is omitted AND `username` is also omitted AND `channel_name` is also omitted, return `{"error": "must provide at least one of keyword, username, or channel_name"}` — prevents a "fetch every message" full-table scan.

### Component 7 — DB helpers

**Location:** `db.py` — three small additions.

1. **`format_analyst_trades_for_context` gains `kind` parameter.**

   ```python
   def format_analyst_trades_for_context(
       hours: int = 168,
       limit: int = 30,
       caller: str | None = None,
       display: str | None = None,
       tracking_mode: str | None = "caller",
       kind: str = "all",   # NEW: "open" | "recent" | "tally" | "all"
   ) -> str:
   ```

   When `kind="all"` it returns today's three-chunk output (regression-safe). When `kind="open"` it returns only the current-open-positions chunk, etc.

2. **`get_user_profile_recent_trades_section(user_id) -> str`.**

   New helper. Reads `user_profiles.profile_text` for `user_id`, regex-extracts the section starting at `**Recent trades.**` (or `**Recent Trades.**` — case-insensitive) and ending at either (a) the next bold-header pattern `**[A-Z][^*]+\.\*\*` or (b) end-of-string. Returns "" if profile absent or section missing. The bot's profile format uses bold section headers (`**Personality and style.**`, `**Voice.**`, `**Recent trades.**`, `**Recent personal life.**`); this regex is tied to that format and will need updating if the profile prompt template changes.

3. **`resolve_username_to_user_id(username) -> int | None`.**

   New helper. Tries `user_profiles.username` first (LLM-canonical, lowercase), falls back to `chat_messages.author_username` (case-insensitive). Returns the first exact match or `None`. Already partially exists in scattered places — consolidate to one helper.

### Component 8 — System instruction updates

**Location:** `discord_bot/bot.py` — `_build_runtime_system_instruction` (and the long prompt-text file it composes from).

**Changes:**

- Replace the `lookup_user_ranks` section with a `lookup_user_profile` section. Carry forward all existing disclosure rules verbatim. Add new examples for `include_profile=True`:
  - *"What's BK's rank?"* → `lookup_user_profile(username="bankerkyle")`
  - *"What does BK think of TSLA?"* → `lookup_user_profile(username="bankerkyle", include_profile=True)` then optionally `search_chat_messages(username="bankerkyle", keyword="TSLA", days=30)`
  - *"Why is BK so loud today?"* → `lookup_user_profile(username="bankerkyle", include_profile=True)`
  - *"Who's the most annoying?"* → `lookup_user_profile(metric="racism")`
  - *"Top 5 traders?"* → `lookup_user_profile(metric="trader")`
  - *"Who's at trader rank #3?"* → `lookup_user_profile(metric="trader", rank_position=3)`

- Remove any references to "YOU ALREADY HAVE ABE'S RECENT TRADES IN YOUR CONTEXT" or equivalent. Replace with `lookup_trade_log` examples:
  - *"What's BK's open book?"* → `lookup_trade_log(caller="bankerkyle", kind="open")`
  - *"Did Abe close NVDA today?"* → `lookup_trade_log(caller="abe", kind="recent", days=1)`
  - *"Did Abe close Tesla?"* → `lookup_trade_log(caller="abe", kind="recent", days=7)` and scan for TSLA close events
  - *"How's Abe's win rate?"* → `lookup_trade_log(caller="abe", kind="tally")`
  - *"How's Sam's win rate?"* → `lookup_trade_log(username="theorb_18574", kind="tally")` (member-mode, hedge on data_quality)
  - *"How's Terlin's trading going lately?"* → `lookup_trade_log(username=".terlin", kind="recent", days=14)`

- Add a `lookup_market_price` section with examples:
  - *"What's $TSLA at?"* → `lookup_market_price(symbols=["TSLA"])`
  - *"How's BTC and ETH today?"* → `lookup_market_price(symbols=["BTC","ETH"])`
  - *"Is $SPY green today?"* → `lookup_market_price(symbols=["SPY"])`

- Extend the `search_chat_messages` section:
  - *"What has Kyle been crying about today?"* → `lookup_user_profile(username="bankerkyle", include_profile=True)` for personality context, then `search_chat_messages(username="bankerkyle", days=1)` (no keyword) for actual recent quotes

- Hard rule already in prompt: don't cite the tool ("per `lookup_trade_log`", "[search_chat_messages]"). Stays unchanged.

## Data flow (worked example)

Sam asks /ask `"did Abe close Tesla?"`:

1. **Entry point** builds `profile_user_ids = [Sam's user_id]` only. "Abe" is named in the text but no longer triggers profile auto-load.
2. **`_answer_with_gemini`** assembles `user_content`:
   - WHO'S TALKING (Sam's profile only)
   - Recent channel chat (auto-injected, last 50 msgs)
   - `--- Sam is asking: ---`
   - `did Abe close Tesla?`
   No `analyst_block`.
3. **Gemini call #1** — model sees "Abe close Tesla" → emits `lookup_trade_log(caller="abe", kind="recent", days=7)`.
4. **Executor** returns Abe's last ~30 trade events.
5. **Gemini call #2** — model scans for TSLA close events, writes the answer.
6. **Answer ships** through existing voice-cleanup → arch-leak retry → repetition-glitch retry → Discord.

**Average savings:** ~10 KB per call (analyst_block + sometimes-extra profiles) on most questions. **Latency cost:** +0.5-1s per tool-call round-trip for questions that need profile / trade / price data.

## Error handling

| Failure | Handling |
|---|---|
| Model fails to call a tool when it should | Model-quality issue. Mitigation: explicit system-instruction examples mapping question patterns to tools. Acceptable failure mode — ship and tune from QC logs. |
| `lookup_user_profile(username="nobody")` | Executor returns `{"error": "user 'nobody' not found"}`. Model handles. |
| `lookup_trade_log(username="...")` with no rows and no profile section | Returns `{"status": "no_logged_trades", "username": "..."}`. Model tells user. |
| Tool-call loop runs away | Existing bound: `_CHAT_SEARCH_MAX_ROUNDS = 3` covers all new tools too (it's a loop-round counter, not per-tool). |
| Username resolution ambiguous | `resolve_username_to_user_id` returns first exact-match; on no match → `None` → executor returns error. |
| `lookup_market_price` Finnhub returns 429 | Per-symbol `{"error": "rate-limited, retry in a few seconds"}`. Other symbols in the same batch still succeed. |
| Both anchors set on `lookup_trade_log` or `lookup_user_profile` | Executor returns `{"error": "provide exactly one anchor"}`. |
| Prompt-block recovery retry (the 1f78b75 fix) | Stays as today — tools are part of `config` so they ride along to the recovery call too. No code change here. |

## Testing

Four new smoke-test files alongside the existing /ask smokes:

### `scripts/smoke_profile_scope.py`

- Static check that `_answer_with_gemini` source no longer assembles `analyst_block` (no `format_analyst_trades_for_context` direct call in prompt assembly).
- Static check that both /ask entry paths (slash command + @mention) no longer pass name-mention-derived ids into `profile_ids`. Anchor by the existing `profile_ids = list(set(` lines; assert the assembly contains only `[user_id]`, `message.mentions`, `[ref_uid] if ref_uid`.

### `scripts/smoke_user_profile_tool.py`

- All three existing `lookup_user_ranks` modes work under the new `lookup_user_profile` tool (regression baseline — same returned structure for the rank-only paths).
- `username="bankerkyle", include_profile=True` returns a payload containing the full dossier text (Personality + Voice sections present).
- Leaderboard mode + `include_profile=True` returns `{"error": ...}` (rejected).
- No-anchor call returns `{"error": ...}`.
- Both `username` and `metric` together returns `{"error": "provide exactly one anchor"}`.

### `scripts/smoke_trade_log_tool.py`

- `caller="abe", kind="open"` returns a payload with `data_quality: "caller"` and `trades_text` containing only the open-positions chunk.
- `caller="abe", kind="all"` returns the same combined output as today's `format_analyst_trades_for_context(caller="abe")` (byte-for-byte regression baseline).
- `username="<known-member>", kind="recent"` returns a payload with `data_quality: "member"`, both `analyst_trades_text` and `profile_recent_trades` keys present (one may be empty).
- `username="<unknown>"` returns `{"error": "username '...' not found"}`.
- Both anchors set → `{"error": ...}`. Neither set → `{"error": ...}`. Invalid `kind` → `{"error": ...}`.

### `scripts/smoke_market_price_tool.py`

- `symbols=["TSLA"]` returns a payload with `session` populated and `quotes[0]` containing `symbol`, `price`, `change_pct`, `source: "finnhub"`. (Live call; skip cleanly when `FINNHUB_API_KEY` is unset in test env.)
- `symbols=["BTC"]` returns `source: "binance"` and a numeric `price`.
- `symbols=["ASDF"]` returns `quotes[0]` with `error` field (unknown symbol).
- `session` is one of the four labels and matches the current ET time.

### End-to-end manual verification

After deploy, watch a representative spread of /ask QC log entries for these patterns:

- *"what has Kyle been crying about today?"* — confirm model calls `lookup_user_profile(... include_profile=True)` + `search_chat_messages(username=..., days=1)` (no keyword).
- *"how's Terlin's trading going lately?"* — confirm `lookup_trade_log(username="...", kind="recent")` fires; check that response handles both empty-analyst-rows and present-profile-snippet cases.
- *"did Abe close Tesla?"* — confirm `lookup_trade_log(caller="abe", kind="recent")` fires; model picks TSLA close event from the list.
- *"what's SV's win rate?"* — confirm `lookup_trade_log(username="sv77788", kind="tally")` fires; answer hedges via `data_quality: "member"`.
- *"what's $TSLA at?"* — confirm `lookup_market_price(symbols=["TSLA"])` fires; model phrases session-aware ("session-to-date" mid-day vs "yesterday's close" pre-market).
- *Architecture-leak monitoring* — confirm `meta-narration` lint-hit rate doesn't regress (the prompt-block recovery from 1f78b75 should now rarely fire because the slur-heavy auto-injection is gone).

## Shipping

Single-PR rollout (Approach A from brainstorming):
- All components in one commit.
- Rollback = `git revert`.
- No feature flag, no two-step toggle. Project size doesn't warrant the dead-code-toggle complexity.

## Out of scope (track separately if usage warrants)

- Member-mode trade stitching in the daily cron (would lift `data_quality: "member"` toward "caller" fidelity).
- CoinGecko fallback for crypto when Binance.US is unreachable.
- Ticker filter on `lookup_trade_log` (e.g. `ticker="TSLA"`) — defer until QC shows the model missing on ticker-specific questions because the `days` window was too narrow.
- Live futures / FX quotes (E-mini S&P, EURUSD, DXY) — Finnhub free tier doesn't cover these reliably.
- Auto-detect "obviously crypto" symbols beyond the hardcoded allowlist — defer until QC shows real misses.
