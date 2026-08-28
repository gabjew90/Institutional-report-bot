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

- 1. regex-able (X-posting cost, 01:00 UTC turn) — `LOCAL/BANTER` route,
  zero tool calls, no search grounding, answer states two precise
  per-post dollar figures ("$0.015 per post", "$0.20 per post... official
  V2 pay-per-use meter") with full confidence. `validate_answer.py
  --tools ""` returns clean — none of `check_unforced_price` /
  `check_unforced_market_data` / `check_macro_unsourced` look outside
  ticker prices, options-chain stats, and macro print figures, so this
  is uncovered territory, not a miss. Candidate `check_unforced_unit_cost`
  fixture (question, bad answer, assertion regex, hedge/grounding
  carve-out) is drafted in full in `pulse-data/ask-qc/2026-08-27.claude.md`
  under the 01:00 entry. Same shape recurred at 21:58 UTC same day
  (Jackson Hole keynote specifics) but with a named person + exact time
  + venue instead of a dollar figure — see finding 2, bucketed separately
  because the fix there isn't a regex.
- 2. judgment (Jackson Hole keynote specifics, 21:58 UTC turn) — same
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
