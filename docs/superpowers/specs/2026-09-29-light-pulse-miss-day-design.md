# Light pulse on an Omnipulse miss day

Date: 2026-09-29. Owner decision: option (a), built now behind a switch, activated when the classic pulse is retired.

## Problem

The Omnipulse body (spec `2026-09-26-omnipulse-body-in-production.md`) has landed on time on every day it landed at all, and has been usable every day since 2026-09-17. It still went missing on 3 of the last 10 market days, each time from upstream (the Claude weekly limit, an empty PDF feed). Today a miss falls back to the classic pulse, which needs Gemini deep analysis of every HIGH and MEDIUM document, about $19 a month. Retiring that spend removes the fallback. The owner chose what a miss day publishes instead: RECAP and WHAT TO WATCH with a one-line note, no research body.

## Decision points already made

- Live only at retirement. Until then a miss day still runs the classic pulse.
- The routine waits 10 minutes for the Omnipulse (`DEFAULT_WAIT_S`), as today.
- The TRADE BOARD still renders when desk calls exist; it comes from the database, not the body.

## Design

### Switch and trigger

`scripts/omnipulse_body.py` gains `MISS_DAY = "classic"` beside `ENABLED`. Allowed values: `"classic"` (today's behavior) and `"light"`. Owner-only edit, flipped in the retirement commit.

The gate (`pulse_driver.py gate omnipulse`, routine STEP 2.4) runs unchanged until the Omnipulse is missing or unusable after the wait. Then:

- `MISS_DAY == "classic"`: `DECISION: CLASSIC`, exactly as now.
- `MISS_DAY == "light"`: the fetch writes the light body and a placeholder headline to the same body and headline files an Omnipulse would use, records `light_reason` in the driver state, and prints `DECISION: LIGHT -- <reason>`. The reason is the fetch's `LAST_FETCH_REASON` or the `problems()` list.

`OMNIPULSE_BODY=off` still forces classic for one process regardless of `MISS_DAY`, so smokes and test fires never produce a light pulse by accident.

The routine treats LIGHT like OMNIPULSE: skip STEP 3.5 adjudication, DRAFT writes RECAP, WATCH and `_LEANS` around the placeholder, the driver splices the body after DRAFT and after EDIT, exempts the theme-choice validators, keeps the adversarial check to RECAP and WATCH, and restores the body at preflight. Frontmatter `body_source: light`.

### The light body

The body file holds the `## 2. INSIGHTS & ALPHA` header and one paragraph, no `###` themes:

> No research body today. The morning's bank research was not ready in time. The market read above and the calendar below are current, and the full edition returns tomorrow.

The text lives as a constant in `omnipulse_body.py` (`LIGHT_BODY_NOTE`) so lint's voice rules can be checked against it once in a test. It carries no figures, so the fact checker has nothing to find in it.

The headline placeholder is empty: DRAFT writes the H1 from the live tape as it does on any day (the H1 is not locked on Omnipulse days either; only the INSIGHTS section is). `_LEANS` is written by DRAFT with no rows when nothing in the pulse proposes a trade; the validator's `leans-block-missing` check fires only when a MAIN EVENT proposes a trade, and a light pulse has no MAIN EVENT.

### Downstream tolerance for zero themes

Every consumer that reads the INSIGHTS section must accept a section with no `###` themes. Each gets a test with the light document.

| Consumer | Today | Light-pulse behavior |
|---|---|---|
| `report/pulse_sections.py :: split_main_event_briefs` | Splits the first theme into THE MAIN EVENT, the rest into BRIEFS | Already leaves a section with no `###` themes untouched, so the note stays under `## 2. INSIGHTS & ALPHA` and no MAIN EVENT or BRIEFS header is spun. Covered by a test, no code change expected |
| `report/formatter.py` | One embed per section, color by header keyword | The INSIGHTS header keeps its legacy blue; the note renders as one short embed between RECAP and TRADE BOARD |
| `github_bridge/jobs.py` post sanity check | Refuses an archive whose INSIGHTS or WATCH header is missing or empty | The light pulse has both headers with text, so it passes; covered by a test |
| `scripts/pulse_dashboard.py :: render_pulse_fragment` | Emits `.insights-body` with theme markup | Emits `.insights-body` holding the note paragraph, same class hooks (the dashboard repo's CSS is unchanged) |
| `report/pulse_sections.py :: parse_lean_block`, and the bridge (`github_bridge/jobs.py`, the `_LEANS` handling in the post job) | Parses `_LEANS` rows | Zero rows is a normal result, not an error |
| `scripts/pulse_draft_validate.py`, `pulse_lint.py` | Theme-count, section-length and theme-choice checks | Under `--omnipulse` (which LIGHT reuses) the theme-choice kinds are already exempt; section-length is soft and skips a section with no themes |
| `pulse_driver.py preflight` | Verifies the saved body is intact in final.md | Same check against the light body |

The bridge's post job reads the archive markdown; nothing there assumes a theme count beyond the splitter above.

### Ops visibility

A LIGHT decision sends one ops ping (`discord_bot/ops_alert.py`, key `pulse-light-<pulse filename>`, keyed per pulse file so a bridge retry does not page twice, 1 h dedupe) naming the reason. Frontmatter `body_source: light` lets `pulse-output/archive/` be counted for miss days. The QC review notes the reason.

### Out of scope

- Retiring deep analysis itself, re-pointing RECAP and WATCH to the pilot's cards, deleting the classic DRAFT path. Those are the retirement commit, gated on 10 consecutive market days with `body_source: omnipulse`. A light day whose reason is no new bank research since the last pulse is neutral: it neither counts toward the 10 nor breaks the run (owner, 2026-10-08; `scripts/omnipulse_streak.py`).
- Changing the 10-minute wait or the fetch routes.
- Any change to the dashboard repo.

## Testing

- `gate_omnipulse` with `MISS_DAY="light"` and a fetch returning none: `LIGHT`, body and headline files written, reason recorded. With `"classic"`: `CLASSIC`, files absent. With `OMNIPULSE_BODY=off`: `CLASSIC` under both.
- The light document (real 9/25 DRAFT with the light body spliced in) passes `pulse_draft_validate --omnipulse`, `pulse_lint` (zero hard), `gate_strip`, and `preflight`.
- `split_main_event_briefs`, `formatter`, `render_pulse_fragment` and `pulse_leans` each accept the light document and produce output containing the note and no MAIN EVENT or BRIEFS header.
- `LIGHT_BODY_NOTE` passes `compose_lint_patterns()`.
- Classic path byte-identical: with `MISS_DAY="classic"` the existing omnipulse driver tests pass unchanged.
- Routine markdown: STEP 2.4 documents `DECISION: LIGHT` (act as OMNIPULSE), the smokes' `OMNIPULSE_BODY=off` still yields classic.
