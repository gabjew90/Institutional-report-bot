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
