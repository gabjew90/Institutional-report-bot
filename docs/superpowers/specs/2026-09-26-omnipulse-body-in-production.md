# Omnipulse body in the production pulse (trial)

Status: approved by the owner 2026-09-26 ("Go"); built, switch off pending the dry run.

## What changes

The 10 AM ET pulse keeps its frame and swaps its middle. THE MAIN EVENT
and BRIEFS come from the Omnipulse (the claim-card pilot's editor output,
`pilot/shadow/<date>.clean.md` on `pilot-data`). RECAP, WHAT TO WATCH and
the TRADE BOARD still come from the production routine, because only it
has live prices, news, releases and the calendar. When no usable
Omnipulse exists, the routine runs exactly as it does today.

## Why

On the three most recent graded days the Omnipulse was at least as
faithful to its sources as production, and better on two: 87% vs 47%
(9/21), 80% vs 80% (9/23), 100% vs 73% (9/24), and it kept the source's
reasoning chain on every graded day where production kept it about half
the time. It already lands at 14:00-14:08 UTC, the pulse's own slot.

It is not a Gemini saving. The production pulse runs on the Claude
subscription. The trial frees subscription use instead: omnipulse days
skip the theme-adjudication sub-agents (up to eight). Retiring Gemini's
read of HIGH PDFs (~$6-7/month) is a separate step, because /ask's
research lookups and the calendar's conference rows read those analyses.

## Flow on a pulse day

A new routine step, **STEP 2.6 — Omnipulse gate**, after the press-time
check:

1. `scripts/omnipulse_body.py fetch --date <today ET>` polls `pilot-data`
   for `pilot/shadow/<date>.clean.md` and its `.meta.json`, up to 20
   minutes. It accepts the body only when:
   - the file is for today's date;
   - the meta shows `unread_source_files_at_edit == 0` and no
     `structural_problems`;
   - it has a `# headline`, one `## 2. THE MAIN EVENT` theme and at least
     two BRIEFS themes;
   - no citation markers (`[cN]`, `[dN]`) survived the clean step.
   On success it writes `/tmp/omnipulse_body.md` in production's format:
   every `###` theme in order under one `## 2. INSIGHTS & ALPHA` header
   (the first becomes THE MAIN EVENT downstream, as today) and the
   Omnipulse headline as the pulse's `#` line.
2. Exit 0 = omnipulse mode (`/tmp/pulse_mode.txt` = `omnipulse`); anything
   else = classic mode, and every later step behaves exactly as now.

In omnipulse mode:

| step | change |
|---|---|
| 3.5 adjudication | skipped |
| 4 DRAFT | runs with an appended block: the INSIGHTS body is supplied; write RECAP, WHAT TO WATCH and `## _LEANS` (desk-called trades from the supplied themes, MAIN EVENT first) and put `<<OMNIPULSE_BODY>>` under `## 2. INSIGHTS & ALPHA`. Code splices the body in. |
| 4.5 validate | `--omnipulse`: `duplicate-sibling-sections` and `contrarian-buried-in-appendix` do not apply (theme choice is the Omnipulse editor's). `leans-block-missing`, `main-event-lean-missing` and the RECAP checks still apply. |
| 5a STITCH | unchanged (cashtag scrub, ETF normalization) |
| 5b EDIT | runs for RECAP and WHAT TO WATCH; afterwards code re-splices the Omnipulse body, so EDIT cannot change it |
| 5.5 lint / 5.7 SCRUB | unchanged: voice fixes (em-dashes, banned phrases) may touch body sentences, never facts |
| 5.85 adversarial | checks RECAP and WHAT TO WATCH only. The body was already checked by the pilot's citation verifier: every figure cites a card whose quote was matched against the source PDF text. |
| 6 commit | frontmatter gains `body_source: omnipulse` or `classic` |

The bridge, the Discord post, the web fragment and the archive are
unchanged: they receive an ordinary pulse file.

## Switch and rollback

`ENABLED` in `scripts/omnipulse_body.py` (see Build notes): False is
today's routine. The routine clones the working branch every fire, so a
push flips the next 10 AM run.

## Known limits for the trial

- **Coverage is HIGH-priority PDFs only** (~19-60 documents a day).
  Production's MEDIUM research drops out of MAIN EVENT and BRIEFS.
  Adding MEDIUM to the readers costs subscription use; decide after the
  trial.
- **Missing days fall back.** The Omnipulse produced nothing on 9/22 and
  9/25 (no reader cards: readers idle on 9/22, Goldman's research
  missing from Dropbox on 9/25). Those days would post a classic pulse.
- **Timing.** The editor starts 13:55 UTC. A late Omnipulse delays the
  pulse by up to the 20-minute poll before classic mode takes over.
- **The pilot's scored evaluation never started** (DAY1 unset). Adopting
  now is an owner decision ahead of that evaluation.

## Trial and decision

Five market days from the first omnipulse-mode pulse. Watch: omnipulse
vs classic days, post time, lint and adversarial findings on RECAP/WATCH,
and room reaction. The pilot graders keep scoring the shadow file as
before. Decide then: keep, add MEDIUM coverage, or switch off.

## Build list

1. `scripts/omnipulse_body.py` (fetch, validate, convert, splice) with tests
   on the 9/17-9/24 Omnipulse files.
2. `pulse_draft_validate.py --omnipulse`.
3. Routine: STEP 2.6, the omnipulse branches in 3.5, 4, 5b and 5.85, the
   frontmatter field, the `OMNIPULSE_BODY` constant (ships `off`, flipped
   `on` after a dry run).
4. Dry run: run the routine's omnipulse path locally against Friday 9/25's
   context and the 9/24 Omnipulse, lint and validate the result, before
   the flag goes on.

## Build notes (2026-09-26)

- The switch is `ENABLED` in `scripts/omnipulse_body.py` rather than a
  routine constant: the driver reads it, so the model running the routine
  cannot misread it.
- The body is re-applied at the draft-validate gate (after DRAFT) and the
  lint gate (after EDIT, before SCRUB), so SCRUB can still fix wording.
- Preflight restores the saved body when a late pass broke a theme
  heading, instead of blocking: a blocked preflight means no pulse.
- Dry run on Friday 9/25's real draft and context with the 9/24
  Omnipulse: validator clean, lint 2 soft findings (a figure cited in two
  themes), no SCRUB needed, final validation clean, strip clean.
- Open: the Omnipulse writes 8-11 themes (1,500-1,850 words) against
  production's 3-6. Owner decision pending on a BRIEFS cap.
