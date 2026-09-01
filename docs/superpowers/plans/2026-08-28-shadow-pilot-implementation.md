# Shadow pilot — implementation plan

**Derived from:** `docs/superpowers/specs/2026-08-20-claim-card-synthesis-redesign.md`
(design + frozen rubric; this plan decides the HOW and changes none of
the WHAT).

**Status:** APPROVED to build 2026-08-31 with six required changes,
all incorporated below (owner review). **Destination decided
2026-09-01 (option B): the `pilot-data` ORPHAN branch** — production
artifacts cannot be touched even by accident, the pulse archive's
history stays readable, and cleanup after the verdict is deleting one
branch. Piece 1 is UNHELD and built.

## 0. The one fork: what rail do the readers run on

The spec says "opus_bridge readers" — written before the headless
GitHub Actions rail existed (daily-qc, 2026-08-27). Decision:

| | Claude.ai routine rail | **GitHub Actions headless (chosen)** |
|---|---|---|
| Scheduling | routine crons, opaque | workflow cron, versioned, PR-reviewable |
| Commit transport | the documented PAT-403 pain | native git push (proven this week) |
| Logs/determinism | app transcripts | Actions logs, exit codes, retry semantics |
| Billing | Max subscription | Max subscription (same) |
| Auth lifecycle | none | setup-token ~1yr, hard-fails loudly (proven) |
| Runner minutes | n/a | ~10 short runs/day; est. 60–90 min/day. **Over the 2000/mo free tier — expect Actions billing; flagged, not hidden** |

Consequence: readers cannot touch the Railway volume or SQLite. The
worker must PUBLISH what readers need, and pilot state lives as a
committed file tree rather than DB rows — same intent (durable,
queryable, ledger-buildable), different substrate, and the pilot stays
fully decoupled from production (nothing to migrate, everything
prunable after the verdict).

**Destination (decided 2026-09-01, option B):** the `pilot-data`
orphan branch, `pilot/` root. Both live in `scripts/pilot_config.py`
(`PILOT_BRANCH`, `PILOT_ROOT`), env-overridable, so a relocation is
still a one-value change.

## 1. Data flow

```
worker (Railway)  [PIECE 1 — HELD]        <PILOT_ROOT>
  HIGH analysis completes --publish-->    source-text/<date>/<id>__<slug>.txt
                                          + <id>.meta.json (source, title,
                                            priority, published_at)

Actions "pilot-readers" (upload-weighted cron)
  for each unread source-text file:
    reader agent (tier by source) -->     cards/<date>/<id>.json
                                          { brief, cards[], reader_tier,
                                            provenance{model, model_version,
                                              prompt_sha}, verify{...} }
  verification inline: every card anchor checked against the source
  text via ai_analysis/anchor_check.normalize; one re-ask in-session;
  failed cards DROPPED and counted (spec section 3)

Actions "pilot-shadow-editor" (13:55 UTC Mon-Fri)
  scripts/pilot_ledger.py over cards/** --> /tmp ledger
  shadow editor agent -->                 shadow/<date>.md
                                          + shadow/<date>.meta.json
                                            (unread_source_files_at_edit,
                                             provenance)

Actions "pilot-graders" (17:00 UTC Mon-Fri)
  two fresh agents per dimension, frozen prompts -->
                                          grades/<date>/<dim>-<a|b>.json
  scripts/pilot_scoreboard.py -->         scoreboard.md
```

Production is untouched end to end.

## 2. Build pieces, in dependency order

1. **Worker publish job** — BUILT 2026-09-01. On HIGH deep-analysis completion, publish extracted
   full text + meta. Size guard (~400KB/file), dedupe by pdf_file_id,
   batched with the existing bridge cadence. ~19 HIGH/day observed →
   ~1.5–2MB/day. Gated on `PILOT_PUBLISH_ENABLED` (default off until
   day −2).
2. **Reader prompt** (`docs/superpowers/routines/pilot/reader.md`):
   the section-2 artifact contract verbatim — brief 100–200 words
   preserving the causal chain; cards with the full field set; anchors
   copied character-for-character (the step-2 language, proven at
   97.3%). STRICT JSON out.
3. **Reader workflow** (`.github/workflows/pilot-readers.yml`): crons
   per section 2.1 (hourly 09–14 UTC ≈ 5–10 AM ET, a 13:15 UTC run ≈
   9:15 ET, every 4h otherwise). Per run: list unread source-text
   files, dispatch one reader each, tier by source, verify anchors
   deterministically (`scripts/pilot_verify_cards.py`, reusing
   anchor_check), commit. Skip-fast when nothing is unread.
4. **Ledger builder** (`scripts/pilot_ledger.py`, pure Python,
   unit-tested): section-4 semantics — bank-dedup, hard keys on
   instruments + figures, soft topic labels, concentration stats,
   grouping filters nothing.
5. **Shadow editor prompt + workflow**: one-write synthesis from
   ledger + briefs with the spec's citation discipline, current pulse
   format minus RECAP, `## _LEANS` included. Records
   `unread_source_files_at_edit`. Never touches Discord.
6. **Grader prompts, one per dimension** (metrics 1, 2, 2a, 3, 4;
   metric 5 is arithmetic from workflow logs): frozen text per
   section 8, two fresh agents (a/b), materiality test verbatim in
   2a's prompt, shared-sections scope stated (see §3.6). **Freeze =
   committed before day 1 and untouched for 10 days.**
7. **Scoreboard** (`scripts/pilot_scoreboard.py`): per-day per-metric
   results, running pass/fail against frozen thresholds, metrics 2
   and 2a **split by reader tier**, `unread_source_files_at_edit`
   beside each day, disagreements awaiting owner tiebreak flagged.
