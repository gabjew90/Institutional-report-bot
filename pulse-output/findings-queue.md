# Pulse findings queue

Appended by the daily-qc pulse judge (8 AM Pacific), one heading per
graded run. Entries are removed by the work session that ships or
rejects them — never by the judge.

## Seeded 2026-08-28 — open items from the 2026-08-21 QC review

These four sat in a QC review (and a session-memory note) for a week
with no owner, which is the failure mode this queue exists to end.

- **prompt-session / pilot-territory** — HIGH-CONVICTION single-bank
  themes (memory, copper) dropped two runs straight: no escalation
  path when theme slots fill. The claim-card redesign's ledger owns
  the structural fix; a slot-pressure escape hatch in DRAFT is the
  interim option.
- **deterministic-fixable** — thin-2bank promotion keys on bank
  count and is blind to PDF count (Jackson Hole cluster blocked as
  thin on 7 PDFs). The promotion rule is code; a PDF-count OR-term is
  a scripted change with a fixture.
- **deterministic-fixable** — volume gate: 26 PDFs consumed while 51
  were available at 14:31 (restart-on-delta suggestion from the 8/21
  QC). Driver/gate change.
- **observation** — progress events (pulse-output/progress/) dead
  since 8/19; the routine-side PAT for the urllib PUT rotated.
  Observability only; the driver artifacts now cover most of what
  progress events showed.

## 2026-08-31 — headless QC on 2026-08-31T14-07-12Z

Full report: `pulse-output/qc-headless/2026-08-31T14-07-12Z.md`.

- **prompt-session** — DRAFT fabricated a Treasury yield data point in
  the MAIN EVENT: "the 2-year yield jumped 11 basis points to 4.34%"
  (RECAP + MAIN EVENT, both instances). No 2-year yield series exists
  in this pipeline's live market data (only 5Y/10Y/30Y), and the only
  "11 bps" figure anywhere in the run's research corpus is a
  **30-year** yield move tied to a **different event** (Warsh's July
  FOMC presser, not this weekend's Jackson Hole speech). Present in
  DRAFT before EDIT touched it; survived 3 adversarial passes, SCRUB,
  and both validate gates untouched. This is the load-bearing evidence
  for the MAIN EVENT's central claim (yield curve shape = hiking cycle
  not inflation) and neither the adversarial checker nor STEP 7's
  self-review caught it. Needs a DRAFT/AUDIT constraint: any cited
  yield-curve point/change must match a `key_data_points` entry on
  tenor + metric + figure, not just figure.
- **prompt-session** — same EDIT attribution-stripping pattern STEP 7
  already flagged (Bloomberg 4→0, Goldman 13→10) also produced a
  shipped defect, not just internal deletions: "moved from roughly 35%
  to between 58% and 60% (JPMorgan)" credits both numbers to JPMorgan,
  but 58% is JPMorgan's figure and 60% is Bloomberg's independent one.
  Fold into whatever prompt-level fix addresses the attribution-count
  stripping — the fix needs to also cover ranges that splice two
  banks' numbers under one name.
