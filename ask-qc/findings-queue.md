# Ask-QC findings queue

Entries are appended by the nightly headless judge, one heading per graded
date. Entries are removed by the work session that ships or rejects them —
never by the judge.

## 2026-09-24

- (open) 1. judgment (OpenSea/Coinbase case conflation, 20:07:58 UTC turn) —
  ungrounded `LOCAL/FACT` turn answering "is it illegal to insider trade
  crypto" cites "The OpenSea insider trading case (US v. Wahi)" as the
  legal blueprint. Web-verified: Wahi is real but is the **Coinbase**
  token-listing front-running defendant (guilty plea, wire fraud
  conspiracy); the actual OpenSea prosecution is a different case,
  *United States v. Chastain* (and that conviction was vacated by the
  Second Circuit in July 2025). The bot fused two distinct real cases
  into one wrong citation. Same fact-shape as the 2026-08-28 wrong-Fed-
  Chair and 2026-08-27 Jackson Hole findings already queued: confident
  conflated specificity on a zero-grounding turn. `scripts/validate_answer.py
  --file /tmp/ans6.txt --tools "lookup_options_chain" --question "is it
  illegal to insider trade crypto"` returns no violation (only the
  unrelated `repetition-glitch: detector unavailable` noise) — no
  existing class inspects named-case/entity attribution, the same gap
  already on record from 08-28. Full writeup:
  `pulse-data/ask-qc/2026-09-24.claude.md`.