8. **Docs**: pilot runbook; daily-qc's pulse judge told to IGNORE the
   pilot tree (it grades production only).

## 3. Decisions and recorded deviations

**3.1 File tree over DB** — deviation from spec wording, intent
preserved (see §0).

**3.2 Within-HIGH reader tiers — THIS PLAN'S INVENTION, recorded as a
deviation.** The spec's tiering was HIGH=Opus with cheaper readers for
MEDIUM. This pilot is HIGH-only, so opus-for-top-banks (GS/MS/JPM/
Citi/DB/BofA) and sonnet-for-the-rest is a quota call this plan
introduces, not the spec's design. Consequence for the verdict: the
scoreboard splits metrics 2 and 2a by reader tier, because fidelity
failing specifically on sonnet briefs is a MODEL-TIER verdict, not an
architecture verdict, and the day-10 read must be able to tell which
one it got.

**3.3 Spot-audit (2a) rides the graders workflow**, not daily-qc:
same-day, 3–5 briefs weighted toward what the shadow MAIN EVENT
cited, tier-stratified.

**3.4 Production fidelity arm**: the graders workflow samples 15
sentences from that day's PRODUCTION archive pulse too, same
procedure, same frozen prompt.

**3.5 Shadow editor at 13:55 UTC** — after the 13:15 catch-up read,
before production's 14:00 fire, so both arms see the same information
window.

**3.6 Coverage parity is measured, and the RECAP exclusion is stated
in the frozen prompts.** The shadow edits at 13:55 on whatever cards
exist; production's 14:00 dump includes everything analyzed by then.
A late PDF production covered and the shadow missed is a COVERAGE
artifact, not a quality gap. So: the editor records
`unread_source_files_at_edit` daily and the scoreboard shows it beside
that day's grades. And because the shadow format correctly omits
RECAP (it has no live snapshot), every grader prompt states that
comparison covers **only the sections both artifacts contain** —
frozen before day 1, or metric 3 penalizes the shadow for a section it
was never asked to write.

**3.7 Model strings are PINNED, provenance is recorded, and a model
change restarts the clock.** `opus`/`sonnet` are unpinned aliases —
the same shape that produced two retracted findings in this repo, and
ten days is long enough for an alias to move server-side. Every
workflow passes the most specific model ID available
(`claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5-20251001`),
and every cards file and grade file records `provenance`:
`{model_requested, model_version_returned, prompt_sha}` — the
returned version because a pinned request string is a request, not a
guarantee (the harness learned this exact lesson). Freeze rule,
symmetric with the prompt freeze: **a model-string change mid-pilot
restarts the clock**, same as a grader-prompt edit. Without it, day 6
splits the pilot into two incomparable halves invisibly.

## 4. Readiness gate before day 1

Two shakedown days (day −2, day −1), everything running, NOTHING
counted toward the rubric.

**4.1 Machinery checklist:** reader failure rate <10%, zero workflow
auth/commit failures, ledger builds from real cards without manual
repair, scoreboard renders, prompts and model strings frozen-tagged.

**4.2 GRADER SEPARATION — the gate that matters most.** Two shakedown
days grading only real artifacts cannot tell a lenient grader from a
good pilot; ten days of verdicts would then rest on prompts never
shown to distinguish good from bad. This is STANDING RULE 4 (a green
suite measures the assertions and nothing else) at pilot scale.

Seed into the shakedown grading runs, committed under
`<PILOT_ROOT>/grader-fixtures/` as permanent known-bad artifacts:
- **one deliberately distorted brief** — causal chain altered, figures
  left intact (so it fails 2a on materiality, not on arithmetic)
- **one bad sentence set** — containing an unsupported claim and a
  misattribution, for metrics 2 and 3

**Every grader dimension must FAIL its seeded artifact and PASS the
clean counterpart. A grader that passes both is TOO WEAK.** Day 1 does
not start until every dimension separates its pair. The fixtures stay
committed with the pilot tree and are re-run whenever a grader prompt
changes.

**4.3 Uncounted stress datapoint:** once during shakedown, run the
ledger builder and shadow editor over a MERGED three-day card set
(~60 documents) — grouping behaviour at load, no rubric contact, no
grading. Records a number; changes no verdict.

Shakedown findings fix machinery; rubric, prompts, and model strings
are frozen from day 1.

## 5. Estimates, quota, and the shed order

- Build: ~2 sessions (pieces 2–4, then 5–8; piece 1 when unheld).
- Shakedown 2 trading days; pilot 10 trading days.
- Load: ~19 HIGH/day → ~20 reader runs + 1 editor + ~10 grader runs
  ≈ **30 agent runs/day**, on the SAME Max subscription already
  running the production pulse and both QC workflows. The documented
  failure mode is quota exhaustion on the heaviest news day.
- **Shakedown must record actual subscription headroom**, not just
  failure rates. If headroom runs thin, the shed order is:
  **readers tier down first** (opus → sonnet for marginal sources),
  then the stress/extra runs, then the shadow editor; **graders shed
  LAST** — losing grading loses the day's verdict entirely, while a
  tiered-down reader is a recorded, attributable degradation.
- Actions minutes ~60–90/day (billing note in §0).
- Tree growth ~2–3MB/day, pruned after the verdict.

## 6. Verdict scope limit (write it now, not at day 10)

~19 HIGH PDFs/day is **the lightest month on record**. A passing
fragmentation number therefore certifies the architecture **at light
corpus load only**. The verdict template states this scope limit
explicitly, and the §4.3 stress datapoint is the single cheap
observation against it — an indication, not a certification.

## 7. Out of scope

MEDIUM expansion, deletion of clustering/DRAFT machinery, prompt
iteration (one allowed by the decision rule, AFTER the 10 days),
anything touching the production pulse path.