- **deterministic-fixable** — corroborates STEP 7's find with
  independent verification: `hyperscaler financial impacts` (4
  sources: ZeroHedge, "Tyler Durden (ZeroHedge) / Morgan Stanley",
  Goldman Sachs, The Market Ear) confirmed zero representation in the
  final pulse. The Morgan Stanley presence lives only inside a
  composite source-label string in the adjudication input, consistent
  with STEP 7's diagnosis that the `falsifiable_prediction bank not in
  inputs or agency whitelist` validator can't match it. Widen the bank
  check to the theme's own cluster bank list; degrade discard to
  field-null instead of killing the whole theme.
- **deterministic-fixable** — `pulse-context/latest.json` is not
  scoped per-run; it gets overwritten by whichever pulse invocation
  (scheduled or manual/test) runs last that day. Today's file reflects
  a 20:31 UTC manual run (44 PDFs, last-24h) instead of the 14:07
  scheduled run's actual 64-PDF context. This makes it unsafe as a
  verification artifact for any headless QC run later in the day, and
  it produced a false lead on this run (a quote that looked
  unsourced in `latest.json` was fully sourced in the run-scoped
  `agent-io/$TS/edit-prompt.txt`). Either stop overwriting `latest.json`
  intraday, or point QC instructions at the per-run-timestamped
  artifacts (`qc-inputs/$TS.adjudication-inputs.json`,
  `agent-io/$TS/*.txt`) instead.
- **deterministic-fixable** — the adversarial-verdict control-series
  directory (`pulse-output/adversarial/`) isn't written to
  consistently: 2026-08-28's verdict landed at
  `qc-inputs/2026-08-28T14-10-34Z.adversarial-verdict.json` instead,
  and would be silently missed by anything that only reads
  `pulse-output/adversarial/`. Separately, today's stored verdict file
  is the post-repair recheck (`{"findings": []}`) — the original 2
  hard findings that triggered the two repair rounds aren't preserved
  as their own artifact anywhere. If the shadow-pilot fidelity
  comparison needs "did the checker catch anything pre-repair" as a
  signal, that data point currently only survives inside the driver
  JSON's `detail` strings, not as structured data.
- **observation** — STEP 7's self-review calls `ai deflationary
  impact` a "(6 banks)" theme with zero final representation, ranking
  it alongside the confirmed `hyperscaler financial impacts` miss. Per
  the same `qc-inputs` adjudication file STEP 7 used, this theme's
  `theme_stances` show exactly one named bank (Apollo) plus eight
  generic `"multiple desks (unattributed)"` entries — not six distinct
  named banks. Couldn't reproduce STEP 7's count. Not filing as a
  correctness bug since it's possibly just a miscount in prose, but
  worth someone checking where the "6 banks" figure came from before
  it's used to justify a fix — the severity ordering between this
  theme and the hyperscaler miss may be backwards.

## 2026-09-01 — headless QC on 2026-09-01T14-09-32Z

Full report: `pulse-output/qc-headless/2026-09-01T14-09-32Z.md`. STEP 7's
self-review this fire was unusually thorough (`qc-reviews/2026-09-01T14-09-32Z.md`)
and I independently verified its central claims from `pending-adjudications.json`
and the drafts/pre-scrub diff rather than taking them on faith; no disagreements
on substance. Below are the items that are new or that cross a confirmation
threshold today.

- **deterministic-fixable** — `pulse-context/latest.json` overwritten-by-later-run
  staleness bug, now confirmed **2/2** (first flagged 2026-08-31, recurs today:
  `dumped_at_utc` in `latest.json` is 2026-09-01T18:12:28Z / 59 PDFs / last-24h,
  while this run's actual scheduled dump was 13:37:33Z / 57 PDFs, per the archive
  frontmatter). Two confirmed occurrences is enough to stop treating this as
  "worth tracking" — it's systemic. Either stop overwriting `latest.json`
  intraday, or repoint QC/verification tooling at the run-scoped
  `pulse-output/pending-adjudications/$TS.json` / `drafts/$TS.md` /
  `pre-scrub/$TS.md` (which is what today's headless QC used instead, and it
  worked fine as a substitute).
- **deterministic-fixable** — `slot-stat-overlap` lint false positive, now
  confirmed **5/5** sampled days (2026-08-27 "15%", 08-28 "3%", 08-31 "4%"/"34%",
  09-01 "40%"). Today's instance verified by hand: the two citing sentences
  ("OpenAI and Anthropic could absorb 40% to 50% of all incremental computing
  power" vs. "roughly 40% of the S&P 500 enters the... buyback blackout window")
  share nothing but a round number. STEP 7's proposed fix (key on `(value, unit,
  nearest subject noun)` instead of the bare numeral, in
  `scripts/pulse_lint.py :: _check_slot_stat_overlap`) is already written up with
  scope in today's qc-reviews file — promoting from "worth tracking" to "should
  ship" given the every-single-day recurrence.
- **deterministic-fixable** — duplicate "highest since January 2025" superlative
  attached to two different 10-year Treasury yield figures in the same pulse:
  RECAP says 4.78% is "its highest since January 2025," the MAIN EVENT bullet
  says 4.75% (Monday's close) is "its highest since January 2025." Both survived
  all four adversarial repair rounds untouched — the repairs fixed the *date*
  attribution on the 4.75% figure (Friday→Monday) but nobody checked the two
  superlatives against each other. Needs either a deterministic check (two
  distinct figures for the same series+metric both claiming the same superlative
  in one document is always a contradiction) or folding into the adversarial
  checker's remit, since it's exactly the kind of cross-reference error that
  checker already catches for dates/attributions.
- **observation** — the Jackson Hole discard (composite source-string bug,
  `_is_valid_source` at `synthesis-routine.md:1212`, already fully written up by
  STEP 7 with scope/fix) and the "Jackson Hole never named in the final despite
  being the MAIN EVENT's entire basis" reader-comprehension defect are the same
  failure surfacing twice, confirmed via `pending-adjudications.json`'s
  `discarded_themes` reason string matching the theme's own `banks_for` list.
  Not filing as a separate item — STEP 7's queued fix for the source-string bug
  should be understood as also fixing the naming gap, since the discarded
  theme's `facts_agreed` block is where "Jackson Hole" as a proper noun lives in
  the pipeline. Worth confirming next run that a fix restores the name, not just
  the theme's validation status.

## 2026-09-02 — headless QC on 2026-09-02T14-08-29Z

Full report: `pulse-output/qc-headless/2026-09-02T14-08-29Z.md`. STEP 7's
self-review this run was again unusually thorough
(`qc-reviews/2026-09-02T14-08-29Z.md`); I independently verified its core
claims against `pulse-context/latest.json`'s `analyses_json` by direct grep
rather than trusting the self-review, and found no disagreement on substance.
Items below are new, or cross a confirmation threshold today.

- **prompt-session** — "highest since January 2025" is a recurring wrong
  anchor for the 10-year Treasury yield, now confirmed **2/2 consecutive
  scheduled pulses** (2026-09-01, 2026-09-02) with a known-correct answer.
  Today's corpus is unambiguous and internally consistent across two
  independent PDFs that the correct anchor is **October 2023**
  (`"US 10yr Treasury yield reached 4.80%, the highest level since October
  2023"` — Deutsche Bank; corroborated by a second PDF referencing "the
  October 2023 yield peak of 4.99%"). Nothing in either day's corpus contains
  "January 2025" for this figure. 2026-08-31's pulse discusses the same yield
  curve without this phrase at all, so it isn't a standing error — it started
  2026-09-01 and repeated 2026-09-02 with a different yield reading each time
  (4.78%/4.75% then 4.80%/4.79%). STEP 7's 2026-09-01 entry on this (filed as
  "duplicate superlative attached to two different figures," proposing a
  same-document cross-reference check) treated it as an unresolved two-way
  conflict with no known-correct value. Having checked the corpus directly,
  it isn't ambiguous — October 2023 is right, January 2025 is wrong, and the
  fact it's the same wrong phrase two days running with different underlying
  figures suggests EDIT/RECAP-authoring may be carrying phrasing from the
  previous day's own published pulse rather than deriving it fresh from the
  current corpus each run. Needs a DRAFT/AUDIT-side fix, not just a
  same-document consistency check.
- **deterministic-fixable** — `pulse-context/latest.json`
  overwritten-by-later-run staleness bug, now confirmed **3/3** occurrences
  (2026-08-31, 2026-09-01, 2026-09-02). Today's instance: `latest.json`'s
  `dumped_at_utc` is `2026-09-02T18:14:32Z` / 55 PDFs / last-24h window, while
  the archived pulse's own frontmatter shows `dumped_at_utc
  2026-09-02T13:55:21Z` / 29 PDFs / since-last-pulse window — a ~4.5 hour-later,
  differently-windowed dump. Practical cost today: RECAP's live price
  percentages were unverifiable against this artifact (the stale snapshot's
  numbers differ from the published ones by normal intraday drift, not error,
  but I can't prove that without a timestamp-matched snapshot). Three
  confirmed occurrences across three different sessions' QC reviews is past
  "worth tracking" — either stop overwriting `latest.json` intraday, or point
  QC tooling at the run-scoped `pulse-output/drafts/$TS.md` /
  `pre-scrub/$TS.md` artifacts the way 2026-09-01's QC did as a workaround.
- **observation** — `pulse-output/adversarial/2026-08-28T14-10-34Z.json` is
  missing from disk even though that day's driver trail proves the gate ran
  and recorded a result inline (`"0 hard findings, 4 soft (recorded)"` at
  2026-08-28T14:33:28Z). Distinct from 2026-08-24 through 2026-08-26, where
  `gates.adversarial` is `null` because the gate didn't exist in the routine
  yet — 08-28 is a case where the result existed and the standalone verdict
  file wasn't persisted. Low priority (one historical day, control-series
  completeness only) but worth a one-line check in whatever writes that file
  to confirm it isn't silently dropping writes on other days too.
  **Correction (2026-09-04 QC):** the file isn't missing — it's at
  `pulse-output/qc-inputs/2026-08-28T14-10-34Z.adversarial-verdict.json`,
  a different directory and filename suffix than every other day's
  `pulse-output/adversarial/$TS.json`. Not a dropped write, just an
  inconsistent path convention on that one day. Downgrading this item's
  urgency accordingly — still worth a one-line consistency fix, but there's
  no data-loss risk to chase.

## 2026-09-04 — headless QC on 2026-09-04T14-07-25Z

Full report: `pulse-output/qc-headless/2026-09-04T14-07-25Z.md`. STEP 7's
self-review (`qc-reviews/2026-09-04T14-07-25Z.md`) was again very thorough;
I independently reproduced its two headline accuracy findings against
primary sources (not just its paraphrase) and both hold up, one of them
more severely than scored. Also found one new sourcing-boundary defect and
escalated the standing `pulse-context/latest.json` staleness item.

- **prompt-session** — fabricated CEO-succession claim in the $ADBE WATCH
  bullet: published pulse says "The succession question is now settled,
  with Anil Chakravarthy named Thursday to replace Shantanu Narayen."
  Verified against the full context dump (`pulse-context/latest.json`, all
  keys, not just `analyses_json`): zero occurrences of "Chakravarthy" or
  "Narayen" anywhere, and the one source on this topic (Deutsche Bank's
  Adobe F3Q preview) says the opposite — "a lack of CEO succession
  clarity." `pulse-output/drafts/2026-09-04T14-07-25Z.md:62` (DRAFT) has
  the correct version ("no clarity on CEO succession"); the fabrication
  first appears in `pulse-output/agent-io/2026-09-04T14-07-25Z/
  adversarial-prompt.txt:90`, meaning EDIT introduced it, and it is
  byte-identical in `adversarial-prompt-2.txt:90` and
  `adversarial-prompt-3.txt:90` — the adversarial checker had three
  chances to catch a direct source inversion with two invented named
  individuals and a specific day, and missed it every round. STEP 7 filed
  this under the same heading as the numeric-provenance-checker gap
  (`_edit_introduced_numbers` in `synthesis-routine.md:2184` is
  numbers-only); that fix (widening the allowed-strings blob) does not
  address this class at all, since nothing here is a number. Needs a
  separate check: named-entity / dated-corporate-action claims introduced
  at EDIT that don't appear in any context source should hard-fail the
  same way an unverified number does. Scope estimate: extract
  proper-noun-plus-date spans from EDIT's diff against DRAFT (a much
  narrower net than full NER — corporate actions read as "named person
  verb'd to/from role, dated"), check each against the context blob the
  numeric checker already uses, flag misses the same way.
- **deterministic-fixable** — RECAP-only sourcing boundary crossed into a
  BRIEF. "Diesel hit a record price as Ukrainian strikes knocked Russian
  refineries offline and Moscow banned exports in response" appears in
  both the RECAP and the diesel BRIEF. The claim is genuinely grounded —
  verbatim in `news_snapshot` (CNBC via Finnhub, 2026-09-04 08:40 EDT) —
  so this is not a hallucination, and STEP 7's grouping of it with the
  ADBE fabrication as "two ungrounded non-numeric assertions" overstates
  it: it's grounded, just in the wrong section. `CLAUDE.md` states "Live
  news... used in RECAP only" as a hard layering rule; the BRIEFS section
  is supposed to derive from research PDFs. A deterministic check
  (BRIEFS-section sentences shouldn't token-match `news_snapshot`-only
  content that has no corresponding PDF source) would catch this class
  without needing a model judgment call.
- **deterministic-fixable, escalated** — `pulse-context/latest.json`
  overwritten-by-later-run staleness, now confirmed for a 4th day
  (08-31, 09-01, 09-02, 09-04; 09-03 unchecked — no `qc-headless` report
  exists for that day, a gap in the review series worth noting on its
  own). Today's dump: `dumped_at_utc 2026-09-04T17:43:47Z`, 23 PDFs,
  `last-24h` window, versus the actual run's `pdf_count: 53` and
  14:07–14:45Z window. Escalating past "RECAP prices unverifiable"
  (2026-09-02's framing): today it also blocked verification of the
  `discovery_audit.promoted` bank/PDF counts STEP 7's selection-review
  section is built on — the dump I read shows only 2 promoted Phase-B
  themes (`treasury bond buybacks` 4 banks/4 PDFs, matching STEP 7
  exactly, and `hormuz strait disruptions` 3 banks/3 PDFs, not the 5
  banks/7 PDFs STEP 7 cites), which is consistent with this being a
  smaller, later, differently-windowed run rather than STEP 7 being
  wrong — but it means a same-day-later QC pass literally cannot check
  the coverage-audit layer against this artifact. Repeating the standing
  recommendation with more urgency: either stop overwriting `latest.json`
  intraday, or write a run-scoped copy (`pulse-context/$QC_PULSE_TS.json`)
  at commit time the way every other artifact in this pipeline is already
  timestamped.
- **observation** — `draft_validate` shipped hard residuals for the first
  time in the 08-27→09-04 sample (`"hard residuals after 2 re-rolls --
  shipping with residuals recorded (budget spent)"`), and no file
  anywhere in `pulse-output/` enumerates what those residuals were — the
  gate trail records that they shipped, not what they are.  Separately,
  today's `lint` gate returned `SKIP_SCRUB` (clean) for the first time in
  the same sample, but STEP 7 already established (and I reproduced) that
  `_check_section_lengths` never executes on this artifact's `## 2.
  INSIGHTS & ALPHA` seam — so part of "clean" is "the check didn't run,"
  not purely "nothing was wrong." And because SCRUB was skipped, there is
  no `pre-scrub/` or `scrubbed/` snapshot for today; the only reason the
  EDIT-stage `_LEANS` rewrite (`$XLE, $SHEL` → `$USO`) was verifiable at
  all is that the adversarial gate happens to log its full prompt
  (including the internal `_LEANS` block) to
  `pulse-output/agent-io/$TS/adversarial-prompt.txt`. That's an
  incidental side effect of one gate's logging, not a designed audit
  trail — if adversarial logging ever changes, a SCRUB-skip day loses all
  visibility into what EDIT produced. Not filing a fix, since I don't
  know which of "persist EDIT output always" vs "persist residual notes
  only when non-empty" is the right shape — flagging so a session with
  routine context can pick one.
- **observation** — three consecutive days now (09-02, 09-03, 09-04)
  fully exhausting the adversarial repair budget (2/2), versus 0-of-2
  spent on 08-27 and 08-28. Not enough data to call this a trend with a
  cause, but worth tracking alongside STEP 7's per-run adversarial notes:
  if the rate of hard findings reaching the adversarial stage keeps
  climbing, the fix belongs upstream of adversarial (DRAFT/EDIT prompt),
  not in adding a third repair round.