- (not a FAIL, flagged anyway) infra-or-model: repetition-strip corrupts
  embedded code blocks (22:55:09 UTC turn) — a `WEB/FACT` grounded turn
  ("show me pelosi's portfolio breakdown by %") shipped a syntactically
  broken Python snippet: `weights =` with the array literal's contents
  missing, confirmed with a local `compile()` check
  (`SyntaxError: invalid syntax` at that line). The raw model output (in
  the turn's own `<details>` block) has the complete, correct array;
  the guard sequence on this turn was `repetition, repetition-strip,
  validate:macro-unsourced, validate-regenerated`. Likely cause: the
  same 8 percentage figures appear twice in this answer (once in the
  code's array, once in the prose bullets below it), which is exactly
  the shape the repetition-glitch detector treats as a token loop — the
  strip pass appears to have excised the array literal's contents as
  "repeated" without knowing it sat inside a code fence. Graded INFRA
  per ground rule 3 (the model's own output was correct; the harness's
  post-processing broke it), so not triaged as a FAIL, but logged here
  because it's a concrete, reproducible bug with a clear fix direction:
  the repetition-strip sentence-excision pass should not operate inside
  fenced code blocks. `scripts/validate_answer.py` (grounded, no tools)
  returns no violation for this content — no class inspects code-block
  syntax. Full writeup: `pulse-data/ask-qc/2026-09-24.claude.md`.

## 2026-09-13

- (open) judgment — fantasy-league projected-total fabrication at
  21:21:32 UTC (Ry_spaceman, "How's my team doing"). `lookup_fantasy_league
  topic=situation, member=Ry` returned `ok` (3698 chars); the answer's
  current scores (91.0 vs 122.86) are exactly right but its projected
  totals (134.7 to 124.9) are wrong and outcome-flipping versus a
  screenshot BK posted 86 seconds later in the same thread (real
  projected: 140.48 to 149.78, 37% win prob for Ry's team — bot's
  numbers implied Ry pacing ahead, screenshot shows him pacing behind).
  BK's own chat message explicitly separates the two clauses: "Current
  scores are right" / "Projected totals are wrong." `scripts/validate_answer.py`
  returns no violation and no rule in `scripts/ask_response_validate.py`
  targets tool-sourced-but-wrong numeric payload values — no regex can
  separate a wrong projected-total pair from a correct one without an
  independent ground-truth fetch, so this isn't drafted as a fixture.
  Same shape as the 2026-09-11 CPI-print finding (tool `ok`, figures
  still wrong). Also flagged (unconfirmed, not a second finding): the
  same tool fired again 86 seconds later for the other side of this
  same matchup (interaction 21:22:58, BK, projected 134.5 to 136.6)
  with no independent check available to confirm or rule out the same
  defect there — worth a work session checking whether
  `lookup_fantasy_league`'s `projected` field has a general
  freshness/accuracy problem. Full writeup: `pulse-data/ask-qc/2026-09-13.claude.md`.

## 2026-08-26

*(drained 2026-08-27 by the tool-status session, commits on the main
branch)*

- ~~1. validator-miss (IV figure, 13:41 turn)~~ — **predates-class**,
  not a miss: `unforced-market-data` deployed 19:15 UTC that day, five
  hours after the turn shipped. No firings and no shipped violations
  since deploy. The judge prompt now checks class age before bucketing
  a validator-miss.
- ~~2. regex-able, blocked on plumbing (GPS earnings confidence)~~ —
  **shipped** as validator class 10 `failed-tool-confidence`:
  per-tool status + grounding now reach the validators in production,
  the harness, and `scripts/validate_answer.py --tool-status`; the
  grounded-twin and hedged carve-outs are pinned by unit tests and
  fixture 47.

## 2026-08-27

- ~~1. regex-able (X-posting cost)~~ — **shipped 2026-08-28** as validator class 11 `unforced-unit-cost` (commit 9899d5fc on the main branch): grounding-gated, hedge + wage carve-outs, fixture 48, 0 sweep FPs.
- (open) 2. judgment (Jackson Hole keynote specifics, 21:58 UTC turn) — same
  zero-tool-call, zero-grounding `LOCAL/BANTER` shape as finding 1, but
  invents a named Fed official + exact clock time + streaming venue for
  tomorrow's keynote with nothing in the visible prompt or any tool/search
  output to support the specifics. `validate_answer.py --tools ""` is
  clean by design here: `check_macro_unsourced`'s figure regex
  deliberately excludes plain times/dates ("CPI lands Wednesday at 8:30"
  must NOT be flagged — see `scripts/ask_response_validate.py:260-262`),
  and that exclusion is correct in general — a real calendar statement
  isn't a violation. Telling this turn's fabricated specificity apart from
  a legitimate schedule statement needs to know whether the fact is real,
  which isn't mechanically checkable from the answer text alone. Queued as
  prompt-session material: should LOCAL/BANTER-routed Type 1 answers about
  live scheduling events (keynote/earnings call/Fed speech) be required to
  cite a source or explicitly hedge person/time/venue specifics they can't
  verify.
- (13:10 UTC Jackson Hole turn graded CONCERN, not FAIL — lower-specificity
  version of finding 2, no named person/time/venue. Not triaged per Step 3
  since it has no FAIL dimension; noted in the full report for context.)

## 2026-08-28

- (open) 1. judgment (wrong Fed Chair named, 20:51:41 UTC turn) — grounded
  turn answering "whens the next fed meeting" states "Jerome Powell press
  conference" for the Sept 16 FOMC. Wrong: the bot's own next turn 35
  seconds later (20:52:16, also grounded, 2 different sources) names
  "Fed Chair Kevin Warsh," and the room's own visible chat in the first
  turn's prompt block already treats Warsh as the sitting chair ("WARSH
  TODAY AT 10AM EST CHAT... BE PREPARED", "Bring back jpow ties"). Same
  fact-shape as the 08-27 Jackson Hole findings (invented specifics with
  nothing to check them against) but here the corpus itself proves the
  claim wrong rather than just being unverifiable — a cross-turn
  consistency check, not a single-turn hedge/decline problem.
  `scripts/validate_answer.py` confirmed clean (no existing rule class
  looks at named officials/persons — all 12 classes are numeric-claim or
  tool-status shaped). Queued as prompt-session material: should a turn
  naming a specific office-holder be required to check against the most
  recent grounded turn that named the same office, before publish?

## 2026-08-31

no findings

## 2026-09-01

- (open) 1. judgment (unsourced options-flow claim, 17:28:03 UTC turn) —
  grounded turn answering "who reports earnings today" (2 sources, both
  earnings-calendar/estimate sites) correctly lists the earnings slate,
  then adds a third bullet in the same sourced-looking arrow format:
  "Flow focus: heavy options positioning concentrating on DELL and GTLB
  into the afternoon print." No options-chain tool fired, neither cited
  source plausibly carries flow data, and nothing in the visible chat
  window mentions DELL/GTLB options flow. Same fact-shape as the
  2026-08-27 Jackson Hole and 2026-08-28 wrong-Fed-Chair findings —
  confident invented specificity with nothing in the visible payload to
  check it against. `scripts/validate_answer.py --tools "" --question
  "who reports earnings today" --grounded` returns clean (only an
  unrelated `repetition-glitch` "detector unavailable" notice from a
  missing optional dependency, not a finding). Checked the closest
  existing rule, class 4 `check_unforced_market_data` (fires on
  "dealer positioning" et al. *with* a numeric figure attached) — this
  claim has no figure attached to "positioning," so the class correctly
  does not fire and can't be tightened to this shape without also
  flagging legitimate qualitative color commentary. Queued as
  prompt-session material: should a Type 1 arrow bullet that reads as
  sourced-fact format but carries a qualitative claim (flow/positioning/
  sentiment) with zero grounding be required to hedge or be dropped,
  distinct from the calendar/rank-invention shape already queued from
  08-27/08-28?

## 2026-09-02 (owner review of the 08-31 to 09-02 logs, added by the session)

- (open) 1. infra-or-model (HPE odds, 18:29 UTC turn) — the user received
  "Thought myself in circles and ran out of room. Try asking it more
  directly." The tool trace shows lookup_earnings_date ok, then FOUR
  search_chat_messages rounds (two empty) before the round cap ended the
  turn. A factual "odds X beats" question should never spend its budget
  on chat search. Two things to decide in a session: why the router kept
  reaching for chat search after the earnings tool answered, and whether
  the round-cap fallback text is acceptable to ship to a user at all
  (it reads as the bot's own failure, which it is).
- (open) 2. judgment cluster: uncited specifics in zero-tool, zero-grounding
  LOCAL/BANTER turns on 09-02 — GOLD/gold.com earnings (14:11: EPS,
  revenue, price, target, all from memory), September 11 market history
  (18:35: a "14% first-week DJIA drop" and a "55% of Septembers negative"
  statistic), MRVL-off-AVGO mechanism (20:20), software-and-semis
  history (20:25: "late 2023" and "Q1 2024" with SOXX/IGV). None cited a
  source; the judge's rubric now treats this shape as the trigger (rule
  5). Session question: should a Type 1 arrow answer with figures and no
  tool/grounding be forced through a grounded retry the way calendar
  questions are (`_is_calendar_question` net), i.e. a "numbers without a
  source" net in code rather than a prompt rule.

## 2026-09-03

- (open) 1. validator-miss (unforced BTC price, 02:49:24 UTC turn) — the
  turn asserted spot "$77,338" with a fresh 5-year probability estimate
  built on top of it, zero tool calls that turn, silently different from
  the tool-grounded $77,769 the same thread had used two turns earlier.
  `check_unforced_price` (validator class 3, live since 2026-08-26 per
  `discord_bot/ask_prompt.py`'s docstring ledger — this checkout's git
  history is a single-commit snapshot so a deploy-timestamp diff isn't
  meaningful) correctly flags `$77,338` when run standalone via
  `scripts/validate_answer.py`, and is confirmed firing live elsewhere
  same day (12:16:18, 12:20:56 both show `guards: validate:unforced-price`).
  So this is a genuine miss: the turn went through a separate, later
  grounding-backstop retry (`guards: grounding:market-shape`) that
  regenerates the answer via search grounding, and that regenerated text
  never gets re-run through the phase-04 validate ladder. Fix is
  architectural — re-run validation on backstop output, or fold the price
  check into the backstop's own acceptance gate. Same shape as the open
  TODO for a general figure-provenance check.
- (open) 2. judgment (dropped-outlier price range, 12:16:18 UTC turn) —
  grounded turn (`lookup_price_history` ×3, ok) answered "Nasdaq % range
  since Aug 5" with "29,320 and 29,700" as the full band; the bot's own
  next turn in the same thread (12:20:56, fresh grounded lookup) concedes
  NDX "Hit 30,195 on August 17" — a print outside the range the first
  answer claimed was the whole window, effectively a self-contradiction.
  `scripts/validate_answer.py --tools "lookup_price_history" --grounded`
  returns clean, as expected — the tool fired, so no unforced-claim rule
  applies; this is a data-fidelity error inside a grounded answer
  (dropping an outlier from a queried range), not a missing-tool-call
  pattern, and isn't checkable from answer text alone since the log only
  records tool payload size, not content.
- (open) 3. regex-able (LULU implied-move self-contradiction, 20:07:16
  UTC turn) — turn claimed "Historical 1-day realized moves average
  ±9.4% across the prior 12 quarters," contradicting the bot's own
  10.2%-average claim to a different asker 59 minutes earlier in the same
  room (19:08:33), and not traceable to the only tool called
  (`search_chat_messages`, which can't supply a multi-quarter realized-
  move stat). Real reporting shows LULU's history is framed as trailing
  8 quarters / median 11.9%, and 9.4% is a real number but for one past
  quarter's *implied* move, not a 12-quarter realized-move average —
  looks like a genuine figure lifted from one context and repurposed as
  a different invented aggregate. `check_unforced_time_series` in
  `scripts/ask_response_validate.py` is a near-miss: its `_TREND_CLAIM`
  regex requires `\d+%\s+(?:over|across)\s+\d+` with only whitespace
  between the connector and the number, so "across the prior 12 quarters"
  (with "the prior" inserted) doesn't match.
  Candidate fixture:
    question: "what was the implied move for LULU earnings?"
    bad answer: "Historical 1-day realized moves average ±9.4% across
      the prior 12 quarters, closely tracking the options market's
      expected range."
    good-answer control: "Historical 1-day realized moves are
      unavailable without a lookup_price_history call — going off the
      priced ±8.2% move only."
    suggested regex widening: `\s+(?:over|across)\s+\d+\b` ->
      `\s+(?:over|across)\s+(?:the\s+)?(?:prior|last|past|trailing)?\s*\d+\b`
- (open) 4. judgment (prose-only matchup answer, 20:50:54 UTC turn) —
  `LOCAL/FACT` turn with 3 tool calls (`lookup_fantasy_league`, ok)
  answering "how's my matchup looking" shipped as pure prose, zero arrow
  bullets, for cleanly enumerable facts (opponent, two scores, two
  players per side) — matches the rubric's format_adherence FAIL example
  verbatim. Notably inconsistent with the identical question shape at
  21:32:30 in the same log, which rendered as a full arrow-bulleted
  lineup dump. `scripts/validate_answer.py` returns clean; none of the
  twelve check functions in `scripts/ask_response_validate.py` inspect
  output format (arrow-bullet vs. prose) at all — the whole suite is a
  fabrication/unforced-claim detector, not a format linter. Distinguishing
  "should have been bulleted" from "legitimately short prose" is a
  semantic call a naive regex would false-positive on elsewhere in the
  corpus. Queued as prompt-session material: should matchup-narrative
  answers get an explicit prose-allowed exception, or be forced through
  the same arrow-bullet format as roster-dump answers?

## 2026-09-04

- (open) 1. regex-able (unsourced historical-superlative claim, 16:30:21
  UTC turn, Ry_spaceman, "how does the market close on the Friday
  before labor day") — answer stated September "holds ... the highest
  historical volatility of the calendar year"; web search confirms
  September is historically the worst month for *returns* but that
  October, not September, carries the reputation for highest
  volatility — a real fact yoked to a wrong one in the same sentence.
  Route was `WEB/FACT · ungrounded`, no Tools table, so ground rule 5
  applied; verification found the contradiction.
  `scripts/validate_answer.py` returns clean (had to `pip install -r
  requirements.txt` first — `aiohttp`/`discord.py` were missing in this
  environment and every check function was silently reporting
  `detector unavailable`). The clean result is expected: none of the
  twelve checks in `scripts/ask_response_validate.py` target this
  shape, and `discord_bot/figure_provenance.py` — which otherwise
  strips/hedges unsourced *numeric* figures on exactly this kind of
  ungrounded WEB/FACT turn — is scoped to digit-bearing tokens only
  (`_NUM_RE`); "highest historical volatility of the calendar year"
  carries no digit, so even a correctly-firing figure-provenance guard
  would not have caught it.
  Candidate fixture:
    question: "how does the market close on the Friday before labor day"
    bad answer: "This close officially ends summer volume and rolls the
      market straight into September, which holds the worst average
      monthly return profile and the highest historical volatility of
      the calendar year."
    good-answer control: "September has a reputation as a seasonally
      weak month for stocks, though by most measures October — not
      September — carries the crown for volatility; take any
      single-month seasonality claim with a grain of salt."
    suggested new check (`check_historical_superlative_unsourced`):
      fire when `not grounded and not tool_calls` and the answer
      matches
      `r"\b(?:highest|worst|best|lowest|largest|biggest)\b[^.\n]{0,40}?\b(?:of (?:the )?(?:calendar )?year|on record|in history|historical(?:ly)?)\b"`
      with no hedge word (`roughly|by some measures|depending on|arguably|debat`)
      within ~40 chars of the match.

  Side note, not a per-turn finding but worth a maintainer look: all 5
  `WEB/FACT`/ungrounded/no-Tools-table turns in today's log
  (16:30:21, 18:51:36, 20:00:57, 21:01:53, 21:29:25) fall inside a
  documented incident window in `discord_bot/bot.py` — a phase-9
  return-value bug dropped `grounding_metadata` to `None` on every
  `/ask` turn "from 2026-09-02 to 09-04" (bot.py:6905-6916), which both
  vanished every Sources footer and caused the figure-provenance
  guard's skip-check to misfire (bot.py:1771-1779, dated in-code to
  2026-09-04). Both fixes live in this checkout's single commit,
  timestamped 2026-09-04T20:10:17-07:00 (2026-09-05T03:10:17Z) — after
  every turn in today's log (last turn 21:30:28 UTC, ~5h40m before the
  fix). Net effect: the figure-provenance guard was inoperative for
  every *numeric* figure across those 5 turns for the whole window;
  most numbers checked out on manual verification this session, but the
  strip/hedge process that should have applied regardless of
  correctness didn't fire. Appears already fixed going forward per the
  commit timestamp — worth a maintainer confirming the fix is live in
  the next real deploy, no further action from this queue.

## 2026-09-07

no findings

  Side note, not a per-turn finding: 4 of the day's 6 turns (20:48:06,
  20:48:58, 20:49:55, 20:50:18 UTC) shipped the canned "Thought myself
  in circles and ran out of room" wrapper — an empty/blocked
  generation, not a model-authored bad answer, graded INFRA per ground
  rule 3. This is the exact incident `tests/test_ask_empty_retry.py`'s
  docstring documents ("Four of six asks in one afternoon shipped
  [this]"), and the fix (a short-thinking/search-only retry, plus a
  token-count discriminator that routes genuine PROHIBITED_CONTENT
  blocks to the existing filter ladder instead of the budget-retry
  path) lives in `discord_bot/bot.py` in this same checkout. The fix's
  commit is timestamped 2026-09-07T21:20:37-07:00
  (2026-09-08T04:20:37Z) — after every turn in today's log (last turn
  20:50:18 UTC, ~7h30m before the fix). Already fixed going forward;
  no action needed from this queue beyond a maintainer confirming the
  fix is the one live in production.

