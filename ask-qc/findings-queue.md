# Ask-QC findings queue

Entries are appended by the nightly headless judge, one heading per graded
date. Entries are removed by the work session that ships or rejects them —
never by the judge.

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
