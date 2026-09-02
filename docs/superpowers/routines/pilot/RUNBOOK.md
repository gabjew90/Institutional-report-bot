# Shadow pilot runbook

Spec: `docs/superpowers/specs/2026-08-20-claim-card-synthesis-redesign.md`
(frozen rubric, section 8). Plan: `docs/superpowers/plans/2026-08-28-shadow-pilot-implementation.md`.
Data: the `pilot-data` orphan branch, root `pilot/`. Production (pulse-data,
Discord, Railway DB) is never written by any pilot job.

## The daily chain (all UTC, Mon-Fri)

| when | job | writes |
|---|---|---|
| worker, on HIGH analysis | `github_bridge/pilot_publish.py` (gated `PILOT_PUBLISH_ENABLED`) | `source-text/<date>/<id>__<slug>.txt` + `.meta.json` |
| 09:00-14:00 hourly, 13:15, 01/05/17/21 | `pilot-readers.yml` | `cards/<date>/<id>.json`, `ops/<date>.json` |
| 13:55 | `pilot-editor.yml` | `shadow/<date>.md`, `.clean.md`, `.meta.json` |
| 17:00 | `pilot-graders.yml` | `grades/<date>/<dim>-<artifact>-<a|b>.json`, `scoreboard.md` |
| dispatch only | `pilot-grader-gate.yml` | `grader-gate/<ts>/` |

## Scheduling: the worker's clock, not GitHub's cron

Shakedown day 1 (2026-09-02) showed GitHub's cron dropping most of our
schedules: the heartbeat fired twice in a day instead of every 30
minutes, the readers' 09-14 UTC hourly window fired once, and the
13:55 editor never fired. `github_bridge/workflow_dispatch.py` POSTs
`workflow_dispatch` for the readers, editor and graders from the
worker's APScheduler at the declared times. It is gated on
`PILOT_DISPATCH_ENABLED` and needs the worker's `GITHUB_TOKEN` to carry
**Actions: read and write** (the current fine-grained PAT answers 403,
which pages ops hourly rather than failing silently). The workflow
`schedule:` blocks stay as a fallback; each workflow's `concurrency`
group makes a double fire harmless.

Until the token is extended, run a missed step locally with the same
scripts the workflow uses (see the 2026-09-02 NOTES entry for the exact
commands); the artifacts are identical.

## Before day 1 (plan section 4)

1. Set `PILOT_PUBLISH_ENABLED=true` on the Railway worker. Source text
   starts landing on `pilot-data` within the bridge cadence.
2. Two shakedown days. Check: reader failure rate under 10%
   (`ops/<date>.json`), zero workflow auth or commit failures, the
   editor produced `shadow/<date>.md` with a `_LEANS` block and
   citation failures near zero after its one re-ask, the scoreboard
   renders.
3. Run `pilot-grader-gate` from the Actions tab. Every dimension must
   read `separates`. TOO WEAK means the prompt lets a distorted brief
   or a misattributed sentence through; fix the prompt, re-run.
4. Record subscription headroom on the heaviest shakedown day. Shed
   order if it runs thin: readers tier down first, then the editor,
   graders last.
5. Once during shakedown, run the ledger and editor over a merged
   three-day card set (`pilot_editor_pack.py --days 3`) and note the
   grouping behaviour. Uncounted (plan 4.3).
6. Commit the file `pilot/DAY1` on `pilot-data` containing the first
   counted date. From that day: no prompt edits, no model-string
   edits. Either restarts the clock.

## Reading the scoreboard

`pilot/scoreboard.md` on `pilot-data`. Days before DAY1 show as
shakedown and count for nothing. A row with a `tiebreak` entry has the
two graders disagreeing on that metric: the owner reads both grade
files and records the call in the day's grade directory as
`<dim>-tiebreak.json` (same shape as an agent grade, agent `owner`).

Decision rule after 10 counted days (frozen): expand to MEDIUM only if
metrics 1, 2, 2a and 3 all pass; kill if 2, 2a or 3 regress; anything
else buys exactly one reader-prompt iteration, then re-run, then
decide. No third iteration.

## Deviations recorded

- Reader tiers within HIGH (opus for GS/MS/JPM/Citi/DB/BofA, sonnet
  for the rest) are the plan's invention, not the spec's; metrics 2
  and 2a are therefore split by tier on the scoreboard.
- The shadow pulse omits RECAP and WHAT TO WATCH (no live data on the
  runner). Graders compare only THE MAIN EVENT and BRIEFS, stated in
  every grader prompt.
- Grader fixtures live in this repo under
  `docs/superpowers/routines/pilot/grader-fixtures/` (frozen with the
  prompts) rather than on the data branch; the gate copies its verdict
  to `pilot-data` under `grader-gate/`.
- Metric 5 comes from `ops/<date>.json`, written by the readers
  workflow, not from Actions run metadata.