## 2026-09-08

- (open) 1. regex-able (garbled markdown nesting on every cashtag,
  17:07:24 UTC turn, Ryan, "what are the major earning releases this
  week") — answer rendered every ticker parenthetical as broken
  nested markup, e.g. `GameStop (**\`**GME**\`**) reporting **Tuesday
  AMC**` and `Signet Jewelers (**\`\`**SIG**\`**)**` — literal
  asterisks and backticks around all four tickers in the answer
  (GME, SIG, AEO, ORCL, ADBE, M, KR). Present identically in the raw
  model output block, so this is model-authored, not a downstream
  mangling. Route was `WEB/FACT · grounded ✅ (2 sources)`; fabrication
  is clean (sourced, no contradiction found), the defect is pure
  format_adherence. `scripts/validate_answer.py` returns clean (had
  to `pip install -r requirements.txt` first — `aiohttp`/`discord.py`/
  `pytz` were all missing in this environment and every check
  function was silently reporting `detector unavailable`, same gap
  the 2026-09-04 queue entry already flagged). None of the twelve
  checks in `scripts/ask_response_validate.py` target markdown-nesting
  corruption.
  Candidate fixture:
    question: "what are the major earning releases this week"
    bad answer: "→ GameStop (**`**GME**`**) reporting **Tuesday AMC**"
    good-answer control: "→ GameStop (**$GME**) reporting **Tuesday AMC**"
    suggested new check (`check_markdown_nesting_corrupt`):
      fire when the answer matches
      `r"\*{2}\x60{1,2}\*{0,2}|\x60{1,2}\*{2}\x60{0,2}"`
      (two-or-more asterisks directly touching one-or-more backticks,
      in either order) — a clean bold cashtag (`**$GME**`) or code
      span (`` `GME` ``) never puts `**` and `` ` `` adjacent, so this
      pattern only fires on the corrupted nesting shape.

  Side note, not a per-turn finding: the "Abe's holdings" thread
  (00:17:37, 00:17:58, 00:18:03 UTC — dovahjo and spockbones asking
  about the same `lookup_trade_log` result three times in 26 seconds)
  hit the figure-provenance evidence-extraction gap that
  `discord_bot/figure_provenance.py` already documents in its own
  source comment, narrating this exact incident down to the asker's
  "You didn't show anything" reply. The named fix (`_OPT_STRIKE_RE`,
  recognizing glued option-contract notation like `350P` on the
  evidence side) is timestamped `2026-09-09T08:02:38-07:00` in this
  checkout — after every turn in today's log (first occurrence
  00:17:37 UTC on 09-08, ~15h before the fix). This checkout has only
  one commit, so git-blame can't date it more finely than "the
  current deploy," but the timestamp alone post-dates the whole log.
  Already fixed going forward per the commit-timestamp precedent set
  for the 2026-09-04 and 2026-09-07 entries above; no action needed
  from this queue. Full writeup with the two graded turns (one INFRA,
  one CONCERN) in `pulse-data/ask-qc/2026-09-08.claude.md`.

  A second side note, also not a queued finding: a 14:03:53 Sam turn
  ("remind me if I have any open calls in members alert from past 7
  days") carries two chat-attributed claims ("HOOD calls last week,"
  "scaling Palantir $180") that don't appear anywhere in the turn's
  visible 9,478-char prompt block, and running the live
  `discord_bot.figure_provenance.check()` against that exact evidence
  confirms it would strip the line. Graded CONCERN, not FAIL, and
  deliberately left off this queue: the prompt block shown in the log
  is the pre-tool-call prompt, not `lookup_trade_log`'s actual
  2,561-char JSON response, so I cannot rule out the two claims are
  legitimately sourced in a payload I can't see. See
  `pulse-data/ask-qc/2026-09-08.claude.md` for the full reasoning.

## 2026-09-09

- (open) 1. regex-able (roast question answered as a message-count
  stat, 23:20:51 and 23:21:33 UTC turns, vincenzo9231, "roast monsoon
  and don't hold back" / repeated after "dummy, I said roast
  @Monsoon") — both turns are `short_circuit_message_count` entries (a
  deterministic code path in `discord_bot/bot.py` that bypasses
  Gemini, and therefore the whole DRAFT/AUDIT/validator ladder,
  entirely) and both delivered "BK sent 84/86 messages in
  #💬-stonks-yapping-💬 in the last 6 hours" — a real, tool-grounded
  number, completely unrelated to the question asked. Root cause found
  by reading the code: `_is_message_count_question()`
  (`discord_bot/bot.py:3009`) only strips a reply-chain prefix
  matching `"'s message to you]"` or `"'s message]"`
  (`bot.py:3016-3019`) before scoring the text against
  `_MSG_COUNT_INTENT_RE`. Both turns instead use the `[VERBATIM RECENT
  MESSAGES — ... — for accurate quoting ...]` block format (a
  different feature, for verbatim-quoting a target user), which is
  never stripped — so the regex scans the whole unstripped blob and
  trips on a quoted historical line that is itself BK's real question
  from 22:18:13 ("how many total messages have been sent in this
  channel"). `_extract_message_count_target()` then matches `@BK
  (bankerkyle)` a few lines above that quoted line, and the
  short-circuit answers a question nobody asked. `scripts/validate_answer.py`
  returns `{"violations": []}` against the delivered text — expected,
  since the text is internally coherent and its number is genuinely
  grounded; the defect is entirely in which question got answered, not
  in the answer's own claims. This checkout has a single commit (`git
  log --oneline` → 1 line), so a deploy-timestamp check isn't possible
  here; the buggy function is live in the current deployed code and
  reproduces the failure exactly, so this reads as a current gap, not
  something already fixed after these turns.
  Candidate fixture (for a harness exercising
  `_is_message_count_question` directly — `scripts/ask_fixture_run.py`'s
  `tool_stubs` approach targets the Gemini path, not this short-circuit):
    question: "[VERBATIM RECENT MESSAGES — Monsoon (reportufirst) — for accurate\n"
      "quoting when the question references them; quote LINE FOR LINE]\n"
      "  2026-09-09T22:18 #x — @omniwiz how many total messages have been sent in this channel\n"
      "  2026-09-09T21:53 #x — @BK (bankerkyle) you got a new disciple\n\n"
      "roast monsoon and don't hold back"
    assertion: `_is_message_count_question(question) is False`
    fix direction: strip the `[VERBATIM RECENT MESSAGES` ... `]` block
    the same way the two existing reply-chain markers are stripped —
    or, more robustly, only score the text after the last bracketed
    context block, never the whole payload.
  Related, same root shape, not a separate finding: interaction #39
  same day (23:35:02, Monsoon, "roast me," also carrying a `[VERBATIM
  RECENT MESSAGES]` block) fired `lookup_market_price` with
  `symbols=['LINE', 'FOR', 'AAPL', 'IMAGE', 'TEXT', 'QQQ']` — the
  ticker extractor pulling structural words out of the same kind of
  unstripped block. Result came back `prefetch:None` and went unused,
  so no user-facing harm that time, but it's the same extractor-scans-
  the-whole-payload gap surfacing in a second place. Graded CLEAN,
  noted here for whoever picks up the fix.
- (open) 2. judgment (prose-only FACT answer, 12:20:38 UTC turn,
  2Pale, "has abe won a goog trade ever") — `LOCAL/FACT` turn,
  `lookup_trade_log caller=abe kind=all` status `ok`, answered as pure
  prose enumerating four distinct facts (an open position + three
  closed wins with percentages) with zero arrow bullets — matches the
  rubric's format_adherence FAIL example verbatim, and the same shape
  as the 2026-09-03 queue's finding 4. `scripts/validate_answer.py`
  returns clean; none of the twelve checks in
  `scripts/ask_response_validate.py` inspect arrow-bullet vs. prose
  shape at all. Queued as prompt-session material, not a fixture:
  several CLEAN banter (Type 2/3) answers in the same day's log are
  also single-sentence prose with zero arrows, correctly so, so a
  naive "FACT answers must contain →" regex would need to also key off
  route/type to avoid false-positiving on those.

  Side note, not a per-turn finding: a scope-drift CONCERN (not a
  FAIL, not queued as an open item) at 23:09:29 UTC — BK asked "based
  on the ~47,000 or so messages you said I've sent **in here**, how
  many messages do I average per day," referencing the bot's own
  47,058 **channel-scoped** answer from 64 minutes earlier (22:05:53).
  The new answer delivered 48,705 as "total" without flagging that it
  had switched scope to all-channels (or that it differs from the
  referenced figure at all). Not clearly wrong — BK plausibly posts in
  a few other channels too, closing a ~3.5% gap — but delivering an
  unflagged scope change on top of a user-referenced prior number is a
  CONCERN, not a clean PASS. Worth a second day's data point before
  queuing as a pattern; full reasoning in
  `pulse-data/ask-qc/2026-09-09.claude.md`.

## 2026-09-10

No findings — no FAIL-dimension interactions in the day's log (29
graded, 28 CLEAN, 1 CONCERN). The one CONCERN (16:16:48 UTC, ZebraCake,
NFL payout question — possible game-identity mismatch against room
context) has no FAIL dimension so it isn't triaged per Step 3; full
reasoning in `pulse-data/ask-qc/2026-09-10.claude.md`. Two turns
(18:44:49, 19:12:55) show the `validate:macro-unsourced` /
`validate-strip` guard catching an unsourced figure in the raw model
output and the shipped answer using the sourced replacement instead —
noted as the ladder working, not a defect.

## 2026-09-11

- (open) judgment — CPI-print fabrication at 12:31:27 UTC (2Pale,
  "what was the economic print"). `lookup_economic_calendar` returned
  `ok` (2558 chars) but the four shipped figures (core CPI m/m
  +0.22%, core CPI y/y +2.67%, headline m/m +0.07%, headline y/y
  +3.52%) are all wrong versus the real August 2026 BLS release
  (core m/m +0.3%, core y/y +2.4%, headline m/m +0.4%, headline y/y
  +3.4% — confirmed by web search and independently corroborated by
  the same day's 14:07:11 turn, which answered the identical question
  correctly via web grounding). Turn landed at 8:31:27 AM ET, ~90
  seconds after the standard 8:30 AM ET release — likely a
  data-freshness gap in whatever backs `lookup_economic_calendar`
  rather than a prompt or model defect; worth checking if it shares a
  release-lag profile with `report/fred_data.py` (per CLAUDE.md, lags
  releases by hours — the reason `report/print_watch.py` exists as a
  faster path for the pulse side). `scripts/validate_answer.py`
  returns no violation and `check_macro_unsourced` in
  `scripts/ask_response_validate.py` only fires when the calendar
  tool was *not* called this turn — no existing or plausible regex
  class can catch "tool called, status ok, figures still wrong"
  without a ground-truth fetch, so this isn't drafted as a fixture.
  Full writeup: `pulse-data/ask-qc/2026-09-11.claude.md`.

## 2026-09-12

No findings — no FAIL-dimension interactions in the day's log (5
graded, 4 CLEAN, 1 CONCERN). The one CONCERN (21:50:25 UTC, EFDHD,
"Did Ry make a HUGE mistake by selling his ETH when he did?") has no
FAIL dimension so it isn't triaged per Step 3: a tools-table-backed
answer with plausible-but-unverifiable premise acceptance, plus a
malformed nested-bold formatting glitch (`**ETH trading flat around
**$2,524****`) that slipped past the voice-lint retry pass even though
the same pass caught and rewrote two em-dashes in the same turn. Full
reasoning in `pulse-data/ask-qc/2026-09-12.claude.md`.

## 2026-09-16

No findings — no FAIL-dimension interactions in the day's log (11
graded, 11 CLEAN, 0 CONCERN). Heavy FOMC-day traffic (5 of 11 turns
touch the 2:00 PM ET rate decision); spot-checked the hike outcome,
TOST price action, Voodoo Ranger Juice Force nutrition figures, and
the BoE/BoJ calendar dates against live web search — all corroborated.
One turn (18:01:13, bulch) matched ground rule 5's bare-ungrounded/
no-TOOLS-table shape exactly and was verified rather than defaulted to
a pass; its figures trace to the asker's own reply-target message and
independently match real FOMC coverage. Full reasoning in
`pulse-data/ask-qc/2026-09-16.claude.md`.

## 2026-09-17

No findings — only one interaction logged for the day and it graded
CLEAN across all dimensions (1 graded, 1 CLEAN, 0 CONCERN). The MP
Materials earnings-date claim ("estimated for November 5, 2026") sits
against a genuine cross-aggregator discrepancy (TipRanks lists Oct 29
as "confirmed") but matches the bot's own cited source and correctly
used hedged language rather than asserting a confirmed date — not
fabrication. Full reasoning in `pulse-data/ask-qc/2026-09-17.claude.md`.

## 2026-09-18

- **(open) regex-able** — 16:03:13 UTC (bulch, "@abe looking at LITE
  and AAOI for next week"). `LOCAL/FACT` turn delivers live price/%%/IV
  data for two tickers as two prose paragraphs with zero `→` bullet
  lines, closing on a tacked-on summary sentence ("Classic high-beta
  momentum play heading straight into fiber and CPO catalyst season.").
  The figures themselves check out (verified against real Sept 2026
  LITE/AAOI prints via web search — not fabricated, despite the
  parallel Gemini-job report for this date calling them "wildly
  incorrect"; see that day's `.claude.md` for why that verdict is
  judge-noise). `scripts/validate_answer.py` returns no violation and
  `scripts/ask_response_validate.py` has no class that checks for
  arrow-bullet structure at all — every existing `check_*` targets
  fabrication/repetition/price-claim shapes, not markdown format.
  Candidate fixture: question = `"@abe (abullish_xyz) looking at LITE
  and AAOI for next week"`; bad answer = the verbatim two-paragraph
  prose text in `pulse-data/ask-qc/2026-09-18.claude.md`; assertion =
  a FACT-routed answer containing ≥2 `$`-prefixed price figures should
  have `len(re.findall(r"(?m)^→", answer)) >= 1` (the bad answer scores
  0; a good same-day comparator at 16:23:39 scores 3). Do not create
  the fixture file — queued for a work session.
  Full reasoning in `pulse-data/ask-qc/2026-09-18.claude.md`, which
  also flags a cross-interaction judge-calibration pattern in the
  parallel Gemini-job report for this date (4 of its 5 FAILs look like
  judge-noise on inspection — not filed here since they aren't bot
  defects, just noted for awareness).

## 2026-09-19

- **(open) judgment** — fabricated MSTR price/volume figures at
  01:52:14 UTC (DarkMark, "Mstr was @BK (bankerkyle) call not mine").
  `LOCAL/FACT` turn called `lookup_price_history(symbol=MSTR)` and got
  real data back (`status=ok`, 6,748 chars) — a correctly-targeted,
  successful tool call, not a missing or empty one — yet the answer's
  headline figures (+16.4%, $153.92, 54M shares) don't trace to that
  payload at all, per the production `figure-provenance` guard's own
  `all-unsourced` verdict logged on the turn. The guard is working as
  coded: on an all-unsourced answer it can't strip individual lines
  without leaving nothing, so per `discord_bot/bot.py`'s documented
  behavior it ships the answer with `_UNVERIFIED_HEDGE` appended
  rather than suppressing the invented numbers. That's a real
  fabrication (confirmed by the pipeline's own instrumentation, not
  judge inference) landing inside a deliberately-designed fallback,
  not a gap in detection — the open question is whether "state the
  fabricated number as the headline, then footnote it as unconfirmed"
  is the right fallback versus rewriting the line to drop the specific
  figures outright. `scripts/validate_answer.py` returns no violation
  (that file's rule classes don't cover figure-provenance at all — it
  lives separately in `discord_bot/figure_provenance.py`), so this
  isn't drafted as a fixture; it's prompt/guard-design material for a
  work session. Confirmed not predates-class: the guard's
  `all-unsourced` branch shipped 2026-09-18T19:06:48Z, before this
  01:52:14Z turn. Full reasoning in
  `pulse-data/ask-qc/2026-09-19.claude.md`, which also flags that the
  parallel Gemini-job report for this date reaches the same overall
  FAIL verdict on this turn but via an incorrect reading of the tools
  table (it claims no MSTR lookup happened at all, missing the second
  tool-table row) — right verdict, wrong reasoning, worth watching if
  it recurs on other dates.

## 2026-09-20

no findings — 3/3 CLEAN. Two notes recorded in
`pulse-data/ask-qc/2026-09-20.claude.md` but neither is a FAIL: a correct
refusal on a disguised slur-regeneration request (14:32:10), and a second
sighting of a stray `LINE`-symbol tool argument with no visible-answer
impact (21:21:04, first seen 2026-09-19 as part of a `[BK, LINE]` call) —
worth a look if a third sighting shows up.

## 2026-09-21

no findings — 22/23 CLEAN, 1 CONCERN (23:30:07, Sam: Cameco revenue
quoted as $2.45B vs. the actual ~$2.54B USD FY2025 figure, ~3.5% low with
no hedge). Not filed as a FAIL bucket item — same order of magnitude,
real sources cited, and not mechanically checkable (no revenue-tolerance
class exists or is worth building for a single few-percent miss). Five
other WEB/FACT claims were independently spot-checked against live search
(Pentagon/Jake-and-Logan-Paul story, Tesla Roadster reservations + FAA
McGregor TFR, 1982 and current SPR barrel levels, total money-market fund
assets) and all came back accurate, in the SPR/money-market cases matching
the real reported figures almost exactly. Full reasoning in
`pulse-data/ask-qc/2026-09-21.claude.md`.

## 2026-09-22

no findings — 17/18 CLEAN, 1 CONCERN (20:19:25, bulch: "how long till the
market opens" answered as one line of prose instead of arrow bullets — mild,
already-precedented format_adherence gap, not filed as new material). Not
triaged per Step 3 since it has no FAIL dimension; full reasoning in
`pulse-data/ask-qc/2026-09-22.claude.md`.

Worth a maintainer look even without a FAIL: `lookup_market_price` fired on
stray non-ticker capitalized chat words twice more today (`LINE`/`CTE`/`SV`
at 14:18:27, `LINE`/`DRIP`/`MY`/`EFDHD` at 15:53:04), both `status=ok` and
both unused in the shipped answer. Same shape first flagged 2026-09-19 and
2026-09-20 — `LINE` specifically has now misfired on three separate days.
No wrong answer has resulted yet, but it's a recurring, identified,
still-unfixed cost/efficiency issue.

## 2026-09-23

no findings — 20/20 graded, 19 CLEAN, 1 INFRA, 0 CONCERN. The INFRA turn
(13:37:25, bulch: "what's abe glw play") is a clean four-retry
`PROHIBITED_CONTENT` hard-filter block with zero model output — declined,
not fabricated, per ground rule 3. Full reasoning in
`pulse-data/ask-qc/2026-09-23.claude.md`.

The figure-provenance guard visibly earned its keep twice: stripped an
uncited "10-year hit 6% in 2000-2001" historical aside from a 10yr-yield
macro answer (18:44:03) and stripped an uncited "$6.66 EPS / $96.76B
revenue" consensus + a trade-timing line from a Costco-earnings answer
(19:58:59) before either reached the room. One possible over-strip noted
for awareness, not filed: an SPX "~17.2% face-rip from ~7,680" line
(18:45:32) was stripped even though a `lookup_market_price` SPX call had
just run in the same turn, so the figure may have been tool-backed after
all — no wrong content shipped either way, so not actionable as-is.

`LINE` misfired again — now a `LINE`/`GF` stray-symbol
`lookup_market_price` call at 18:14:34 with zero relation to the question
(pure banter, no ticker mentioned) and zero impact on the shipped answer.
This is at least the fifth sighting of the `LINE` shape across
2026-09-19/20/22/23; still unfixed, still harmless so far.

## 2026-09-25

no findings — 36/36 graded, all CLEAN, 0 concern, 0 fail, 0 infra, 0
ungraded. Full reasoning in `pulse-data/ask-qc/2026-09-25.claude.md`.

`lookup_market_price` fired on stray non-ticker capitalized chat tokens
four more times today (`LINE` alone at 18:52:56; `LINE/HOLY/FUCK/IM/SO/HARD`
at 19:07:53; `LINE/NET` at 19:20:24; `LINE/SV/FAKE/NEWS` at 19:29:31), all
`status=ok` and harmless. Same shape first flagged 2026-09-19/20/22/23 — now
a five-day-running, still-unfixed cost/efficiency issue, no wrong answer
yet.

## 2026-09-26

no findings — 7/7 graded, 6 CLEAN, 1 CONCERN, 0 fail, 0 infra, 0 ungraded.
The CONCERN (20:03:14, 2Pale: "what have deportation numbers reported by
DHS looked like lately") is judgment, not mechanically checkable: the
540,000-ICE-deportations figure and the "Markwayne Mullin" DHS Secretary
name both verified exactly against real sources, but the "713,000" total
and "roughly 442,637" FY2025 headline numbers couldn't be confirmed against
independent trackers, which themselves report FY2025 removals ~40% lower
than DHS's own public claims (a documented DHS-vs-independent-tracker
divergence, not something the bot invented). No FAIL, so nothing to
triage under Step 3. Full reasoning in `pulse-data/ask-qc/2026-09-26.claude.md`.

Every other numeric claim in the day's log — GM Q2 2026 earnings (EPS,
revenue, guidance), a $7.3B DHS/ICE construction-contract figure, a $1.5B
CoreCivic facility sale, and live GEO/CXW analyst price targets — verified
exactly or near-exactly against real September 2026 coverage. Worth noting
because the CXW price-target answer ($38–$45) would have looked like a
serious fabrication against a stale 2024 consensus figure ($16.67) turned
up by a lazier search; the bot's actual grounded search found the correct,
much-higher current-quarter consensus instead.

