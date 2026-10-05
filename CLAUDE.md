# CLAUDE.md

## Writing style (binding — applies to everything Claude writes here)

Write in direct, technically accurate, plain English. Avoid melodramatic, flowery, or decorative language. Do not state the obvious or repeat points, but do not skip logical steps — explanations must be thorough on the first pass. Avoid unnecessary buzzwords; use precise domain terminology where accuracy requires it. Eliminate common AI mannerisms: do not overuse em-dashes and semicolons, and avoid generic transition words (delve, tapestry, crucial, pivotal, furthermore, in conclusion).

Scope: responses to the user, commit messages, code comments, docs, and reports. This is separate from the bot's output voice — the pulse and /ask have their own voice contracts (`discord_bot/ask_prompt.py`, DRAFT_USER, `ai_analysis/voice_rules.py`) and those rules win for bot-facing text.

## Owner decisions and working agreements (binding on all sessions)

Moved here from local session memory on 2026-10-01 so a cloud session has them. Product and process only; notes about specific members stay out of this public file.

- **How to work:** plain-English progress updates while building; run the code-review skill on every change before pushing and at milestones; bring risks and product decisions to the owner rather than deciding them.
- **Evidence before claims:** verify a claimed error (the web for facts, the full source sentence for a statistic) before calling it wrong; search the transcript, heredocs included, before saying an action was not this session's; three or more findings in one subsystem mean a design flaw, so propose the structural fix instead of a fourth patch.
- **/ask model stays `gemini-3.5-flash-lite`** (owner declined 3.8 Flash on 2026-09-30). Close quality gaps in code and data. Do not re-propose without new evidence.
- **/ask stock answers:** narrate data, never grade it (no thresholds: "low float", "liquid", "risky"); growth and beat/miss first with the dollar figure beside it, computed in code; name the part of the business that drives the figure as a reason; a desk is credited only with what its own note says; a print that already happened leads. Fix quality in `discord_bot/stock_outline.py` or the data, not with another rewrite pass. Show every sample through the live harness (`.claude/skills/ask-harness`).
- **Pulse voice:** no news-source attribution ("per Reuters"); no corpus meta-narration ("8 notes flag", "consensus is firming"); no single-name shorts built from intraday dispersion; no house trade calls (see Writing voice).
- **Bot behavior:** a reply to the bot always gets an answer, even when it tags someone else; member identity is `author_id`, never a display name.
- **Declined, do not re-raise:** rotating the GitHub PAT; the 2026-09-01 review's P0 about the public repository.

## Working from a cloud session

Everything the code needs is in this repository. Three things were local to the owner's machine until 2026-10-01. `scripts/cloud_setup.sh` handles two of them: it builds `.venv` on Python 3.12 (the push gate refuses any other version), installs the pinned requirements plus `requirements-dev.txt` (test-only packages such as PyYAML, which go there and never into `requirements.txt`, the file the worker installs), and points git at the versioned push gate in `.githooks/pre-push` (`git config core.hooksPath .githooks`, the same hook the local machine uses). Run it at session start or set it as the cloud environment's setup script. The third is production access: `railway logs`, `railway ssh`, the read-only DB probes and the /ask live harness need a Railway token in the session environment (`RAILWAY_API_TOKEN`, an account token, which the script links to `marvelous-dream`/`production`/`worker`, or `RAILWAY_TOKEN`, a project token). Without one, production checks are unavailable and the session should say so rather than guess. `railway ssh` also needs an SSH key registered on the Railway account, and a cloud container starts with none: the environment carries a dedicated cloud key as `RAILWAY_SSH_KEY_B64` (the private key file, base64 on one line), which the setup script writes to `~/.ssh/id_ed25519` (installing `openssh-client` first, since the cloud image has no ssh), then checks that its fingerprint is registered on the Railway account (`railway ssh keys list`). Revoke it with `railway ssh keys remove`. **In Anthropic-hosted cloud sessions `railway ssh` cannot work at all** (confirmed 2026-10-02): every outbound connection goes through an HTTP/HTTPS proxy at every network level, Full included, so SSH on port 22 never connects and the CLI hangs. From the cloud, `railway logs`, `status`, `variables` and pushes that deploy all work. Worker DB probes and the /ask live harness need `railway ssh`, so they run on the owner's machine. A cloud session that needs one says so and asks. Live API keys (Gemini, Finnhub, Dropbox) are not put in the cloud: live work runs on the worker, which has them. Set only `RAILWAY_API_TOKEN`. A `RAILWAY_TOKEN` alongside it takes precedence in the CLI and breaks auth. No key of any kind goes into this public repository.

