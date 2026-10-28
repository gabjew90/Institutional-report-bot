# Economic print alerts: deterministic verdict, templated takeaway, mobile layout

Date: 2026-10-28. Status: approved by the owner 2026-10-28, built the same day. Replaces the body layout and the Quick Takeaway of `2026-09-30-print-embed-redesign-design.md`. Fetching, polling, arming, the ledger and the ops pings in `report/print_watch.py` are unchanged.

## What this solves

The alert makes one judgment in seconds with nobody checking it: the verdict and what it means for the Fed. The numbers are right or wrong in code. The verdict is the only place the bot can be wrong while every number is right, and a verdict that is wrong even occasionally teaches readers to skip it. So every judgment the alert makes is a written rule, decided in code, and the alert posts as soon as the agency's number is read.

Readers: a trader who needs the numbers the market trades on at once, and a newer reader who needs two or three sentences on what they mean. Both read on a phone, mostly from the lock screen first.

## Owner decisions this spec records

- No model on the release path. The verdict, the takeaway and every number come from code. The Gemini Quick Takeaway (`report/print_takeaway.py`) is retired.
- "Hawkish" and "dovish" are used as is (owner, 2026-10-28). Basis points are not: "a quarter-point hike".
- The title follows core inflation, the number the market trades. A headline difference is stated on its own line and in the takeaway.
- Payrolls within 50K of forecast count as in line (default; the agency's own margin of error on the monthly change is roughly ±130K, the market reacts to misses of about 50K).
- The Fed sentence depends only on what the Fed has done, read from the bot's own FOMC ledger. Nothing is updated by hand and nothing goes stale.
- One alert per release. No separate flash and detail messages, no later edits.

## Pipeline

```
agency data + forecast feed + ledger
  -> normalize    report/print_data.py      ReleaseData: reported, unrounded, consensus, revised, derived values
  -> signals      report/print_verdict.py   one signal per input: +1 / 0 / -1 / None
  -> verdict      report/print_verdict.py   truth tables below -> title text and direction
  -> takeaway     report/print_templates.py fixed sentences filled with facts
  -> render       report/print_render.py    message text + embed description + footer
  -> post         report/print_watch.py     unchanged _post, mark_posted (ledger gains signals and verdict)
```

Each stage is a pure function of the one before it, so every stage is testable without the network. `print_watch.py` keeps `ReleaseSpec`, `compute`, the fetchers and the job loop; `build_rows`, `render_release`, `_takeaway_lines`, `fomc_lines` and `_fomc_row` move into the new modules or are deleted.

## The message

- **Message text** (the lock-screen line): `@everyone Sep CPI: Hotter than expected`. The short month and the release name, then the verdict. Without the @everyone setting, the same line without the mention.
- **Embed**: no title. The description is the primary block, a blank line, the secondary block, a blank line, `Takeaway: ...`.
- **Footer**: `BLS · forecast: ForexFactory · released 8:30 AM ET`. FOMC: `Federal Reserve statement · released 2:00 PM ET`.

Example (numbers from the Oct 14 post; the unrounded figure is illustrative):

```
@everyone Sep CPI: Hotter than expected

Core CPI MoM +0.3% vs +0.2% expected (+0.31% unrounded)
Core CPI YoY 2.6% vs 2.5% expected
Headline CPI MoM +0.6% vs +0.6% expected
Headline CPI YoY 3.7% vs 3.6% expected
Core CPI 3M 3.4%

Shelter +0.4%
Food 0.0%
Energy +5.2%

Takeaway: Core inflation rose more than expected. Shelter rose 0.4% and energy 5.2% on the month. That leans hawkish and adds to the case for another hike, and reports like this tend to lift Treasury yields.
```

## Formatting rules

- One metric per line. No column alignment with spaces (Discord's proportional font breaks it), no tables, no code blocks, no pipes, arrows, emoji, or all-caps headings.
- `vs` between actual and consensus, then `expected`. A metric with a consensus is never shown without it.
- MoM, YoY and 3M (three-month annualized). K for thousands of jobs.
- Signs: changes and growth rates carry a sign (`+0.3%`, `-0.1%`, `+142K`); levels do not (`4.2%` unemployment, `4.1%` saving rate, `62.6%` participation). A y/y inflation rate is shown without a sign, as the agencies headline it.
- Precision: the agency's published precision. The unrounded value appears only on the lead line, as `(+0.31% unrounded)`.
- Revisions: `August revised to 180K from 190K`.
- Rate ranges: `3.75% to 4.00%`. Moves: `a quarter-point hike`, never basis points.
- No em-dashes or semicolons anywhere in the alert. Title separator is a colon.
- Data lines stay under 60 characters; a test enforces it.

## Precision and comparison

The verdict compares at the precision both sides share. The forecast feed gives one decimal, so a published +0.3% against +0.3% expected is in line even when the unrounded value is +0.34%. The unrounded value is context, never evidence of a beat. Unrounded values come from the agency's index levels (CPI, PPI, PCE) or levels (unemployment from the unemployed count and the labor force), never from a rounded display value. The 3M rate is computed from index levels, as today.

## Signals

A signal is +1, 0, -1 or None (no consensus or no value).

| Signal | +1 | -1 | 0 |
|---|---|---|---|
| Core inflation (CPI, PCE, PPI) | core MoM above consensus | below | equal |
| Headline inflation | headline MoM above consensus | below | equal |
| Payrolls | actual more than 50K above consensus | more than 50K below | within 50K, or cancelled by revisions (below) |
| Unemployment | below consensus (stronger) | above | equal |
| Wages | avg hourly earnings MoM above consensus | below | equal |
| FOMC decision | new upper bound above expected | below | equal |
| FOMC projections (projection meetings only) | median rate for next year above the last projection meeting's | below | equal |
| FOMC dissents | more dissents for a higher rate than at the last meeting (net of dissents for a lower one) | more for a lower rate | no change |

Core inflation falls back to core YoY when core MoM has no consensus (PCE's feed lists only core MoM, so this rarely applies). Payroll revisions: the payrolls signal becomes 0 when the two-month net revision has the opposite sign to the surprise and is larger than it. A unanimous vote in the ledger counts as zero dissents for the comparison.

## Verdicts

### CPI, PCE, PPI

| Core | Headline | Title |
|---|---|---|
| +1 | +1, 0 or None | Hotter than expected |
| -1 | -1, 0 or None | Cooler than expected |
| +1 | -1 | Mixed |
| -1 | +1 | Mixed |
| 0 | any | In line with expectations |
| None | any | Released, no forecast to compare |

Direction for the takeaway: hotter = hawkish, cooler = dovish, mixed and in line = none.

### Jobs

Count the three signals (payrolls, unemployment, wages), ignoring None.

| Signals | Title |
|---|---|
| at least one +1 and at least one -1 | Mixed |
| two or three +1, no -1 | Stronger than expected |
| two or three -1, no +1 | Weaker than expected |
| exactly one +1, the rest 0 | Slightly stronger than expected |
| exactly one -1, the rest 0 | Slightly weaker than expected |
| all 0 | In line with expectations |
| payrolls None | Released, no forecast to compare |

Direction: stronger = hawkish, weaker = dovish, slightly = the same direction, mixed and in line = none.

### FOMC

The action word is hike, hold or cut. The decision decides first, then the projections, then the dissents. The changed statement wording is shown but never decides the verdict: code cannot judge its tone reliably.

| Decision | Projections, else dissents | Title |
|---|---|---|
| +1 | any | Surprise {action}, hawkish |
| -1 | any | Surprise {action}, dovish |
| 0 | +1 | Hawkish {action} |
| 0 | -1 | Dovish {action} |
| 0 | projections and dissents point opposite ways | {Action}, mixed signals |
| 0 | 0 | {Action} as expected |
| None (no forecast) | dissents +1 or -1 | Hawkish {action} or Dovish {action} |
| None (no forecast) | dissents 0 | {Action} |

Today's decision under these rules: hold, expected hold (0), not a projection meeting, two dissents for a hike against none in September (+1): `Oct FOMC: Hawkish hold`.

## Policy state

Derived in code from the FOMC ledger, never set by hand:

- `last_action`: hike, hold or cut, from the latest decision.
- `current_range`: from the latest decision.
- `direction`: the last decision that moved rates within the last four meetings: a hike gives tightening, a cut gives easing, none gives neutral. On 2026-10-28 that is tightening (Sep 16 hike).

The ledger starts at the 2026-09-16 decision. That hike is in it, so the direction reads tightening until four meetings pass without a move.

## Takeaway templates

Two or three sentences: what came in hotter, cooler, stronger or weaker (sentence 1), the supporting facts (sentence 2), what it means for the Fed and the first market effect (sentence 3). Every figure comes from the release data. Sentences state facts and never claim a component "drove" the result, because contributions are not computed.

**Fed sentence (all data releases)**, chosen by direction and policy state:

| Release direction | Tightening | Easing | Neutral |
|---|---|---|---|
| hawkish | That leans hawkish and adds to the case for another hike | That leans hawkish and weakens the case for another cut | That leans hawkish |
| dovish | That leans dovish and eases pressure on the Fed to hike again | That leans dovish and adds to the case for another cut | That leans dovish |
| none | The report gives the Fed no clear new signal | same | same |

followed by the market clause: hawkish `, and reports like this tend to lift Treasury yields.`, dovish `, and reports like this tend to pull Treasury yields lower.`, none: no clause.

**CPI and PCE**
- Hotter: `Core inflation rose more than expected.` Cooler: `Core inflation rose less than expected.` In line with headline the same: `Core and headline inflation matched expectations.` In line with headline different: `Core inflation matched expectations, while headline came in {higher|lower}.` Mixed: `Core and headline inflation pointed in opposite directions.`
- Sentence 2, CPI: the shelter and energy MoM, and food when it moved 0.3 point or more: `Shelter rose 0.4% and energy 5.2% on the month.` PCE: `Real consumer spending rose 0.6% and the saving rate fell to 4.1%.`
- PCE sentence 1 names the gauge once when the verdict is hotter or cooler: `Core PCE, the Fed's preferred inflation gauge, rose more than expected.`

**PPI**
- Sentence 1 as CPI with "Producer prices" in place of "Core inflation" (`Core producer prices rose more than expected.`).
- Sentence 2: `PPI matters mostly for what it implies for core PCE, out {Mon Nov 25}.` (date from the release schedule; dropped when the next PCE date is unknown).
- Material revisions (prior month MoM revised by 0.1 point or more) add `{Month} was revised to +0.4% from +0.2%.` before the Fed sentence, replacing sentence 2 if the takeaway would exceed three sentences.

**Jobs**
- Sentence 1 from the signals, stronger parts first, joined with "but" when mixed: payrolls +1 `Payroll growth beat expectations`, -1 `Payroll growth fell short of expectations`, cancelled `Payroll growth beat expectations, but downward revisions to the prior two months cancel it out` (or "fell short ... upward revisions"); unemployment +1 `unemployment came in lower than expected`, -1 `unemployment came in higher than expected`; wages +1 `wage growth was faster than expected`, -1 `wage growth was slower than expected`. Neutral signals are left out. All neutral: `Hiring, unemployment and wages all matched expectations.`
- Sentence 2: the net revision when it is 50K or more either way: `The two prior months were revised {down|up} by 60K combined.` Else participation when it moved: `Participation rose to 62.8%.` Else omitted.

**FOMC**
- Sentence 1: `The Fed {held rates at|raised rates by a quarter point to|cut rates by a quarter point to} 3.75% to 4.00%, {as expected|which was not expected}.` plus `, and two officials voted for a hike` when there are dissents (names in the secondary block).
- Sentence 2, projection meetings: `Officials now see rates at 3.6% at the end of 2027, up from 3.4% in September.` Otherwise the statement: `The statement changed how it describes {growth|inflation|jobs}.` only when a changed sentence contains one of those words, else omitted.
- Sentence 3: hawkish or dovish verdicts use the Fed sentence's market clause on its own (`Decisions like this tend to lift Treasury yields.`). As-expected decisions: `With the decision expected, the press conference at 2:30 PM ET is the next test.`

The owner signs off on every template's text once. A later change to a template is a code change with a test.

## Primary and secondary fields

| Release | Primary (in order) | Secondary |
|---|---|---|
| CPI | Core CPI MoM (unrounded on the line), Core CPI YoY, Headline CPI MoM, Headline CPI YoY, Core CPI 3M | Shelter, Food, Energy |
| PPI | Core PPI MoM (final demand less foods and energy, `WPSFD49104`, every release), Core PPI YoY, Headline PPI MoM, Headline PPI YoY | `PPI ex food, energy, trade MoM` (final demand less foods, energy and trade services), prior-month revisions of 0.1 point or more |
| PCE | Core PCE MoM (unrounded), Core PCE YoY, Headline PCE MoM, Headline PCE YoY, Core PCE 3M | Real consumer spending, Personal income, Saving rate, prior-month revisions of 0.1 point or more. Nominal personal spending only when the feed has its consensus and real spending has none |
| Jobs | Nonfarm payrolls, 2-month net revision, Unemployment (unrounded), Avg hourly earnings MoM, Avg hourly earnings YoY | Each prior month's revision, Participation, Avg weekly hours |
| FOMC | Decision (`Hold, as expected`), Target range, Vote | Dissenters and what they wanted, changed statement sentences as `Was:` / `Now:` (at most two, never the decision or vote sentence), projections at projection meetings (median for this year, next year and the long run, each `3.4% to 3.6%`), the Fed's bond holdings only when the implementation note changes them |

## Missing data

- A missing value is omitted, never zero, never inferred. A consensus is never inferred.
- A revision is shown only when both the posted value (ledger) and the revised value exist.
- The post still waits for the core lines (`release_ready`, as today). At the deadline, a release missing its lead line posts `{Release}: Incomplete data` with what it has, and the takeaway is `The initial release is incomplete.` Ops is pinged as today.

## Data this needs that the bot does not fetch yet

| Field | Source | Notes |
|---|---|---|
| Unrounded core MoM | existing index levels | already fetched, rounded today in `compute` |
| Unrounded unemployment | BLS `LNS13000000` (unemployed), `LNS11000000` (labor force) | confirm IDs on first fetch |
| Avg weekly hours | BLS `CES0500000002` | confirm ID |
| PPI less foods, energy, trade services | BLS `WPSFD49116` | confirm ID |
| PPI and PCE prior-month revisions | the same series re-read, against the ledger | ledger already stores rows |
| FOMC dissent names and direction | the statement text already fetched | regex on "Voting against ... preferred to {raise|lower}" |
| FOMC projections | the Fed's projection table published at 2:00 PM on projection meetings | next one Dec 9; needs a fetcher and parser |
| PPI on the release schedule | `world_context.OFFICIAL_RELEASES` | PPI arms only from the ForexFactory feed today |
| Statement body cleanup | `_statement_body` | today's stored text starts with `--> --> -->` page residue |

## Latency

With no model call, the time from the agency's publication to the post is the poll interval (10 s with a BLS key) plus one fetch. The poll interval for the first two minutes after the release time drops to 5 s; the BLS key allows 500 requests a day and a release day uses about 40.

## Retired

`report/print_takeaway.py`, its tests and `scripts/print_sample_run.py`'s takeaway path, the `print_takeaway` spend caller. The research ranking it carried is not used elsewhere.

## Testing

- Golden replays: the five real posts in `/data/print-alerts/` (Sep 16 FOMC, Sep 30 PCE, Oct 2 jobs, Oct 14 CPI, Oct 28 FOMC) copied into `tests/fixtures/print_alerts/` with their rows, rendered under the new rules, compared with expected text written by hand.
- Per release, a synthetic case for each of: clearly hotter or stronger, clearly cooler or weaker, in line, mixed, a large prior revision, missing secondary data, missing primary data. Each checks the title, the takeaway sentences and that no line exceeds 60 characters.
- Verdict tables tested as tables: every row above is one parametrized case.
- Policy state from ledger fixtures: tightening, easing, neutral, and a ledger with one entry.
- Voice: no em-dash, semicolon or banned word in any rendered alert (reuse `ai_analysis.voice_rules.compose_lint_patterns`).

## Rollout

The Oct 29 PCE posts in the current format. Target the Nov 6 jobs report for the new path, Nov 10 CPI next, the projections and dissent parsing before the Dec 9 FOMC.