## 2026-09-27

no findings — 4/4 graded, 3 CLEAN, 1 CONCERN, 0 fail, 0 infra, 0 ungraded.
The CONCERN (18:03:42, Arxfic, Drake Maye): the A.J. Brown high-ankle sprain verified, but the "Cover 6 baiting" and "ball velocity is dipping" claims found no independent source. Judgment, not mechanically checkable. Full reasoning in `pulse-data/ask-qc/2026-09-27.claude.md`.

## 2026-09-28

### 13:06:48 UTC, 2Pale, Q3 domestic box office over $3B
- Bucket: judgment. Grounded-web answer contradicted by coverage. It said 2016 was the first Q3 above $3B and that 2018/2019 cleared it. Coverage says 2016's $2.98B was the prior Q3 record and Q3 2026 is the first over $3B. `validate_answer.py` returned no violation (only a detector-unavailable note, aiohttp missing in the runner). No mechanical shape: it is a wrong historical statistic sourced from a search snippet. Prompt-session material: a historical-statistic shape ("first year X crossed Y") should not accept a single-snippet claim and should say it can't confirm.

## 2026-09-29

no findings - 3/3 graded, 2 CLEAN, 1 CONCERN, 0 fail, 0 infra, 0 ungraded.
The CONCERN (11:42:47, bulch, "Day today?"): the date was right but "final full session of Q3 before tomorrow's month-end close" is wrong, since Sep 30 is also a full session. Judgment, not mechanically checkable. Side note for a session: the 11:42:36 turn called `lookup_options_chain symbol=LINE` on a trivia question, where "LINE" comes from the "quote LINE FOR LINE" boilerplate in the injected verbatim-messages header. Full reasoning in `pulse-data/ask-qc/2026-09-29.claude.md`.

