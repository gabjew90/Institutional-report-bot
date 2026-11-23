# Shadow pilot runbook

Spec: `docs/superpowers/specs/2026-08-20-claim-card-synthesis-redesign.md`
(frozen rubric, section 8). Plan: `docs/superpowers/plans/2026-08-28-shadow-pilot-implementation.md`.
Data: the `pilot-data` orphan branch, root `pilot/`. Production (pulse-data,
Discord, Railway DB) is never written by any pilot job.

## The daily chain (all UTC, Mon-Fri)

| when | job | writes |
|---|---|---|
| worker, on HIGH analysis | `github_bridge/pilot_publish.py` (gated `PILOT_PUBLISH_ENABLED`) | `source-text/<date>/<id>__<slug>.txt` + `.meta.json` |
| worker, on MEDIUM analysis | same publisher | `source-text-all/<date>/...` (the Claude analysis lane's MEDIUM notes; the readers never read it) |
| 09:00-14:00 hourly, 13:15, 01/05/17/21 | `pilot-readers.yml` | `cards/<date>/<id>.json`, `ops/<date>.json` |
| 13:55 | `pilot-editor.yml` | `shadow/<date>.md`, `.clean.md`, `.meta.json`, `scoreboard.md` |

## Scheduling: the worker's clock, not GitHub's cron

Shakedown day 1 (2026-09-02) showed GitHub's cron dropping most of our
schedules: the heartbeat fired twice in a day instead of every 30
minutes, the readers' 09-14 UTC hourly window fired once, and the
13:55 editor never fired. `github_bridge/workflow_dispatch.py` POSTs
`workflow_dispatch` for the readers and the editor from the
worker's APScheduler at the declared times. It is gated on
`PILOT_DISPATCH_ENABLED` and needs the worker's `GITHUB_TOKEN` to carry
**Actions: read and write** (extended 2026-09-02; a 403 pages ops hourly
rather than failing silently). Only the readers keep a `schedule:`
fallback, where a late fire is harmless. The editor has none: on
2026-09-03 and 09-04 GitHub's cron fired it three hours late and the
late run overwrote the 13:55 shadow pulse with one written from a
different card set, after production had published. A concurrency group only protects
against two runs at once, not against a second run hours later.

A missed step can be run locally with the same scripts the workflow
uses (see the 2026-09-02 NOTES entry for the exact commands); the
artifacts are identical.

## Model grading: retired 2026-11-23

Until 2026-11-23 a graders workflow ran at 17:00 UTC: two agents per
dimension graded the day's edition and the production pulse against the
source text, with a third on a disagreement, and a ten-day verdict was to
decide whether the design shipped. Day 1 was never set (`pilot/DAY1` was
never committed), the Omnipulse body went into production on 2026-09-26
by the owner's decision, and the grades arrived three hours after each
pulse was published. The owner retired it. The workflows, their scripts,
the grader prompts and the seeded fixtures are deleted from this
repository (they are in its history), and the grades through 2026-11-20
stay under `grades/` on `pilot-data`.

What checks an edition now runs before it is published, in code:

- `scripts/pilot_verify_citations.py`: every figure in a sentence must be
  in a card that sentence cites, and a bank a sentence names must be one
  its citations (or, for a sentence with none, its theme) carry. The
  editor gets one repair pass on the failures, and what remains is
  recorded in `shadow/<date>.meta.json`.
- `scripts/omnipulse_body.py :: problems`: production does not take the
  body of an edition written with source files unread, with a structural
  problem, or with a citation marker left in its text.

## Reading the scoreboard

`pilot/scoreboard.md` on `pilot-data`, rendered by the editor workflow
after each edition (`scripts/pilot_scoreboard.py`). One row per edition:
citations, failed sentences, source files unread at edit (any is a VOID
day), documents the readers gave up on, the share of citations from the
first and last fifth of the pack (flagged above 70%), and whether the
readers ran clean (failure rate under 10%, no read inside the pulse
window, from `ops/<date>.json`).

## Deviations recorded

- Reader tiers within HIGH (GS/MS/JPM/Citi/DB/BofA against the rest)
  are the plan's invention, not the spec's. Both run Sonnet since
  2026-09-16.
- The shadow pulse omits RECAP and WHAT TO WATCH (no live data on the
  runner).
- The readers' record comes from `ops/<date>.json`, written by the
  readers workflow, not from Actions run metadata.
- A document the readers give up on after three attempts is a recorded
  coverage gap (`read-failures/`, `given_up_at_edit`), not an unread
  file; the void rule counts readable-unread only.