**Session memory is in a private repository**, `gabjew90/institutional-report-bot-memory` (since 2026-10-01): the auto-memory directory on the owner's machine is a checkout of it, and `scripts/cloud_setup.sh` puts a checkout at the cloud session's memory path, on `main` (a repo attached to a cloud session arrives on a `claude/...` branch, and a memory committed there never reaches the owner's machine). **Local and cloud stay in sync through two hooks** in `.claude/settings.json`, both running `scripts/session_sync.py`: at session start it pulls the code and memory repos (moving a throwaway `claude/...` checkout onto the deploy branch or memory `main` only when that loses nothing, and reporting unpushed or stranded work), and after every reply it commits and pushes changed memory files, rebasing once if the other side pushed first. The hooks never commit or push code. Code goes out through the push gate. A cloud session can start one folder up (`/home/user`), where the repo's `.claude/settings.json` does not load: the setup script then writes the same hooks (absolute paths) to `/home/user/.claude/settings.json` and links that folder's memory path to the same checkout, and the start hook tells the session to work in the repo and read this file first. The cloud loads settings a few seconds before setup writes that file, so the hooks miss the session they were written for: setup therefore also runs the start sync itself, and in a cloud session where no "session sync" note appeared, push memory by hand after writing it. In the cloud the code checkout ends up on the deploy branch, so a push there deploys to production, the same as from the local machine. The one manual rule: do not work on code in a local and a cloud session at the same time.

## /ask prompt: enforcement policy (binding on all sessions)

The system prompt is a scarce shared resource, currently ~64,000 chars.
The budget below is provisional and set by the owner; it is not a
measured optimum. Treat it as a spending limit, not a target.

1. Deterministic first. If a rule can be checked by regex, a tool-call-log
   assertion, or a schema constraint, implement it as CODE and DELETE the
   corresponding prompt text in the same commit. Never both.
2. Tool mechanics belong in tool declarations, not the system prompt.
   Parameter semantics, status handling, and per-tool usage shapes go in
   the schema. Only cross-tool routing priority stays in the prompt.
3. Every prompt addition names its paying deletion in the commit message:
   "adds N chars, removes M chars, net -X".
4. Incident narratives live in the ask_prompt.py module docstring ledger.
   The prompt body carries the RULE only — no dates, no story, no "observed:".
5. Before adding a rule, grep for the behavior it governs. If a rule already
   covers it, REWRITE that rule. Never add a second one alongside.
6. _SIZE_CEILING only decreases. Raising it is owner-only, requires evidence
   from scripts/ask_fixture_run.py, and is not an agent-available action.

## Project Overview

Institutional Research PDF Analyzer + Discord Market Pulse Bot. Processes 100-200 institutional financial research PDFs daily from Dropbox and delivers synthesized trading intelligence to Discord channels.

**Target audience:** self-directed options and crypto traders. Smart but not finance professionals — they don't know terms like "convexity," "term structure," or "NII." Every technical term must be translated.

**Live deployment:** Railway, project `marvelous-dream`, service `worker`. Always-on. Connected to Dropbox + Discord + Gemini + Finnhub.

## Architecture

```
Dropbox (/Current)
  ↓ every 15 min (cursor-based delta polling)
  ↓ dropbox_client/watcher.py — poll_and_download
  ↓ (file lands on /data volume as PDF)
pdf_files row created with status=DOWNLOADED
  ↓ every 5 min (asyncio processing)
  ↓ pdf_processing/extractor.py — PyMuPDF text extraction
  ↓ ai_analysis/analyzer.py — triage (Gemini text-only, ~2K tokens)
  ↓                          — deep analysis (Gemini text-only, full document, ~15K tokens)
  ↓ pdf_analyses row (append; old analyses preserved as history)
  ↓
Scheduled 10:00 AM ET, market-open days only (Claude.ai routine cron 0 14 * * 1-5 UTC; NYSE holidays skip via world_context.US_MARKET_HOLIDAYS + routine STEP 2.1 — update the calendar annually)
  OR user runs /pulse, /pulse hours:N
  ↓
report/synthesizer.py — builds context (live market data + news + calendar + prev pulse if scheduled)
  ↓ calls Gemini with aggregated per-PDF JSON
  ↓ daily_reports row
  ↓
report/formatter.py — Discord embeds (RECAP / MAIN EVENT / BRIEFS / WHAT TO WATCH + TRADE BOARD)
discord_bot/sender.py — posts to every channel in DISCORD_CHANNEL_ID
```

## AI Model

**Google Gemini 3.1 Flash Lite** (`google-genai` SDK). Env var `GEMINI_MODEL=gemini-3.1-flash-lite`. Same model for triage, deep analysis, synthesis. NOT Anthropic/Claude.

## Key Design Decisions

### Text-first ingestion with a narrow multimodal carve-out
Deep analysis sends the full document as text to Gemini — no truncation. Multimodal was dropped entirely on 2026-04-13, then **selectively re-enabled on 2026-05-07** (commit `93863c7f`) for a narrow trigger: priority HIGH + source in {GS, MS, JPM, Citi, DB, BofA} + (report_type in {equity_research, derivatives, vol_commentary} or exhibit-heavy filename) + ≥5 pages. Those reports get up to 30 rendered page images attached (via `page_selector.py`); everything else stays text-only. Failures fall back to text-only. Don't broaden the trigger without explicit user sign-off.

### Gemini-only priority (no source/topic overrides)
`_apply_priority_rules` returns Gemini's call verbatim. Tier-1 floor (GS/JPM/BofA/MS = min MEDIUM) and HIGH topic boost (macro/crypto/vol_commentary/morning_briefing/sales_trading/strategy/derivatives = force HIGH) were removed. Triage prompt has expanded LOW criteria to filter out peripheral EM macro, minor FX pairs, niche commodities, single-stock regional research, credit without spread calls, technical-only analysis, historical wrap-ups.

### Scheduled vs manual pulse behavior
| | Window | Prev-pulse context |
|---|---|---|
| Scheduled 10 AM ET (Claude.ai routine) | Since last scheduled pulse | ✅ Diff-framing — skip themes unchanged from yesterday |
| `/pulse` (no args) | **Last 24h always** | ❌ None — fully standalone |
| `/pulse hours:N` | Last N hours (max 168) | ❌ None |

Scheduled pulse updates `daily_reports` with `report_type='daily'` (this is the cutoff anchor for next day's window). Manual /pulse writes `report_type='manual'` so it does not affect the scheduled cadence.

### Timestamp format normalization
SQLite's `datetime('now')` uses space separator (`"2026-04-14 13:00:00"`); Python's `isoformat()` uses T (`"2026-04-14T13:00:00"`). Lexical TEXT comparison treats T > space, so mixed-format comparisons are broken. `db._normalize_ts()` is applied at every cutoff comparison site. `insert_daily_report` explicitly writes T-format going forward.

### Pulse output structure (4 sections + TRADE BOARD)

**This section described 3 sections with an `INSIGHTS & ALPHA` block until 2026-08-12. That has been wrong since the phase-3 overhaul on 2026-06-18** (`docs/superpowers/specs/2026-06-18-format-overhaul-phase3-main-event-briefs.md`).

1. **RECAP** (gold embed) — live market prices + news + this morning's data releases. Only section where live prices/news are used. A Tier-1 event that has already printed must carry its ACTUAL value: `pulse_draft_validate` hard-fails `released-actual-missing` otherwise (2026-08-12, a CPI recap shipped three banks' forecasts and never the print). Earnings actuals get the same duty via the CONSENSUS LEDGER (`{prev_consensus_block}` in DRAFT_USER/AUDIT_USER, rendered from the previous pulse's own consensus lines): report a ledger name's print as a beat or a miss, never claim "no consensus" for it — `consensus-amnesia` hard-fails both (2026-08-19, the pulse denied a Target consensus its own prior edition had published).
2. **THE MAIN EVENT** — the single lead theme, ~300-450 words (reconciled 2026-08-20 to the observed good range; pulse_lint's soft `section-length` check flags drift outside 250-500), full five-movement arc with a named bank-vs-bank debate and an explicit invalidation. Must be what the tape is actually doing today (a fresh break/unwind/reversal), never an evergreen "the trend is intact" reassurance.
3. **BRIEFS** — every remaining theme, ~110-180 words each, 4-6 sentences (reconciled 2026-08-20; lint flags outside 70-220). No five-movement arc, no bullet stacks, form varied so they don't read as clones.
4. **WHAT TO WATCH** (orange embed) — ### Today + ### This Week subsections. **Calendar is filtered hard at the data layer** — only FOMC/Powell/CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI + MAG7/big-bank earnings reach synthesis. Fed governor speeches (non-Powell), regional Fed surveys, minor data, foreign macro are dropped.

DRAFT writes 2 and 3 as ONE ordered list under a single `## 2. INSIGHTS & ALPHA` header; downstream tooling splits it, so **the order DRAFT chooses is the depth assignment** — first theme becomes THE MAIN EVENT, the rest become BRIEFS. That header is still the seam every upstream fixture uses.

**TRADE BOARD** renders between BRIEFS and WHAT TO WATCH and carries only trades a desk explicitly called (see the Writing voice section). `## _LEANS` is a separate internal block DRAFT writes and the routine strips before publish; it records each theme's directional read for `pulse_leans` tracking and is not a published recommendation.

Footer: dynamic stats (top sources, priority mix always shows high/medium/low, research date range, next pulse time).

### Cashtag formatting + structured entity extraction
Each deep analysis extracts `entities_mentioned: list[{name, ticker, asset_class}]`. Synthesizer aggregates across all PDFs into a dedup ticker lookup and injects into the synthesis prompt. Cashtag rule: `$AAPL`, `$NVDA`, `$BTC`, `$ETH`, `$SPX` etc. for stock/etf/crypto/index. Skip `$` for FX (DXY, EURUSD), commodity spot (Brent, Gold), currencies in prose.

### Writing voice — non-AI prose
The live voice contract lives in DRAFT_SYSTEM/DRAFT_USER + `ai_analysis/voice_rules.py`. `compose_audit_voice_block()` is interpolated into **both DRAFT_SYSTEM and AUDIT_SYSTEM** via the `<<VOICE_RULES_BLOCK>>` placeholder, and `compose_lint_patterns()` feeds pulse_lint, so one edit propagates to the writer, the editor and the linter. Module load raises if either placeholder is missing. Do NOT edit from memory of the old "Circle/USDC few-shot, memorable phrasing" description; those blocks (`DAILY_SYNTHESIS_*`) were dead code and were deleted 2026-08-07.

**This paragraph used to claim voice_rules was "composed into the AUDIT prompt". It was not, from whenever the block was written until 2026-08-11.** `compose_audit_voice_block()` had zero callers and had never reached a model, so the rewrite-over-gloss rule existed only as an intention while the live prompts said the opposite. Inline glosses ran 5.6x higher per 100 words as a result. When editing this section, verify the wiring with `grep -rn compose_audit_voice_block` rather than trusting the description here — a doc that describes intent instead of behavior is what let it hide.

Live contract, per the 2026-08-07 style directive: direct, technically accurate, plain English. Opinionated and specific (a call, a stance, an invalidation) but not decorative — melodrama family (bloodbath/carnage/eye-popping class) is lint-banned; conviction is directness, not theatrics.

**The pulse does NOT issue trade calls of its own** (owner decision, 2026-08-12: "let's not make up longs, let's only do it when explicitly called"). Every theme still closes with something to act on, but it must be one of: a trade a NAMED DESK explicitly called, attributed; the falsifiable condition that decides the theme (a level, a print, an event); or the specific catalyst that resolves it. An invented house position ("Long $NVDA while the yield relief holds") is a defect. Same rule governs the TRADE BOARD, which renders only desk calls — see `report/pulse_sections.py :: render_trade_board`. Board text is rendered by the bridge at post time, AFTER lint/SCRUB ever run, so the renderer is the only voice-contract enforcement point for it: `_clean_inline` rewrites em-dashes/semicolons and `_hc_call_lines` drops calls sourced to `BANNED_PUBLICATION_NAMES` (2026-08-20 — published boards had shipped semicolons and a "The Market Ear" source line). The `## _LEANS` block DRAFT writes is unaffected: it still feeds `pulse_leans` tracking and the hard `leans-block-missing` validator, it just no longer reaches the reader. Hard bans: em-dashes and semicolons (total, lint-enforced; the TRADE BOARD uses `·` separators), "it's worth noting", "notably", "Meanwhile,", "Furthermore,", "Additionally", "In conclusion", "Taken together", crucial/pivotal/tapestry/delve/robust, hedging, wrap-up sentences. One sanctioned exception: the H1 headline stays punchy/declarative (3-5 words, tabloid register) — it is the only place decoration is allowed. Precise load-bearing terminology (basis points, core PCE, EBITDA) is kept and glossed on first use, never paraphrased away.

Translation rules — **rewrite the sentence so the term is not needed; do not keep the term and bolt its definition on beside it.** Order of preference: rewrite, replace the term, drop it, and only then a parenthetical gloss. The single exception is named metrics whose meaning a substitution would change (basis points, core PCE, EBITDA, P/E, EV/EBITDA, ROIC) — those keep the name and take a first-use gloss, then run bare. Desk idiom ("got hit", "caught a bid", "bear-steepener", "the long end", "de-grossing") has no definitional precision to protect and always gets rewritten. The system prompt carries a table of 20+ terms with plain-English equivalents; those are starting points for a rewrite, not text to paste in parens.

`pulse_lint` enforces this with `gloss-density` (inline definitions per 100 words, soft, limit 0.45), which replaced the retired `jargon-bare` check. `jargon-bare` flagged term *presence* and could not tell a glossed term from a bare one, so it fired on correct prose, could only be satisfied by deleting nomenclature the contract requires keeping, and pushed the writer toward glossing.

**Ingestion does NOT translate, by design.** `ANALYSIS_SYSTEM_PROMPT` enforces register (neutral, factual, numbers and mechanisms not adjectives, no em-dashes or semicolons) but preserves the source's terminology. Deep analysis is a fidelity layer and the only surviving artifact of the PDF, so translating there would lose precision before anything knows which terms reach the pulse. All translation is DRAFT/AUDIT work.

### Data sources layered at synthesis
1. **Research PDFs = primary content driver** across all sections
2. **Live market data** (CoinGecko BTC/ETH/SOL + Yahoo Finance S&P/VIX/oil/gold/10Y/DXY) — used in RECAP only, ground prices in current reality
3. **Live news** (Finnhub market news, last 48h) — used in RECAP only, catches weekend/overnight events
4. **Finnhub earnings + economic calendars** — verification only, not a content source. Hard-filtered at the data layer: only MAG7/top banks/bellwethers earnings + macro whitelist (CPI/PCE/NFP/GDP/Retail Sales/ISM/PPI/FOMC/Powell + ECB/BOJ/BOE rate decisions)
5. **Previous pulse markdown** (scheduled only) — diff baseline, not template

Per-PDF JSON passed to synthesis includes: source, title, type, priority, published date, key_insights, market_movers (with conviction), sector_views, earnings_insights, macro_indicators, crypto_views, vol_and_positioning, trade_ideas (with time_horizon), risk_factors, cross_bank_references, entities_mentioned, charts_described.

## Module Guide

| Module | Purpose |
|---|---|
| `config.py` | All settings from env vars via pydantic-settings |
| `channel_config.py` | Which Discord channel is which, by permanent channel ID first and name as a fallback (2026-10-04, after kloh's alert channel was renamed and the bot silently stopped reading it). Every channel check goes through it: `ingests` (every channel except `chat_ingest_exclude`, test-channel, owner call; new channels included automatically), `eager_ocr` (the alert rooms), `caller_for` (by the caller's `channel_id`), `is_command_channel` (admin commands, test-channel). Settings hold IDs with the name in a comment. A daily 9:05 ET job pings ops when a configured channel is gone |
| `db.py` | SQLite core: connection model, schema, migrations, shared helpers, and the FACADE that re-exports every query helper from `db_parts/` (split by subject 2026-09-01: `pdf`, `chat`, `pulse`, `analyst`, `summaries`, `ask`). Always `import db` and call `db.<name>`; inside `db_parts/` every db call reads `_db.<name>` so facade patches keep working. New helper: put it in the subject module and add it to the facade import list at the bottom of `db.py` |
| `dropbox_client/watcher.py` | Cursor-based Dropbox polling + download |
| `pdf_processing/extractor.py` | PyMuPDF text extraction; page-image rendering used only by the multimodal carve-out |
| `pdf_processing/page_selector.py` | Multi-signal page scoring. LIVE again since 2026-05-07 for the narrow multimodal carve-out (see Key Design Decisions); imported by `ai_analysis/analyzer.py` |
| `ai_analysis/prompts.py` | Gemini prompt templates (triage, deep analysis, synthesis) |
| `ai_analysis/analyzer.py` | Gemini orchestrator (triage + deep analysis, text-only) |
| `ai_analysis/rate_limiter.py` | Concurrency + RPM management |
| `ai_analysis/usage_ledger.py` | Gemini call ledger (2026-09-29): `make_client(caller)` wraps a genai client so every generate_content lands in `gemini_calls`; `as_caller(name)` narrows the label inside one client; `db.gemini_spend(days)` prices it per feature for `/status` |
| `ai_analysis/models.py` | Dataclasses: TriageResult, PdfAnalysis, MarketMover, SectorView, MacroIndicator, TradeIdea, EntityMention |
| `report/synthesizer.py` | Cross-PDF synthesis via Gemini; builds ticker map; handles prev_pulse context |
| `report/market_data.py` | CoinGecko + Yahoo Finance live price snapshot |
| `report/news_data.py` | Finnhub news + earnings calendar + economic calendar (all hard-filtered) |
| `report/calendar_data.py` / `calendar_render.py` / `implied_move.py` | The omni-calendar sheet. Posts 3:00 PM ET for the next session (an hour before the close, so the BEFORE OPEN column is bettable), refreshed in place 7:30 AM ET when the lineup changed. Rows: a Finnhub row must also be on Nasdaq's earnings calendar for the same date, and a Nasdaq-only name at or above the $5B floor is added with Nasdaq's session; Nasdaq's session also fills a blank Finnhub hour and wins a conflict (`fetch_nasdaq_earnings_rows`, 2026-09-14/16/17; Finnhub alone when Nasdaq is down). An empty band prints `no names at scale confirmed` so a quiet week is not read as a feed failure. TOP_N=15 per session, cap-ranked; below the $5B floor a name needs a confirmed session AND a priced ATM straddle; above it a name renders unpriced with a dash, but only if Yahoo lists options for it (PDI, 2026-09-02). Econ rows: every US event except Fed officials' speeches other than the chair's (`is_non_chair_fed_speech`, owner call 2026-09-30; stricter than the pulse, which also passes the predecessor), Tier-1 series and high-impact prints bold (`econ_is_important`). Company rows bold (`earn_is_important`) when on the pulse's major-ticker list, $50B+ cap, or named inside a bank's earnings insight in the last 7 days (`db.recently_covered_tickers`) |
| `report/formatter.py` | Discord embed formatting with color-coded sections + dynamic footer |
| `discord_bot/bot.py` | Discord bot with /pulse, /status, /load, /reanalyze, /clearqueue, /seedcursor, /reprocess. `_answer_with_gemini` is the /ask pipeline: ~380 lines that call eleven phase functions `_ask_00_setup_tools_and_context` .. `_ask_10_log_and_render` in order (split 2026-09-01, text verbatim, explicit parameter/return interfaces; `_AskEarly` carries the budget refusal out of phase 2). Edit the phase, not the caller |
| `discord_bot/ask_tools.py` | The /ask tool layer: `_build_*_tool` declarations and `_execute_*` executors (extracted from bot.py 2026-09-01; bot.py re-exports every name) |
| `discord_bot/race_tagger.py` / `rank_evidence.py` | The racism board (2026-09-26). Every chat message is tagged once as race-edged or not (`race_tags`; racial-slur regex first, Gemini batches for the rest, a refused message stored as -1), every 15 minutes over the last 60 days. `db.race_board` ranks by race-edged messages over 30 days. `user_profiles.racial_humor_score` is retired and written NULL. Every `lookup_user_profile` rank carries its evidence (30-day count and recent examples, or the 21-day trade ledger), and `rank_evidence.footer` appends it to the reply as an Evidence block in code |
| `analyst_log/member_batch.py` | Member (non-caller) text posts in the alert channels, read every 30 minutes per channel instead of one Gemini call per message (2026-09-26). Official callers and screenshots stay on the live path in `analyst_log/watcher.py`. Each call carries every message's own date, its reply parent, and the author's last three earlier posts; code checks drop trades whose ticker or strike is not in the text, whose ticker is a member's name, or that come from reactions and questions. Position per channel is a `chat_messages.id` in `member_batch_state`, so catch-up rows with older timestamps are still read. `MEMBER_TRADE_BATCH_ENABLED=false` returns member posts to the live path |
| `discord_bot/room_rank.py` | Room rankings (2026-10-02). `lookup_user_profile` ranks only trading and racism, and `gate` refuses a metric call the conversation never asked for (a "most gay" follow-up was answered with the racism board on 2026-10-01). A "who's the most X" question about any other trait gets `superlative_note`: pick real people from the profiles and chat, no data ranking (owner call). Both are mirrored in `scripts/ask_fixture_run.py` |
| `discord_bot/tool_docs.py` | Routing text for every tool (WHEN TO CALL / DO NOT use); tested by `tests/test_tool_docs.py` |
| `scripts/ask_live.py` + `.claude/skills/ask-harness/` | The /ask live replica (2026-09-30): runs one question through the real `_answer_with_gemini` on the worker with the live DB read-only and prints the route, tool trace and the exact embed text. The skill documents the command, flags and the three deviations from the channel (writes captured, chat rebuilt from `chat_messages`, no `<@id>` resolution). Use it for every sample shown to the owner; `scripts/ask_fixture_run.py` stays the stubbed regression suite |
| `discord_bot/ops_alert.py` | Ops pings to `OPS_ALERT_CHANNEL_ID` from any context |
| `report/x_client.py` / `calendar_caption.py` | X (Twitter) posting, stdlib OAuth 1.0a (2026-09-11). The nightly calendar job posts the same sheet to X after the Discord post succeeds; `X_POST_ENABLED=false` is a logged dry run; one post per date via `/data/x-posts`. Credentials: `scripts/x_post_test.py --check` (read-only). Test post: `touch /data/x-requests/post-calendar` on the worker, owner-approved only; the worker posts within a minute and writes `post-calendar.result` (never `--post` from a second process, it writes to the live DB) |
| `report/print_watch.py` | Economic prints posted at release (2026-09-11): armed at 8:29 and 13:59 ET on days the ForexFactory feed OR the agencies' published schedule (`OFFICIAL_RELEASES`, update annually) lists CPI, the jobs report, PCE (2026-09-17, BEA API, `BEA_API_KEY` set 2026-09-25) or an FOMC decision; pings ops when a scheduled release lacks its key or is not published in time, polls the BLS/BEA APIs / the Fed's press feed until the reference month appears, posts actual / consensus / prior to `PRINT_ALERT_CHANNEL_ID` (falls back to `REMINDER_CHANNEL_ID`). Also the FIRST source of `actual` on econ-calendar rows, ahead of `report/fred_data.py`, which lags releases by hours and has no October 2025 observation. Since 2026-09-30 the embed is the owner's bullet layout (core lines first, m/m and y/y pairs on one line, optional extra series from BEA tables 2.6 and 2.8.6 and BLS components, computed lines such as the 3-month annualized core rate and the prior-month payroll revision), then a Quick Takeaway from `report/print_takeaway.py` (Gemini over the print rows plus the last ten days of bank research, tier-1 desks first, figure and voice guarded, 20 s timeout, omitted on any failure), then the agency source line. The ledger under `/data/print-alerts/` keeps the rows and the FOMC statement body so next month's revision and statement-change lines have something to compare against |
| `discord_bot/sender.py` | Embed delivery (per-embed = separate message — batching was reverted) |
| `pipeline/orchestrator.py` | End-to-end pipeline coordination |
| `scheduler/jobs.py` | APScheduler cron jobs (15-min poll, 5-min process; Gemini pulse job skipped — bridge/routine owns the 10 AM ET pulse) |
| `test_pulse.py` | CLI tool for manual testing |
| `inspect_db.py` | CLI for browsing DB state |
| `main.py` | Entry point |

### /ask: the deterministic router (discord_bot/ask_router.py)

Since 2026-09-02 the question is shaped in CODE before the model sees it. `ask_router.classify()` returns a shape (earnings slate, single-ticker earnings, price, options chain, econ calendar, price history, company profile, member ledger, chat history, fantasy, historical statistic, news/why, ticker opinion, or unknown). A ticker-opinion question ("what do you think of MU into earnings") prefetches `lookup_research` (`discord_bot/research_tool.py`, `db.research_for_ticker`: the last 14 days of bank notes naming the ticker, reduced to their calls, earnings lines and insights about it) as the authoritative block, so a stock view leads with named desks. Every single-stock shape (view, options, earnings date, price, price history, profile, news, and a bare "MU?" or "$aeva") also prefetches `lookup_ticker_snapshot` (`discord_bot/snapshot_tool.py`, `report/ticker_snapshot.py`, 2026-09-30, the owner's swing-trader triage): what the company does and its industry, market cap, 52-week range, ATR, beta, 30-day volume in shares and dollars with the latest session's relative volume, float, short interest with its report date, cash, debt, cash flow, and S-1/S-3/424B5 filings in the last year, from Yahoo. It narrates and never grades (owner: no thresholds, the reader judges); funds, indices and coins are skipped (`ask_router.is_stock`). An options question on a stock now gets the research, snapshot, earnings date and Google too, not just the chain. The view, options, news and profile shapes (and an earnings question about a result) also run `ticker_news` (`discord_bot/news_tool.py`), a Google-grounded Flash-Lite search made in code, because the model skips Google when a research payload is present and answered MU from pre-print previews two hours after the print. It returns dated lines only, cached 10 minutes per symbol; `data_footer.compose` cites its links only when the answer uses a figure from it. The chain summary carries the at-the-money straddle as `implied_move_dollars`/`implied_move_pct`, and earnings rows carry `when` in New York terms ("tomorrow (Thu Oct 1), before market open"). Every stock shape also prefetches `ticker_primer` (`discord_bot/primer_tool.py`, table `ticker_primers`, 30-day life): five labelled lines (what it sells, segments with revenue share, which segment drives growth and margin, the figures it is judged on, outside drivers) from a grounded call, stored only when sourced; a view question waits up to 7 s for a cold build, a price or date lookup starts it in the background, and a build that fails every attempt is not retried for 6 hours. Phase 9 ends with `_business_line_guard` (`discord_bot/business_line.py`): on a view, options, news or profile question with a primer injected, an answer that names no segment or product line from it is rewritten once to tie its figures to the business line; the rewrite is kept only if it keeps every figure and now names one. Canned fallbacks and figure-less answers are never rewritten. The named line must be the one the primer's DRIVERS line calls the driver when it names one (owner, 2026-10-01). `_implied_move_guard` follows: an options question about a print whose chain priced a move must state it (percent or dollars), else one rewrite adds it without dropping the business line. The snapshot also carries growth computed from Yahoo (`growth_block`): the coming quarter's and fiscal year's consensus with y/y growth, the last four EPS reports against estimates, reported revenue y/y; the model is told to lead with growth and give the dollar figure beside it. EPS history comes from Yahoo's earnings dates (report date and session, not quarter end). When the last report is within 3 days (`ask_router.fresh_print`), the research block opens with "RESULTS ARE OUT" and tags notes written before it PREVIEW, and `_fresh_print_guard` (first in the phase-9 chain) requires the printed EPS in the answer; a scheduled release whose time passed before Yahoo filled the actual still tags the previews. **Outline first (2026-10-01, owner-approved redesign):** for a view, options, news or profile question on one stock, `discord_bot/stock_outline.py` builds an ANSWER OUTLINE from the prefetch results and phase 2 appends it as the last block: PRICE, then RESULT (a print in the last 3 days, with the news lines and their publishers) or UPCOMING (next report, consensus growth, last beat), DRIVER (the primer's DRIVERS and WATCHED lines), up to two DESK slots (one note per bank, calls and high conviction first, the desk's own figures appended, PREVIEW-tagged before a print), OPTIONS (the straddle move, last live quotes, or "not quoting"), POSITIONING (short interest with its date, distance from the high, an offering filing). Each slot names its true source; the model writes one arrow per slot. The rewrite guards in phase 9 stay as backstops. Outside market hours Yahoo's chain has zero bids, zero open interest and a placeholder IV: `summarize_options_chain` marks `live_quotes` false and drops IV and OI, and the options tool serves the last live read it saw (20 h) labelled with its time, or a note that options are not quoting. Chat search is a room tool: it is offered only on the ledger, chat-history, fantasy and banter shapes, never on a research or data shape or the catch-all (2026-09-30). A recognised shape (a) restricts the declared tool list to what that shape may use (`TOOL_POLICY`, `filter_tools`; chat search is unreachable from a price question, Google from a ledger question), (b) schedules a mandatory prefetch that phase 2 executes before the first model call and injects as an authoritative block (`inject_text`), and (c) sets the WEB/LOCAL and FACT/BANTER flags without the Gemini classifier call. Unknown shapes keep the full tool list and the classifier. `scripts/ask_fixture_run.py` mirrors the router with fixture `tool_stubs`, so fixtures measure deployed routing. Add a shape by extending the regex table and `tests/test_ask_router.py` (labelled real room questions), then delete the prompt text the shape makes redundant (the policy above). The prompt's "tool-first" routing paragraphs were deleted the same day; the post-hoc grounding nets and validators stay as the backstop.

## Discord Commands

Password gate: `COMMAND_PASSWORD=<set-in-railway-env>` env var. Gated commands take `password` arg.

Channel allowlist: pulse/admin commands (everything except `/ask`) only execute in channels listed in `PULSE_COMMAND_CHANNELS` (env var, comma-separated channel names, default `"test,tldr"`). Empty value disables the restriction. Discord still lists the commands in the global picker — the bouncer fires on execution, replying with an ephemeral "command not available here" message.

**Visible in slash menu (currently registered):**
- `/ask question:X` — Gemini-grounded web-search Q&A (Google Search tool). Works in **every** channel (not gated by `PULSE_COMMAND_CHANNELS`). Per-user daily cap from `ASK_DAILY_QUOTA_PER_USER` (default 40); resets at UTC midnight. Reuses the existing `GOOGLE_API_KEY` env var. Also responds to `@bot question` mentions in any channel. Free tier on Gemini 3.x = **5,000 grounded prompts/month** (shared across the Google AI Studio account); paid overage is ~$14 per 1000 queries.
- `/status` — dashboard: today's ingestion + total DB state + priority mix (always shows high/medium/low even if 0) + upload range + all-time tokens + last pulse times + Dropbox cursor state + upload volume (24h + since last scheduled) + last 5 ingested filenames (in configured timezone). Channel-allowlisted.
- `/reanalyze hours:N password:<your-command-password>` — re-analyze PDFs already in DB with current prompt (appends new pdf_analyses rows; old preserved). Channel-allowlisted + password-gated.

**Disabled in slash menu, code preserved in `discord_bot/bot.py`** — function bodies are intact; only the `@bot.tree.command` and `@app_commands.describe` decorators are commented out (search file for `DISABLED in slash menu`). Uncomment the decorator lines above the function to re-expose it. Disabled 2026-05-14:
- `/pulse [hours:N]` — manual pulse synthesis. Scheduled pulse runs daily; `/reanalyze` also drives the synthesis pipeline internally, so this is rarely needed.
- `/load hours:N password:<your-command-password>` — manual Dropbox ingest. Auto-polling every 15 min already covers this.
- `/clearqueue password:<your-command-password> [confirm:true]` — destructive queue purge. Run `db.clear_pending_queue()` via `railway ssh` if needed.
- `/seedcursor password:<your-command-password>` — one-shot Dropbox-cursor recovery tool.
- `/reprocess filename:X` — manual retry of a single failed PDF (auto-retry covers this via `MAX_RETRY_COUNT`).

## Deployment

**Dependencies are `==`-pinned to the production environment** (`requirements.txt`, refreshed from `requirements.lock`, 2026-09-01). Bump a pin deliberately, deploy, then refresh the lock. `scripts/preflight_push.py` refuses an unpinned line.

**External heartbeat:** `.github/workflows/heartbeat.yml` curls the public `/healthz` every 30 min and fails loudly (GitHub email, plus the ops channel if the `DISCORD_OPS_WEBHOOK` repo secret is set). The worker cannot page for its own boot-time crash; this is the watchdog.

**Smoke tiers:** `scripts/smoke_manifest.json` lists every `scripts/smoke_*.py` with a tier. `py -3.12 scripts/run_smokes.py` runs the fast tier (preflight runs it too); `--full` runs everything not retired. A smoke on disk that is missing from the manifest fails the gate.

Railway project **`marvelous-dream`**, service **`worker`**, environment **`production`**. Volume mounted at **`/data`** (SQLite DB + temp PDFs). Every `git push` to the working branch auto-redeploys.

### Accessing production state

**If you (Claude) have shell access** (Claude Code desktop with terminal):
```bash
railway logs --deployment | tail -50                        # recent logs
railway logs --deployment 2>&1 | grep -iE "ERROR|failed"     # filter for issues
railway variables --service worker                            # list env vars (human-readable)
railway variables --service worker --kv                       # list env vars (KEY=value)
railway variable set --service worker "KEY=value"             # set env var (triggers redeploy)
railway ssh "python -c 'import sqlite3; ...'"                 # query prod DB directly
```

Railway CLI is authenticated via `railway login` (already cached on the user's machine). The project is already linked from this repo's directory.

**Never `import db` in a second process against the live volume.** `db.get_connection()` runs `_ensure_schema` (an `executescript` of CREATE/ALTER statements) the first time a process opens the DB, which takes a write lock. Inside the worker that runs once at boot; from a `railway ssh` probe it contends with the live worker and, on 2026-09-04, a probe hung on that lock for 30 minutes while the worker logged `database is locked` 16 times, including a failed Dropbox poll. Probe production data with a read-only connection that cannot contend: `sqlite3.connect('file:/data/reports.db?mode=ro', uri=True, timeout=5)`, and write the query by hand. Importing `report.*` or `discord_bot.*` modules is fine as long as nothing in the probe touches `db.get_connection()`.

**If you (Claude) don't have shell access** (mobile app, web UI, or any env without terminal):

You cannot run `railway` commands yourself. Ask the user to run them locally and paste output. Structured request pattern that works well:

> "I need to check production state. Could you run this in your terminal and paste the output?"
> ```
> cd c:/Users/gabje/Institutional-report-bot
> railway logs --deployment | tail -100
> ```

Common requests worth pre-writing for the user:

| What you need | Command for user to run |
|---|---|
| Recent logs | `railway logs --deployment \| tail -100` |
| Error patterns | `railway logs --deployment 2>&1 \| grep -iE "ERROR\|failed\|Traceback"` |
| List env vars | `railway variables --service worker` |
| Query prod DB | `railway ssh 'python -c "import sqlite3; ..."'` (write the Python carefully — no complex quoting) |
| Deploy status | Ask user to check Railway dashboard (deploy tab) |

Alternative for log inspection: user can run `/status` in Discord which surfaces most health signals without needing terminal access. That's usually faster for quick health checks than pulling raw logs.

**DB schema + contents** without shell access: read `db.py` for schema. For data inspection, ask user to run `inspect_db.py` locally (has pre-built CLI views for recent PDFs, analyses, reports, logs).

## Environment Variables (on Railway)

Key ones set on `worker` service:
- `DROPBOX_APP_KEY`, `DROPBOX_APP_SECRET`, `DROPBOX_REFRESH_TOKEN` — Dropbox OAuth2
- `DROPBOX_FOLDER_PATH=/Current`
- `GOOGLE_API_KEY`, `GEMINI_MODEL=gemini-3.1-flash-lite`, `GEMINI_TRIAGE_MODEL=gemini-3.1-flash-lite`
- `DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID` (comma-separated list of channel IDs)
- `FINNHUB_APi_KEY` (note lowercase 'i' typo — pydantic-settings is case-insensitive so it works; don't fix cosmetically without reason)
- `COMMAND_PASSWORD=<set-in-railway-env>`
- `TIMEZONE=America/New_York`
- `DAILY_PULSE_HOUR=10`, `DAILY_PULSE_MINUTE=0`
- `DB_PATH=/data/reports.db`, `PDF_DOWNLOAD_DIR=/data/pdfs` (MUST use leading slash — relative paths write to ephemeral container storage and get wiped on redeploy)
- `OPS_ALERT_CHANNEL_ID` — one-line ops pings via REST (`discord_bot/ops_alert.py`; `ops_alert()` on the loop, `ops_alert_sync()` from worker threads; 1 h dedupe per key).
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`, `X_POST_ENABLED` — X auto-post of the omni-calendar (`report/x_client.py`). OAuth 1.0a user context for the posting account, app permission Read and Write. Never paste these into chat; set them with `railway variable set`.
- `PRINT_ALERT_CHANNEL_ID` (optional, falls back to `REMINDER_CHANNEL_ID`) and `BLS_API_KEY` (optional, free; 500 requests/day instead of 25) — economic prints at release, `report/print_watch.py`. The watch is off when neither channel is set.
- `MALLOC_ARENA_MAX=2` — caps glibc malloc arenas. Without it, the ~30 asyncio worker threads each get their own arena and freed PDF/image buffers never return to the OS; RSS ratchets to ~1.26 GB and Railway bills ~$10/GB-month for it (set 2026-07-23, cut RSS to ~180 MB at boot). Don't remove.

## Database

SQLite with WAL mode at `/data/reports.db` (persists on Railway volume).

**Connection model (2026-09-01):** the main thread keeps the module-level `db._conn`; every other thread gets its own connection from a `threading.local` (`db.get_connection()` handles both). One shared connection across threads let thread B's `commit()` commit thread A's half-written rows. Every DML helper in `db.py` commits before returning; keep it that way, a cross-thread reader never sees uncommitted rows now. Tests use `db.reset_connections()`; the legacy `db._conn = None` reset still works on the main thread.

Tables:
- `dropbox_state` — cursor for delta polling
- `pdf_files` — status tracking (DOWNLOADED → PROCESSING → PROCESSED / FAILED)
- `pdf_analyses` — **append-only** per-PDF structured JSON results + token usage. UNIQUE constraint was dropped so reanalyses create new rows, old ones preserved as history. Queries use `MAX(id) GROUP BY pdf_file_id` CTE to get the latest analysis per PDF.
- `daily_reports` — **append-only** synthesized pulses. UNIQUE(report_date, report_type) also dropped. `report_type='daily'` for scheduled, `'manual'` for manual.
- `processing_log` — audit trail

Migration on boot: `_migrate_drop_unique_constraints` rebuilds tables without UNIQUEs if the old schema is detected.

## Dropbox Structure (Live)

Root: `/Current`
```
/Current/2026/April/Apr 15/
  Goldman/     # biggest volume, ~50-80 PDFs/day
    S&T/       # Sales & Trading — chart-of-day, flow notes
  JPM/
  Citi/
  BofA/        # Hartnett Flow Show, Morning Tidbits, Economic Weekly
  UBS/
  RBC/
  Barclays/
  Deutsche Bank/
  TME/         # The Market Ear — short vol/positioning pieces
  ANZ/, ING/, Mizuho/, MUFG/, Rabobank/, TS Lombard/, Other/
```

/Current volume: ~100-200 PDFs/day across all sources.

## Market context

Do not hardcode the tape or the geopolitics in this file; both move daily. The live pulse (`pulse-output/archive/` on the `pulse-data` branch) is the current read, and `world_context.py` carries the few facts the pipeline depends on (Fed chair, NYSE holidays, market-holiday helpers). Update `world_context.py` when a fact there changes, not this section.

## Cost Monitoring

Measured 2026-09-26 from the AI Studio spend page (project **BESS**, `gen-lang-client-0723421357`, the bot's key) for Aug 30 - Sep 26: **$50.81 Gemini** ($35.95 gemini-3.1-flash-lite, $12.30 gemini-3.5-flash-lite, the rest other SKUs). Railway is ~$4 of usage against the $5 Hobby minimum (worker: ~377 MB, <0.01 vCPU, 0.5 GB volume).
- gemini-3.1-flash-lite ($0.25/M in, $1.50/M out): PDF triage + deep analysis (~60M in / 3.8M out a month, recorded in `pdf_analyses`), the alert-channel trade classifier (`analyst_log/watcher.py`, pre-filtered 2026-09-26, was ~41,900 calls a month for ~900 trades), screenshot OCR.
- gemini-3.5-flash-lite ($0.30/M in, $2.50/M out): /ask and the profile refresh.
- **Per-feature spend is measured since 2026-09-29**, not estimated: every live Gemini client is built with `ai_analysis.usage_ledger.make_client(caller)`, which records each call's usage_metadata in `gemini_calls`. `db.gemini_spend(days)` prices the rows per model (`PRICES_PER_M`, list prices) and `/status` shows the last 7 days by caller (`pdf_triage`, `pdf_deep`, `ask`, `ask_news`, `ask_primer`, `profile_refresh`, `trade_classifier`, `screenshot_ocr`, `member_trade_batch`, `race_tagger`, `chat_ocr`). The AI Studio spend page remains the bill; the ledger says which feature ran it up. A new Gemini call site must use `make_client`, never a bare `genai.Client`.
- Google Search grounding: 5,000 free a month across Gemini 3.x, then $14 per 1,000. /ask runs ~400 questions a month.
- Spend cap at ai.studio/spend is $90/month. AI Studio also shows a prepay switch dated October 12 after which requests fail until credits are bought; that is the owner's billing action.

## Web embed integration — cross-repo boundary

**The production daily-pulse page is hosted in a SEPARATE repo:** [gabjew90/Stock-market-dashboard](https://github.com/gabjew90/Stock-market-dashboard) → published at **https://gabjew90.github.io/Stock-market-dashboard/pulse/**. That dashboard renders the pulse content this bot publishes. The two repos communicate only via a public URL contract on the `pulse-data` branch.

**Boundary — who owns what:**

| Concern | Lives in | Why |
|---|---|---|
| Pulse content (voice, themes, RECAP/INSIGHTS/WATCH structure, cashtag rules, QC criteria) | **This repo** (Institutional-report-bot) | DRAFT/AUDIT/SCRUB prompts + voice_rules.py + theme clustering |
| HTML class structure the pulse fragment emits (`.pulse h2.recap`, `.pulse .cashtag`, `.pulse-masthead`, etc.) | **This repo** | `scripts/pulse_dashboard.py :: render_pulse_fragment()` |
| `archive.json` schema (fields per entry: ts, title, date_utc, pdf_count, archive_url, fragment_url) | **This repo** | `github_bridge/jobs.py :: publish_web_fragment_job()` |
| Page layout, # of pulses shown per page, pagination logic (weekly view, prev/next), nav chips, colors, fonts of the embed | **OTHER repo** (Stock-market-dashboard) | `web/pulse.html` there is a self-contained static page that fetches our archive.json + fragments at runtime |
| Hosting, GitHub Pages workflow, the `/pulse/` URL | **OTHER repo** | Their daily-gmi.yml workflow stages `web/pulse.html` into `_site/pulse/index.html` |

**Steering rule when a user asks for a change in this repo's context:**

- "Change the page layout / show more pulses per page / add infinite scroll / change the nav bar / change page colors" → **redirect to the Stock-market-dashboard repo**. Don't make changes here for those — there's nothing to change here that would affect the page. Tell the user to open a Claude Code session against `c:\Users\gabje\dev\Stock-market-dashboard\` (clone exists) or pull fresh and work there.
- "Change what the pulse says / add a new section / change voice / fix a fact / change which research gets in" → **this repo**. The fragment's content + class hooks are owned here.
- "Add a new section like POSITIONING alongside RECAP/INSIGHTS/WATCH" → **both, in order**: I add the new class hook upstream (this repo), the other session adds CSS for it.
- "Change the archive.json schema / add a field" → **both, in order**: change `publish_web_fragment_job` + cached-entry handling here, then the other session updates its JS to read the new field.
- "Switch the data host / migrate from raw.githack.com to something else" → **both** — both sides have URL constants. Coordinate via the user.

The pulse fragment classes are a **stable contract**. Don't rename `.pulse`, `.pulse h2.recap`, `.pulse-masthead`, `.cashtag`, `.recap-body`, `.insights-body`, `.watch-body`, etc. without coordinating with the dashboard repo — their CSS targets these exact selectors. If a rename is unavoidable, ship a transition period where both old and new class names are emitted (e.g., `class="pulse-masthead pulse-header"`) so the dashboard side can update without a hard break.

**Backfill policy** (worth knowing when a user asks "why aren't past pulses showing up"): the bridge worker's `publish_web_fragment_job` only renders the HTML fragment for the **single most recent** archived pulse. Older entries in `archive.json` are minimal stubs (just `{ts, filename, archive_url}` — no `fragment_url`). The dashboard's pulse page filters stubs out, so historical pulses don't appear until a fragment exists. To backfill (e.g., to populate a full historical week-by-week view), a one-shot script is needed; none exists yet.

## Shadow pilot (claim-card redesign)

Runs beside production on the `pilot-data` orphan branch and GitHub Actions; production is never written by any pilot job. **Omnipulse trial (2026-09-26, spec `docs/superpowers/specs/2026-09-26-omnipulse-body-in-production.md`):** when `ENABLED` in `scripts/omnipulse_body.py` is True, the synthesis routine's STEP 2.4 (`pulse_driver.py gate omnipulse`) takes the day's `pilot/shadow/<date>.clean.md` as the pulse headline, MAIN EVENT and BRIEFS; the routine still writes RECAP, WHAT TO WATCH and `_LEANS`. The driver re-applies the body after DRAFT and after EDIT, exempts theme-choice checks, keeps the adversarial gate to RECAP/WATCH, and restores the body at preflight if a late pass broke it. No usable Omnipulse within 20 minutes = the classic pulse. Frontmatter `body_source` records which ran. `ENABLED` is True since 2026-09-26 (first pulse Monday 9/28), with MAIN EVENT plus at most 5 BRIEFS (`MAX_BRIEFS`). Environment `OMNIPULSE_BODY=off` forces the classic pulse for one process (smokes set it; use it for a test fire) without touching the committed switch. `MISS_DAY` in the same file decides a day with no usable Omnipulse: `"classic"` (current) runs the classic pulse, `"light"` (owner option a, 2026-09-29, spec `docs/superpowers/specs/2026-09-29-light-pulse-miss-day-design.md`) publishes RECAP, TRADE BOARD and WHAT TO WATCH around a one-paragraph note (`DECISION: LIGHT`, frontmatter `body_source: light` and `light_reason`, one ops page from the bridge). A light day whose reason is a fetch error still gets one backup read. It flips in the retirement commit, gated on 10 consecutive market days with `body_source: omnipulse`. Runbook: `docs/superpowers/routines/pilot/RUNBOOK.md`. Frozen grader prompts and fixtures live under `docs/superpowers/routines/pilot/`; a change to them or to a model string in `scripts/pilot_config.py` restarts the pilot clock. The daily-qc pulse judge ignores the pilot tree by instruction. `PILOT_PUBLISH_ENABLED` on the Railway worker is the on/off switch for the whole chain. `PILOT_DISPATCH_ENABLED` makes the worker dispatch the pilot workflows on its own clock (GitHub's cron dropped most runs on 2026-09-02); it needs Actions: read and write on `GITHUB_TOKEN`. A dispatch is skipped while the workflow has a run queued (2026-09-16), and no pilot workflow declares a `schedule:` of its own (2026-09-18: cron plus dispatch fired the same minute and the concurrency group cancelled the loser). The editor workflow waits up to 40 minutes for the readers to finish before it packs.

## Session ledger

Dated entries live in `NOTES.md` (newest at the bottom); this file carries only the standing structure. The structural changes of 2026-09-01/02 that a new session must know: `db.py` is a facade over `db_parts/` with per-thread connections; `/ask` runs as eleven `_ask_NN_*` phases behind a deterministic router (`discord_bot/ask_router.py`) with the tool layer in `discord_bot/ask_tools.py`; dependencies are `==`-pinned and every push runs `scripts/preflight_push.py` (including the fast smoke tier) through a local pre-push hook; the shadow pilot runs on the `pilot-data` branch, dispatched from the worker's clock.

## Next Steps / TODO

- Shadow pilot: two clean shakedown days, then commit `pilot/DAY1` on `pilot-data` and freeze prompts and model strings (runbook: `docs/superpowers/routines/pilot/RUNBOOK.md`).
- /ask structural fix 2: a general figure-provenance check (every figure in a factual answer must appear in a tool payload or grounding snippet, else one grounded retry, else strip), replacing the per-shape `unforced-*` validators. The pilot's citation verifier already has the matcher.
- Pilot headroom (plan section 5): answered by the 2026-09-15 weekly-limit outage (pilot plus pulse did not fit one week with Opus readers); readers moved to Sonnet 2026-09-16. Runner minutes are measurable and were 630 for readers alone on 2026-09-04.
- Prompt diet continues as router shapes retire prompt text (policy at the top of this file).
- Calendar refresh (`CALENDAR_REFRESH_ENABLED`, off): before re-enabling, give the 7:30 AM job a row-level merge that keeps the 3 PM priced moves (`calendar_posts.lineup_json` exists for this and is never written). Rebuilding from pre-market chains downgrades priced rows to dashes.