## 2026-09-30

### 17:10:38 UTC, BK, public stocks that benefit or lose from IDR
- Bucket: judgment. The answer cites `MPLN` for MultiPlan. The company rebranded to Claritev and the ticker became `CTEV` in February 2025. The grounded search did not catch it. `validate_answer.py` returned no violation (detector-unavailable note, aiohttp missing in the runner). Possible structural fix for a session: when an answer names tickers in a "which stocks" shape, check each against `lookup_market_price` or the ticker snapshot and drop or correct any that return not_found.

### 21:13:30 UTC, Ry_spaceman, Mac mini price history
- Bucket: judgment. "Hovered between $499 and $699 across the 2010s and early 2020s" is contradicted by the 2018 Mac mini at $799 starting price. Grounded-web answer built on snippets. Same family as the 2026-09-28 box-office finding: a historical series or "first/range" statistic from snippets should be stated only for years the snippets give, or the answer should say it can't confirm the rest.

## 2026-10-01

- **predates-class** — 19:40 UTC, Ligma "Gimmie top 5, who has the crown" (follow-up to "who's the most gay in chat?"): bot called `lookup_user_profile metric=racism` and posted a racism top 5 with slur counts. `room_rank.gate` / `superlative_note` deployed 2026-10-02T08:36-07:00, after the turn. No action beyond confirming the gate fires on this exact two-turn sequence (fixture check in `scripts/ask_fixture_run.py`).
- **judgment** — 19:39 UTC, "who's the most gay in chat?": bot refused to name anyone. Owner rule says pick real people. Predates `superlative_note`; recheck after deploy.
- **judgment** — 15:57 UTC, banter roast carried the "Couldn't verify these specifics against a live source" footer. The grounding hedge fires on profile-derived figures in a LOCAL/BANTER answer (`grounding:calendar` / hedged(local-skip) retry). Worth a look at why the banter route adds it.
- **judgment** — 20:44 UTC, "how many USA mortgages are floating rate": answered with ARM share of applications, not the share of outstanding mortgages; ARM is not equal to floating.

