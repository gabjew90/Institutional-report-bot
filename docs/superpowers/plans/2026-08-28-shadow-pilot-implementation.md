# Shadow pilot — implementation plan

**Derived from:** `docs/superpowers/specs/2026-08-20-claim-card-synthesis-redesign.md`
(design + frozen rubric; this plan decides the HOW and changes none of
the WHAT). **Status:** plan for review, then build.

## 0. The one fork: what rail do the readers run on

The spec says "opus_bridge readers" — written before the headless
GitHub Actions rail existed (daily-qc, 2026-08-27). Decision needed:

| | Claude.ai routine rail | **GitHub Actions headless (recommended)** |
|---|---|---|
| Scheduling | routine crons, opaque | workflow cron, versioned, PR-reviewable |
| Commit transport | the documented PAT-403 pain | native git push (proven this week) |
| Logs/determinism | app transcripts | Actions logs, exit codes, retry semantics |
| Billing | Max subscription | Max subscription (same) |
| Auth lifecycle | none | setup-token ~1yr, hard-fails loudly (proven) |
| Runner minutes | n/a | ~10 short runs/day; est. 60–90 min/day. **Over the 2000/mo free tier — expect Actions billing or plan headroom; flagged, not hidden** |

Recommendation: **Actions**, for the same reasons the ask/pulse QC
went there. Both rails bill the same subscription; Actions is the one
whose failure modes we have already hardened.

Consequence: readers cannot touch the Railway volume or SQLite. The
worker must PUBLISH what readers need, and pilot state lives on the
`pulse-data` branch as files, not in the DB. The spec's "cards
accumulate in the DB" becomes "cards accumulate in a committed file
tree" — same intent (durable, queryable, ledger-buildable), different
substrate, and it keeps the pilot fully decoupled from production
(nothing to migrate, everything prunable after the verdict).

## 1. Data flow

```
worker (Railway)                         pulse-data branch
  HIGH analysis completes --publish-->  pilot/source-text/<date>/<id>__<slug>.txt
                                        (extracted full text + a small .meta.json:
                                         source, title, priority, published_at)

GitHub Actions "pilot-readers" (upload-weighted cron)
  for each unread source-text file:
    reader agent (tier by source) -->   pilot/cards/<date>/<id>.json
                                        { brief, cards[], reader_tier,
                                          verify: {matched, dropped, reasked} }
  verification inline: every card anchor checked against the source
  text via ai_analysis/anchor_check.normalize; one re-ask in-session;
  failed cards DROPPED and counted (spec section 3)

GitHub Actions "pilot-shadow-editor" (13:55 UTC Mon-Fri)
  scripts/pilot_ledger.py over pilot/cards/** --> /tmp ledger
  shadow editor agent -->                pilot/shadow/<date>.md
                                        (one-write, card/brief citations)

GitHub Actions "pilot-graders" (17:00 UTC Mon-Fri)
  two fresh agents per dimension, frozen prompts -->
                                        pilot/grades/<date>/<dim>-<a|b>.json
  scripts/pilot_scoreboard.py -->       pilot/scoreboard.md  (running tally;
                                        day-10 verdict is a READ, not a dig)
```

Production is untouched end to end. The pilot tree is deleted or
archived wholesale after the decision.

## 2. Build pieces, in dependency order

1. **Worker publish job** (`github_bridge/` addition): on HIGH deep-
   analysis completion, commit extracted full text + meta to
   `pilot/source-text/`. Size guard (cap ~400KB/file), dedupe by
   pdf_file_id, batched with the existing bridge commit cadence.
   Volume: ~19 HIGH/day observed → ~1.5–2MB/day text. Gated on
   `PILOT_PUBLISH_ENABLED` env (default off until day −2).
2. **Reader prompt** (`docs/superpowers/routines/pilot/reader.md`):
   the section-2 artifact contract verbatim — brief 100–200 words
   preserving the causal chain; cards with the full field set; anchors
   copied character-for-character (the step-2 prompt language, proven
   at 97.3%). Output STRICT JSON.
