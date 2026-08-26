"""Per-tool documentation, prepended to each tool's schema description.

WHY THIS FILE EXISTS
====================
This text used to live in the `## TOOLS` section of the /ask system
prompt, which was 18,735 chars — most of it parameter semantics, status
codes, usage shapes and per-tool "when to call" detail. All of that is
tool-selection material: the model needs it when deciding which tool to
reach for, and a FunctionDeclaration is where it belongs.

CLAUDE.md's prompt-enforcement policy, rule 2: "Tool mechanics belong in
tool declarations, not the system prompt. Parameter semantics, status
handling, and per-tool usage shapes go in the schema. Only cross-tool
routing priority stays in the prompt."

WHAT STAYED IN THE PROMPT
=========================
Cross-tool routing priority, the HARD ROUTING RULES block, and the
anti-fabrication rules that span tools. Nothing here duplicates those —
a rule lives in exactly one place, per CLAUDE.md rule 1.

HOW IT IS WIRED
===============
`_with_tool_docs()` in bot.py prepends `TOOL_DOCS[name]` to each
FunctionDeclaration's description at build time, so every consumer of the
builders — the bot and scripts/ask_fixture_run.py alike — sees the same
text. Editing a tool's docs here changes what the model reads during
selection without touching the prompt budget.
"""

# Appended to every tool's docs: how to read the envelope and how to
# speak the result. Kept short deliberately — declarations ship on every
# turn, so a paragraph duplicated ten times is not a saving.
_COMMON = (
    "\nRESPONSE ENVELOPE: read the top-level `status` before composing. "
    "`ok` use the data. `empty` say 'nothing logged in that window' — do "
    "NOT fabricate. `not_found` say you don't see that user/row, "
    "naturally. `error` retry once with corrected args or say the lookup "
    "failed — never claim you found something you didn't. Freshness: "
    "`as_of` 'now' for chat/trade phrasing; a days-stale `updated_at` "
    "gets hedged with the date; reflect `window_days`/`window_start`/"
    "`window_end` in your phrasing, never claim coverage beyond the "
    "window. Weave the result in as something you just know — never 'I "
    "searched and found'.\n"
)

_NO_SELF_TA = (
    "\nNO SELF-GENERATED TECHNICAL ANALYSIS applies to anything you say "
    "about this data — see that binding rule in the system prompt. You "
    "have no chart view and no indicator feed; relay a level only with "
    "its named source attached.\n"
)

TOOL_DOCS: dict[str, str] = {}

TOOL_DOCS["search_chat_messages"] = (
    "WHEN TO CALL: THIS server's chat history — when the asker "
    "references something the room discussed that isn't in your "
    "pre-injected blocks. Returns up to 20 matches. Call for: 'did we "
    "ever talk about CRWV', 'what was that trade @BK posted last "
    "Wednesday', 'how many times has @BK said X' (keyword + username, "
    "count from the result), and follow-ups where the subject's profile "
    "is already tapped out — `username=<them>, days=7`, no keyword — for "
    "fresh raw chat material. Do NOT call when the answer is already in "
    "your blocks, for external facts (Google), or with a generic "
    "keyword — only with a clear, specific one."
) + _COMMON

TOOL_DOCS["lookup_user_profile"] = (
    "THREE MODES — exactly one per call.\n"
    "Mode A, named user: `username=\"bankerkyle\"` returns trader-rank "
    "(#N/M), racism-rank, and both rationales. For questions naming a "
    "specific user. Add `include_profile=true` (A and B only) when the "
    "question needs personality/voice/personal context.\n"
    "Mode B, single position N: `metric=\"trader\"|\"racism\", "
    "rank_position=N` returns the ONE user at #N, any N, no cap. "
    "`from_bottom=true` counts from the worst end: 'who's the worst "
    "trader' is `rank_position=1, from_bottom=true` (rank still "
    "expressed top-down).\n"
    "Mode C, leaderboard: `metric=...` returns top 5 with rationales by "
    "default; when the asker names a size ('top 10') pass `top_n` (max "
    "10). For 'top 5 racists', 'top 10 leaderboard', 'who's the most "
    "annoying'.\n"
    "HARD RULES: leaderboards serve the asked-for size, default top 5, "
    "max 10 — a 'top 20' ask gets the 10 and stops there, no "
    "cap-explaining and no 'ask differently'. 'Worst X' is a real "
    "question: Mode B `from_bottom=true`, answer with the NAME and "
    "rationale, never a meta-explanation. No N cap on single positions — "
    "the tool errors past the roster, so say 'no one at #25' naturally, "
    "no tool talk. CONV-SCOPED rank is NOT the GLOBAL leaderboard "
    "(binding): the 'racism-rank ... in this conv' line in WHO'S TALKING "
    "ranks ONLY people active in this conversation and must never be "
    "presented as global standing — global questions ('am I #1?', 'where "
    "do I rank?') MUST go through this tool, and never assert a #N that "
    "contradicts a leaderboard you already gave. A roast is not a "
    "license to invent a rank. No raw 0-100 scores, ever — the tool "
    "deliberately doesn't return them."
) + _COMMON