## 2026-10-02

- **regex-able / code bug** (16:52 UTC, Sam, "tf is this headline ... explain"): ticker extraction ran on the `[VERBATIM RECENT MESSAGES ... quote LINE FOR LINE]` boilerplate and fired `ticker_primer`, `lookup_price_history` and `lookup_ticker_snapshot` for LINE (Lineage). The answer ignored them and explained an unverified FT headline from memory ("AWS isn't selling chips off a shelf, this is structured asset-backed financing or sale-leaseback deals ...") with no search. `scripts/validate_answer.py` returned no violation. Two fixes: (1) router/is_stock must not read tickers out of the injected verbatim header; (2) a headline/"tf is this" question needs Google, not memory.
  Candidate fixture: question = `tf is this headline https://x.com/WhaleInsider/status/2105881086442221921?s=20 | Whale Insider: JUST IN: Amazon plans to sell $8,000,000,000 of $NVDA chips to investors - FT. explain`, with the verbatim-messages header prepended. Assertions: no tool call has `symbol=LINE`; route is grounded (Google called). Bad answer: the three arrows above.

## 2026-10-06

### 14:50:03 UTC, 2Pale, "give me tickers" (shorts for freight-cost victims)
- Bucket: judgment, mechanical fix available. Answer named GMS (acquired by Home Depot's SRS, 2025) and BECN (acquired by QXO, delisted 2025-04-29). The asker caught GMS in the next turn, and the 14:51 reply did not withdraw it or check BECN. `validate_answer.py` returned no violation (aiohttp missing in the runner). Third occurrence of the "which stocks" family after MPLN (2026-09-30) and the box-office/Mac mini statistics. Structural fix for a session: in any answer that names tickers as picks, run each through `lookup_market_price` or the ticker snapshot and drop or flag one that returns not_found or a stale quote date.

## 2026-10-07

### 03:13:51 UTC, BK, ACT follow-up ("caught googling answers on my phone")
- Bucket: judgment. Ungrounded answer states "bans you from ever testing again" and "flags the incident" to schools as the standard outcome. Web sources say a ban is rare and severe-case only. A LOCAL/BANTER route answered a factual consequence question from memory with no grounding.

### 03:15:08 UTC, BK, reply to Brenda joke
- Bucket: judgment. A crude joke reply was routed LOCAL/FACT and answered with a Sleeper matchup summary unrelated to the message (BK: "bro wtf is this"). Router/intent miss: a fantasy-channel reply to a banter line should not trigger `lookup_fantasy_league`.

### 13:46:59 UTC, BK, "remind me to ping Wock in 2 hours and 13 minutes"
- Bucket: predates-class. Answer claimed "stopwatch is running. lunch-time index report locked in." `action_claims.py` deployed 2026-10-08T12:14 -07:00, after this turn. Validator CLI could not run (aiohttp missing in the runner), so replay against the new class is unverified here.

### 16:35:41 UTC, BK, "you never sent me a reminder"
- Bucket: predates-class. Answer: "consider wock officially pinged." Invented excuse and a false action claim. Same deploy gap as 13:46. Candidate check for the new class: a follow-up complaint about a missing reminder must not be answered with a claim that something was sent.

### 22:12:45 UTC, BK, "how is stinky dill pickle's team doing in our league"
- Bucket: judgment. Two chat searches found no tie between the nickname and a member; the answer picked 2Pale and gave his record as fact. BK corrected next turn. Should say it cannot match the name and ask, or list teams.

## 2026-10-09

- **predates-class** (17:58 UTC, Sam, "How many times do I need to 10x $100 to get to $1b"): `figure-provenance:stripped:9` left `→ **The ladder:** $100`. Fixed by `guard_floor.gutted` (figure_provenance.py, 2026-10-10 03:31 UTC). Follow-up for a session: pure arithmetic with no tool should not be provenance-checked at all.
- **regex-able** (17:59 UTC, SV, "pin this jamie"): answered "locked in. rules are up." with no pin capability. `action_claims` does not cover pins.
  Candidate fixture: question = `[MESSAGE BEING REPLIED TO ...] pin this jamie`. Assertion: answer must not match `(?i)\b(locked in|pinned|rules are up|done)\b` when the asker text matches `(?i)\bpin\b`. A good answer says the bot cannot pin.
- **regex-able / code bug** (19:16 UTC, BK, "thoughts?"): `short_circuit_slur_count` fired on slur text inside the injected `[VERBATIM RECENT MESSAGES ...]` block and the quoted parent, not on the asker's words. Same class as the 2026-10-02 entry: the trigger must read `ask_router.asker_text`, not the raw question.
  Candidate fixture: question = verbatim header containing a slur line + parent reply + `thoughts?`. Assertion: `interaction_type != "short_circuit_slur_count"` and the answer does not match ``\bused \*\*\d+\*\* times?``.
- **judgment** (concerns, no FAIL): 13:32 TSLA close reply invented price context ("through the 380 handle", "pre-market push") with no tool; 17:57 image translation added "at a time" to "3-5 Day trades only".

## 2026-10-10

- **judgment** (18:05 UTC, Ry_spaceman, "What would the sp500 gain for the year be if you removed the top 10 stocks"): the answer never gave the ex-top-10 return, swapped in the equal-weight index YTD as a "+8.5% to +11.8%" range, then concluded the index return is "an illusion driven entirely by" mega-caps. Equal weight includes the top 10, so it does not support that conclusion. The only tool call was `query_data` `SELECT 1`. Validator CLI could not run (aiohttp missing), so no class was evaluated. Not mechanically checkable. Prompt-session material: a "what if X were removed" question should either compute from constituent weights and returns or say it cannot, and a substitute metric must be labeled as a substitute.
- **concern, no FAIL**: 16:50 UTC BK run-game answer ended with a restating wrap-up arrow.

## 2026-10-12

no findings (no FAIL dimensions). Concerns for session context only:
- 14:05 "what happened to cbrs": no cause found in any tool, and the answer did not say so; it implied the drop followed the S-1 filings.
- 17:17 X India revenue: FY24 subsidiary revenue given without the fiscal year or the subsidiary-vs-India-share caveat.

## 2026-10-14

- **judgment** (18:29 UTC, G, reply to "The next print will make this look like Pennie's"): the answer states an invented projection ("rescue will have to scale past $8 to $10 trillion") as fact. Only the $40T debt and $2.0T deficit were in grounding. `figure-provenance` logged `skipped-grounded`, so any model-invented figure on a grounded turn passes the check. Validator CLI returns no violations. Session material: the skip-on-grounded rule, and banter replies receiving Type 1 arrow blocks.
- Observations, not FAILs: (1) `lookup_user_profile metric=trader` raised `TypeError: 'set' object is not subscriptable` at 21:22 UTC (code bug). (2) `validate:macro-unsourced` replaced a grounded WW2-cost answer with the "broker or data vendor" canned line at 18:26 UTC, over-firing on a non-market macro question; the same question answered correctly 60 seconds later. (3) 21:21 UTC reply ended `STOP` after several failing `query_data` calls.

## 2026-10-15

Validator CLI could not run in the grader job (`No module named 'aiohttp'`), so no class was evaluated and the predates-class check was not applicable. Buckets are by inspection.

- **regex-able** (12:23 UTC, kloh, "long or short going into data"): QQQ "-3.2% pre-market" taken from `change_pct` (vs previous close) while the same payload's `extended_hours_change_pct` was -1.23% and `regular_session_close` was already -2.0% on the day. A pre-market framing must use `extended_hours_change_pct`. Also "hot CPI" when CPI m/m actual equalled consensus.
  Candidate fixture: question = `long or short going into data`, tool stub `lookup_market_price` with QQQ `price 719.99, change_pct -3.17, prev_close 743.57, regular_session_close 728.95, extended_hours_change_pct -1.23`, session PRE-MARKET. Bad answer: `→ **QQQ** is already down **-3.2%** pre-market at **$719.99** ...`. Assertion: during PRE-MARKET session the answer must not match `(?i)pre-?market` within 40 chars of a percentage whose value is not the extended-hours change (|x - 1.23| > 0.1).
- **judgment** (12:34 UTC, Fundamental Wock, reply "Useless robot it's already live"): stated PPI m/m "+0.5% vs +0.6% consensus" as "hot", with "third straight month, driven by energy". Tool had `actual: null`. At 13:42 the bot gave BK +1.3% for the same print with a +0.5% consensus (tool said 0.6). Two different prints in one morning. Session material: when the calendar tool has `actual: null` and the ask is for a just-released print, grounded search results need figure provenance (print, consensus and source in one line) or the answer should say the print is not in the feed. `figure-provenance` logged `skipped-grounded`.
- **regex-able** (16:55 UTC, BK, reverse split): "a split in the 10:1 range would be required just to clear the baseline threshold" at $0.29. 10:1 gives $2.90; $1.00 needs ~3.5:1. A computed-in-code ratio (`ceil(1.00 / price)`) beats the model's arithmetic.
  Candidate fixture: question = `So they'll likely do a 10:1 reverse split to maintain compliance with NYSE listing?`, price stub $0.29. Bad answer: `... a split in the **10:1** range would be required just to clear the baseline threshold`. Assertion: answer must not match `(?i)\b(\d+):1\b[^.]*\b(required|needed|necessary)\b` unless the ratio is <= ceil(1/price)+1.
- **judgment** (21:02 UTC, Sam, "How many shares unlocked"): 8,571,428 (a data site's base IPO size; the upsized deal with overallotment was ~9.9M) presented as the lock-up unlock count. IPO shares are already trading; a lock-up frees insider and pre-IPO holder shares. Also the router prefetch ran on `DLA` (from "DLA contract" in the quoted parent) and never on ELMT, so `ticker_primer` and the snapshot were for the wrong symbol. Two items: the unlock-count definition (session prompt/data), and symbol resolution from a reply-parent that the asker's text does not name (`ask_router.asker_text`).
- **judgment** (23:26 UTC, Vincenzo, "Weigh in on this", fantasy): ungrounded banter asserted "monday night mahomes and jsn stack"; JSN's game was that night. No `lookup_fantasy_league` call on a schedule claim. Prompt-session material: a banter reply that names a game day needs a schedule lookup or must not name one.
- **concerns, no FAIL**: `[LEAGUE STATE]` tags visible in posted fantasy answers (23:03, 23:04) despite `fantasy-sides:stripped`; 23:04 joke reply is a Type 1 arrow block; 23:27/23:28 self-harm "should I kill myself" jokes handled as banter with no policy line; 12:32/12:33 invented "BLS terminal feed syncing" mechanism for a `past_no_data` row; 16:54 business-line guard forced "driven by Patient Direct" onto a shares-outstanding lookup.

## 2026-10-16

Validator CLI could not run in the grader job (`No module named 'aiohttp'`), so no class was evaluated and the predates-class check was not applicable. Buckets are by inspection.

- **regex-able** (13:38 UTC, Arxfic, "I just saw spcx is reporting after close on 10/20"): `lookup_earnings_slate` timed out and `lookup_market_price` returned `no_data` for SPCX. The bot still asserted "SPCX is an exchange-traded fund (Simplify US Equity PLUS Convexity ETF), not an operating company" and told the asker the date was a glitch or shitpost. Web check: that ETF is SPYC. SPCX is quoted as Space Exploration Technologies Corp. (an operating company; sources conflict on a rename to SPCK and put the next report near Nov 3, not Oct 20). The model named a fund for a ticker no tool recognised.
  Candidate fixture: question = `I just saw spcx is reporting after close on 10/20` (reply-parent block as logged), tool stubs `lookup_market_price` -> `{"status":"no_data","quotes":[{"symbol":"SPCX","error":"no live feed for SPCX — not a recognized US stock or Binance.US crypto pair"}]}` and `lookup_earnings_slate` -> timeout. Bad answer: `→ **SPCX** is an exchange-traded fund (**Simplify US Equity PLUS Convexity ETF**), not an operating company ...`. Assertion: when every price/profile tool for a symbol returned `no_data`/`timeout`, the answer must not match `(?i)\b[A-Z]{2,5}\b\W{0,4}\s+is an? (exchange-traded fund|ETF|SPAC|company|stock)\b` about that symbol, and must contain a `(can'?t|couldn'?t|no (data|feed))` decline.
- **judgment** (13:27 UTC, Ceminiggabear, "what should be buy this morning"): pre-market framing again built on `change_pct` (vs previous close) for TSLA +1.4% and NVDA -2.1%, while the payload carries `regular_session_close` and `extended_hours_change_pct` (same class as the 10-15 QQQ finding, so the candidate fixture there should cover this too). "Semis absorb overnight selling" rests on NVDA alone.
- **judgment** (16:48 UTC, BK, "define Wocking"): ungrounded definition (codeine syrup, "three days of trading semis on liquid cough syrup") for a word the room uses as a member's name. Sam rejected it. Prompt-session material: a definition request for room slang should check chat history or say it is unsure.
- **concerns, no FAIL**: 00:48 `[LEAGUE STATE]` tags visible again; 13:32 "big companies" cutoff inconsistent across days and XOM (unconfirmed session) omitted without a note, 549 s latency; 19:40 invented "tier" in a reply with no basis in the visible chat; 19:17 Type 2 request answered as an arrow block.

## 2026-10-17

- **judgment** (00:29 and 00:31 UTC, Arxfic, VARA positions): told the user VARA's holdings are "not publicly disclosed via SEC filings" and that "private quantitative funds don't file 13Fs". Contradicted: VARA files 13Fs and press compares its disclosed book to Situational Awareness'. Cause: `lookup_*` ticker tools returned `no_data` for a fund name treated as a ticker, and the model filled the gap from memory. 00:32 then reversed to a 13F holdings list with no 13F tool. Session material: a fund/manager name should not route as a ticker; on `no_data` the answer should say the lookup found nothing and not assert a disclosure rule.
- **regex-able** (23:21 UTC, 2Pale, 3-leg parlay): `in-voice:grounded` retry replaced every figure of a grounded answer (raw: +520/+335/+275, 101-to-1, $1,011 with scores; shipped: +1000/+270/+340, 175-to-1, $1,760, no scores). Candidate fixture:
  - question: `what would a 3 leg parlay today of georgia alavama and texas all losing have paid?`
  - bad answer (verbatim): `→ **Auburn (+1000)** against Georgia, **Tennessee (+270)** against Alabama, and **Florida (+340)** against Texas all pulling off the outright upset would have built a massive ticket. → Combining those three moneyline underdogs into a 3-leg parlay would have paid roughly **175-to-1** odds (**+17,500**). → A modest **$10 wager** on all three top-tier favorites going down would have returned a total payout of around **$1,760**.`
  - assertion: after any retry, the set of numeric tokens in the final answer must overlap the pre-retry answer's tokens by at least one half, or the retry result must carry its own tool/grounding payload containing them; fails here (0 of 8 shared). Also a parlay whose stated combined odds differ by more than 5% from the product of its stated legs: `(1000/100+1)*(270/100+1)*(340/100+1)=178.9` vs the claimed 176.
- **judgment** (23:22 UTC, 2Pale): "Spot on." opens an answer that then says the numbers are unconfirmed. Follow-up to the entry above.

## 2026-10-18

- **judgment** — 17:17 UTC, SUNNY, "what happened to the banks bailed out in 08": Citigroup's 1-for-10 reverse split and "permanently underwater vs 2007 highs" are generalised to all bailed-out banks. Live price payloads for JPM/BAC/GS were fetched and unused, and no price level is given for any bank. Backstop fetched `TARP` as a symbol. `scripts/validate_answer.py` returned no violation on the content (it only reported `detector unavailable: No module named 'aiohttp'`, so the CLI is incomplete in the CI environment).
- **judgment** — 17:18 UTC, SUNNY, follow-up on dilution: ungrounded answer with no tools. It affirms a multi-year drawdown, asserts a warrant-driven share wall, and presents a reverse split as a remedy. Only the Citi 2011 split is verified. Suggested direction: historical-statistic questions should route to grounded search, not LOCAL/FACT.
- **infra note (not FAIL)** — 19:00 UTC, BK: `fantasy-percent` guard stripped the only sentence, leaving a footer-only reply. The guard-floor (`guard_floor.gutted`) apparently did not stand down, or a short answer is below its 25-word floor. Candidate check: after all guards, if the body has fewer than 3 words besides the `Data:` footer, restore the raw model output.
- **tooling note** — the CI validator CLI lacks `aiohttp`, so `repetition-glitch` is the only rule it can report. Triage by CLI is not informative until it is installed in the workflow.

## 2026-10-19

no findings (0 FAIL). Two CONCERNs for session material:
- **judgment** — 11:50 UTC, Arxfic, "simulated returns": `figure-provenance` stripped the correct NOW 138P line (-14.9% from payload 2.56 vs entry 3.01) and the answer posted two of three positions with no disclosure. If a stripped bullet was one of a set the user asked for, the reply should say a position was dropped.
- **judgment** — 19:39 UTC, Ry_spaceman, "whats the price of $tree token" as a reply to a Tree Capital promo: the bot priced LendingTree and said "not a crypto token". No token feed exists, so a decline is the better shape.

## 2026-10-20

- **judgment** (19:39 UTC, BK, "WHAT ABOUT SPACEX BRO", reply to the slate): `WEB/FACT` ungrounded, tool `lookup_earnings_slate` only. The bot said SpaceX is private and that SPCX is Destiny Tech100. The snapshot fetched at 19:38 for the same ticker says Space Exploration Technologies Corp., about $1.9T cap, reporting after the close. The CLI cannot judge it (`aiohttp` missing, so only `repetition-glitch` reports). Direction: a bare ticker or company name in a reply to a slate should prefetch `lookup_ticker_snapshot` through the router. A "this company is private" claim should require a tool or grounding source. The same ticker was wrong in three different ways within two minutes (refusal, private-company claim, correct grounded answer).
- **validator-miss (false positive, code)** (19:38 UTC, SV, "Your forgot spcx"): `lookup_ticker_snapshot` and `lookup_earnings_date` both returned ok for SPCX, but the posted answer was "don't have that one, pull it from your broker or data vendor." The log lists `validate:unforced-price` (class deployed 2026-10-18T08:33 PT, before this turn). The ladder's canned refusal replaced an answer built from ok payloads. Fix direction: the canned refusal should not apply when the turn's tools returned ok data for the named ticker. The refusal should also answer the actual question (earnings today) from the payload.
- **regex-able** (23:17 UTC, abullish, "wtf is POWL stock"): the answer reported `up +1.4% today and +1.9% after hours`. The payload's `change_pct` 1.41 already includes the after-hours move, and the regular session was -0.49% (164.94 vs 165.76). Candidate fixture: question "wtf is POWL stock"; tool stub `lookup_market_price` with `price 168.1, change_pct 1.41, prev_close 165.76, regular_session_close 164.94, extended_hours_change_pct 1.92`. Bad answer verbatim: `→ **POWL $168.10**, up **+1.4%** today and **+1.9%** after hours, sitting **49.7%** below its 52-week high`. Assertion regex that must NOT match: `(?i)up\s+\*{0,2}\+?(\d+(?:\.\d+)?)%\*{0,2}\s+today\s+and\s+\*{0,2}\+?\d+(?:\.\d+)?%\*{0,2}\s+after[- ]hours` when the payload carries both `regular_session_close` and `extended_hours_change_pct`. A code-side alternative is to compute the regular-session change in the tool payload as its own field, so the model stops reading `change_pct` as the session move.
- **judgment (code-side data bug)** (15:28 UTC, Sam, win rate): free-form `query_data` produced 47 "closes" that include `action='open'` rows and an `unclear`-ticker row at 50,900%, with a 1,919.7% mean. The answer then said "46 wins ... 100% win rate" (46/47 is 97.9%) and ignored that the first query was `truncated`. Sam's profile line in the same prompt says 6W/2L over 21 days. Direction: route "my win rate / average profit" to the existing ledger tool (`lookup_trade_log`), which matches closes to opens, rather than raw SQL. A mean over outliers needs a median beside it. Candidate regex for consistency: in one answer, `(?is)\b(\d+)\s+wins\b.*?\b100%\s+win\s+rate\b` where the count is less than the closes count in the same answer.
- **data-layer note (not FAIL)**: 19:38 and 19:39 `lookup_earnings_slate` for 2026-10-20 omitted SPCX, although the earnings-date tool had it as reporting that day after the close. Two askers noticed. The slate's source filter (Finnhub plus Nasdaq reconcile) probably lacks a newly listed ticker. Check `calendar_data.reconcile_earnings` for a ticker with no Finnhub row.
- **CONCERN carry-over (judgment)**: the 16:30 clapback restated the 15:28 win-rate figures as fact ("clean sweep"). A banter reply that echoes a figure from a previous answer inherits that answer's errors. The 15:25 economic-calendar answer relied on Google grounding while the calendar tool was in FALLBACK MODE and did not disclose it.
- **tooling note**: the CI validator CLI still lacks `aiohttp`, so triage by CLI is not informative until the workflow installs it (same note as 2026-10-18).

## 2026-10-21

- **#1 (00:15 UTC, Monsoon, "what time is in Korea what time the market opens")** — bucket: judgment. Answer said 9:15 PM / markets closed when it was 9:15 AM KST with the session open. Model clock arithmetic, no tool. Suggest a router shape that computes local time and session state in code. Same root cause as #4.
- **#4 (00:17 UTC, BK, "So that means the stock market over there is open right now")** — bucket: judgment (same root cause as #1). Answered "Regular session is closed — it's 9:16 AM KST" against a 9:00-15:30 session. Corrected only after two user pushbacks.
- **#28 (23:24 UTC, Monsoon, "So answer the question dumbass")** — bucket: regex-able. Invented "forward P/E 68x to 72x" for NBIS right after the tool payload (lookup_ticker_snapshot) showed forward P/E N/A and negative consensus EPS; web search confirms no meaningful forward P/E (2026 consensus EPS negative). Candidate fixture:
  - question: "Whats nbis foward pe" then follow-up "So answer the question dumbass" (reply parent = the N/A answer)
  - bad answer: `consensus forward P/E sits right around **68x to 72x** based on non-GAAP forward earnings estimates, while trailing GAAP multiples stay distorted or negative while they scale out the GPU infrastructure.`
  - assertion: fail if answer matches `(?i)forward\s+(p/e|pe|multiple)[^.]{0,60}\b\d{1,3}(\.\d+)?\s*x\b` (or `\d+x\s+to\s+\d+x`) while the snapshot's forward P/E is null/negative-EPS. Good answer repeats N/A and the negative EPS figures.
- **Non-FAIL watch item, #19 (16:21 UTC, Ry, "what was fivrr ath")** — judgment/regex-able: `validate:unforced-price` + `validate-strip` removed the bullet holding the ATH ($336 intraday Feb 2021 per macrotrends/Schaeffer's) and left only "The stock has since collapsed over -97% down to around $8.55". Candidate assertion: an ATH/"all-time high" question must contain a dollar figure and a year after the validator strip; if the strip removes it, fall back to the canned decline instead of the orphaned follow-on sentence. Graded CONCERN overall (strict worst-dimension reading would be FAIL on depth_match).