3. **Reader workflow** (`.github/workflows/pilot-readers.yml`):
   cron table implementing section 2.1 (hourly 09–14 UTC ≈ 5–10 AM ET,
   a 13:15 UTC run ≈ 9:15 ET, every 4h otherwise). Each run: list
   unread source-text files (no matching cards file), dispatch one
   reader per file, tier by source (**opus**: GS/MS/JPM/Citi/DB/BofA;
   **sonnet**: everything else — mirrors the multimodal trigger's
   tier line), verify anchors with a deterministic script
   (`scripts/pilot_verify_cards.py`, reusing anchor_check), commit.
   Skip-fast when nothing is unread (~1 min run).
4. **Ledger builder** (`scripts/pilot_ledger.py`, pure Python, unit-
   tested): section-4 semantics — bank-dedup, hard keys on
   instruments + figures, soft topic labels, concentration stats,
   grouping filters nothing. Deterministic; the shadow editor
   consumes its output.
5. **Shadow editor prompt + workflow**: one-write synthesis from
   ledger + briefs with the spec's citation discipline, current pulse
   format (MAIN EVENT / BRIEFS / WATCH), `## _LEANS` included. Writes
   to `pilot/shadow/<date>.md`. Never touches Discord.
6. **Grader prompts, one per dimension** (metrics 1, 2, 2a, 3, 4 —
   metric 5 is arithmetic from workflow logs): frozen text per
   section 8, two fresh agents (a/b), the materiality test verbatim
   in 2a's prompt. **Freeze = committed before day 1 and untouched
   for 10 days**; a grader-prompt edit mid-pilot restarts the clock.
7. **Scoreboard** (`scripts/pilot_scoreboard.py`): aggregates grades
   into `pilot/scoreboard.md` — per-day per-metric results, running
   pass/fail against the frozen thresholds, disagreements awaiting
   owner tiebreak flagged inline.
8. **Docs**: pilot runbook section in the spec dir; daily-qc's pulse
   judge told to IGNORE the pilot tree (it grades production only).

## 3. What the spec leaves open — decisions this plan makes

- **Reader tiers** = model tiers (opus/sonnet) split by source bank,
  mirroring the existing top-bank line. The spot-audit already
  samples across tiers by design.
- **Spot-audit (2a) rides the graders workflow**, not daily-qc:
  same-day grading, 3–5 briefs weighted toward what the shadow MAIN
  EVENT cited, tier-stratified.
- **Production fidelity arm** (metric 2's comparison): the graders
  workflow samples 15 sentences from that day's PRODUCTION archive
  pulse too, same procedure, same frozen prompt.
- **File tree over DB** (deviation from spec wording, intent
  preserved — recorded here deliberately).
- **Shadow editor timing 13:55 UTC**: after the 13:15 catch-up read,
  before production's 14:00 fire — same information window as
  production, which is what metrics 2 and 3 compare.

## 4. Readiness gate before day 1

Two shakedown days (day −2, day −1) with everything running and
NOTHING counted: readers on real HIGH flow, shadow pulses written,
graders firing, scoreboard rendering. Day 1 is declared only when a
checklist passes: reader failure rate <10%, zero workflow auth/commit
failures, ledger builds from real cards without manual repair,
scoreboard renders, grader prompts frozen-tagged. Shakedown findings
fix the machinery; rubric and prompts stay frozen from day 1.

## 5. Estimates

- Build: ~2 sessions (pieces 1–4, then 5–8).
- Shakedown: 2 trading days. Pilot: 10 trading days.
- Reader load: ~19 HIGH/day × 1 agent ≈ 20 subscription-billed agent
  runs/day + shadow editor + ~10 grader runs. Actions minutes ~60–90
  /day (billing note above).
- pulse-data growth: ~2–3MB/day, pruned after the verdict.

## 6. Out of scope

MEDIUM expansion, deletion of clustering/DRAFT machinery, prompt
iteration (one allowed by the decision rule, AFTER the 10 days),
anything touching the production pulse path.
