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
