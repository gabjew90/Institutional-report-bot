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