TOOL_DOCS["lookup_trade_log"] = (
    "TWO ANCHORS — exactly one per call.\n"
    "Caller anchor: `caller=\"abe\"|\"bankerkyle\"`, "
    "`kind=\"open\"|\"recent\"|\"tally\"|\"all\"`, `days?` — the "
    "structured log for a registered caller, high fidelity "
    "(`data_quality:\"caller\"`).\n"
    "Username anchor: `username=...` returns `profile_recent_trades` "
    "(screenshot ledger, OCR-verified) AND `chat_stated_trades` (their "
    "chat messages that read as trade calls, self-reported). Member "
    "fidelity (`data_quality:\"member\"`).\n"
    "'How did I do' / 'my trades' / 'did I trade X' — USE BOTH SOURCES, "
    "binding. Most of the room calls trades by TALKING, not "
    "screenshotting. Ledger `gain_pct` is a verified result; "
    "`chat_stated_trades` is their own words — quote their stated % as "
    "their claim, flagged self-reported. If `chat_stated_trades` is "
    "non-empty you may NEVER say 'you did nothing', 'batting .000', "
    "'nothing logged', or 'zero mentions'. Only when BOTH are empty: "
    "'nothing in the ledger and nothing trade-shaped in your chat "
    "today' — then nudge them to screenshot for verified tracking.\n"
    "Outcome and P&L claims from these rows are governed by ZERO "
    "UNFORCED TRADE-OUTCOME ASSERTIONS in the system prompt — read "
    "each row's status tag and assert nothing the tag does not "
    "support."
) + _COMMON

TOOL_DOCS["lookup_market_price"] = (
    "WHEN TO CALL: live price and change % for stocks (Finnhub) and "
    "crypto (Binance.US — any major coin by symbol: BTC, ETH, SOL, SUI, "
    "PEPE, not just the top few), up to 10 symbols per call. A symbol "
    "that resolves to neither comes back with a 'no live feed for X' "
    "error — relay that honestly, don't guess a price. Covers major US "
    "indices the same way it covers single names: 'Where's $NDX?' / "
    "'SPX level?' / 'how's the Dow?' take `symbols=[\"NDX\"]` / "
    "`[\"SPX\"]` / `[\"DJI\"]` (RUT too) — use the index ticker, not the "
    "ETF, when the asker named the index. The response carries a "
    "`session` label; phrase the move accordingly: OPEN is "
    "'session-to-date', AFTER-HOURS is 'today's full session', "
    "PRE-MARKET is 'yesterday's close', WEEKEND-CLOSED is 'Friday's "
    "close'. Crypto trades 24/7, so its move is always today's.\n"
    "SCOPE: this tool is PRICE ONLY. Open interest, options volume, "
    "implied volatility, put-call ratios, Greeks and any chain data are "
    "`lookup_options_chain`, not here. Price HISTORY is "
    "`lookup_price_history` — this tool is current-only."
) + _NO_SELF_TA + _COMMON

TOOL_DOCS["lookup_options_chain"] = (
    "STATUSES: `status:\"ok\"` use `summary`. `status:\"no_chain\"` "
    "means no listed options — tell them. `status:\"error\"` means the "
    "chain is unavailable right now — point them at their broker. Never "
    "fabricate alternative numbers.\n"
    "USAGE: the first call without `expiration` returns the nearest "
    "expiration plus the full `available_expirations` list — for "
    "'what's the OI on SPY next week', call bare, find next week in that "
    "list, then re-call with the right ISO date. Pass `strike` + "
    "`contract_type` for ONE contract's current OI/volume/IV. Implied "
    "volatility comes back as a decimal — phrase it as a % for readers. "
    "Works for indices too. Aggregated stats cover ONE expiration: "
    "total call+put volume, total call+put OI, ATM implied "
    "volatility, put-call ratios (volume and OI), the available "
    "expirations, and spot. It is a SNAPSHOT, current only, with no "
    "multi-day history — a '5-day OI trend' is not available, so "
    "say so (broker pull) rather than fabricating it."
) + _NO_SELF_TA + _COMMON

TOOL_DOCS["lookup_economic_calendar"] = (
    "WHEN TO CALL: canonical scheduled time, consensus, prior and actual "
    "for US Tier-1 macro (CPI, PCE, NFP/payrolls, unemployment, GDP, "
    "retail sales, ISM, PPI, FOMC, Powell speeches) and ECB/BOJ/BOE rate "
    "decisions — sourced from the SAME Finnhub feed the daily pulse "
    "uses, so /ask numbers never contradict the pulse. Call for print "
    "dates, consensus, actuals, and Powell's schedule ('when is the next "
    "CPI', 'May CPI release date', 'what was the May payrolls actual'). "
    "Call with no query for 'key prints this week' (a ±14d window); pass "
    "a wider `days_window` for historicals. Do NOT call for "
    "forecaster-specific reads ('what does Goldman expect') or causation "
    "('why is CPI elevated') — those are Google. Regional Fed surveys, "
    "minor data and unlinked foreign macro are whitelist-filtered, so "
    "the tool returns empty for them.\n"
    "PER-EVENT `status`: `\"released\"` state the actual with prior and "
    "consensus. `\"scheduled\"` give date/time plus consensus if "
    "present — a null consensus is 'no consensus posted yet', and you "
    "must NOT pull a forecast from Google to fill it. "
    "`\"past_no_data\"` means the print is out but Finnhub hasn't "
    "ingested it (common 30-60 min post-release) — say so and offer to "
    "Google the wire."
) + _COMMON

TOOL_DOCS["lookup_earnings_date"] = (
    "WHEN TO CALL: next earnings date and last reported quarter for ONE "
    "ticker — ANY US-listed symbol, no whitelist. Call for 'when does "
    "GEO report', 'did PLTR beat last quarter', and next-quarter "
    "estimates. NOT for earnings CONTENT (guidance commentary, why it "
    "moved — Google), not for macro prints "
    "(`lookup_economic_calendar`), and not for broad 'what reports this "
    "week' sweeps (Google — this tool is one symbol per call). The "
    "`timing` field is before open / after close / during hours / TBD.\n"
    "FALLBACK IS REQUIRED, NOT OPTIONAL: on `no_data`, `error`, or a "
    "null `next` (common more than 6 weeks out) go straight to Google "
    "Search and answer the actual date question, flagging "
    "company-confirmed vs estimated. NEVER substitute last quarter's "
    "results for a next-date question. If neither source has it: 'no "
    "confirmed date yet — typically reports early [month] based on past "
    "quarters.'"
) + _COMMON

TOOL_DOCS["query_data"] = (
    "WHEN TO CALL: read-only SQL SELECT over the bot's SQLite DB, for "
    "aggregates, trends over time, activity-by-hour, and group-bys the "
    "other tools can't do (they return capped individual rows, not "
    "counts). The go-to for 'analyze X over time', 'who trends up', "
    "'distribution of Y'. Pair with code execution: query the aggregate, "
    "then chart it. SELECT/WITH only; writes, DDL and PRAGMA are "
    "blocked; 500-row cap. Key tables: `chat_messages` (the corpus — no "
    "per-message racism score, so approximate a slur trend with "
    "`content LIKE '%nigg%'` and SAY it's approximate), `analyst_trades` "
    "(WINS-BIASED — losses leak as `inferred_status='expired_unknown'` "
    "ghosts, so a naive win-rate is wrong). QUERY `analyst_trades_real`, "
    "NOT `analyst_trades`: the raw table is 98% non-trades — the "
    "classifier writes one row per message it inspects and 75,960 of "
    "77,362 rows are is_trade=0 with ticker, action and gain_pct all "
    "NULL. The view is the same columns filtered to is_trade=1. Also "
    "`user_profiles` / "
    "`user_metrics` (scores + ranks), `daily_reports`. Bucket time with "
    "`strftime('%Y-%W' / '%Y-%m-%d' / '%H', posted_at)`.\n"
    "NOT a substitute for a dedicated tool. If a purpose-built tool "
    "covers the question, call that instead — this one returns rows, not "
    "the tool's resolved and labelled view."
) + _COMMON

TOOL_DOCS["lookup_price_history"] = (
    "WHEN TO CALL: historical daily/weekly CLOSES for one symbol — the "
    "only market price HISTORY you have, since `lookup_market_price` is "
    "current-only. Use it for any time series: performance since a date, "
    "drawdowns, charts over time, and ANY correlation against another "
    "series. Index tickers take the Yahoo caret form: `^GSPC` for the "
    "S&P 500, `^NDX`, `^DJI`, `^RUT`, `^VIX`; plain tickers otherwise. "
    "`status:\"no_data\"` means say so — never invent price levels to "
    "fill a chart axis."
) + _NO_SELF_TA + _COMMON

TOOL_DOCS["lookup_fantasy_league"] = _COMMON
