# Economic print embed: owner layout, extra series, Quick Takeaway

Date: 2026-09-30. Owner direction from today's thread, format given verbatim:

```
August PCE Inflation Print
 * Core PCE (MoM): +0.2% (vs. +0.3% exp)
 * Core PCE (YoY): +3.0% (vs. +3.3% exp)
 * Headline PCE: +0.3% MoM / +3.4% YoY (in line / below forecast)
 * Real Consumer Spending: +0.6% (beat +0.3% exp)
 * Personal Income: +0.2% | Saving Rate: 4.1% (down from 4.4%)
Quick Takeaway
 * Disinflation in play: ...
 * Soft-landing fuel: ...
 * The caveat: ...
```
plus a source citation (bea.gov, bls.gov, federalreserve.gov). Releases stay the current four: CPI, jobs report, PCE, FOMC decision. Every post keeps the /ask green border and the @everyone ping from `e2b7e9ad`.

## What the reader sees

Embed title: `<Month> <release headline>`: "August CPI Inflation Print", "September Jobs Report", "August PCE Inflation Print", "September FOMC Decision". Month is the reference month for data prints and the meeting month for the FOMC.

Body, in order:

1. **Series bullets**, one per line, `• <display>: <actual> (<comparison>)`:
   - with a consensus: `(vs. +0.3% exp, below)` where the verdict is above / below / in line at the displayed precision (the existing `verdict`);
   - without one: `(prior +0.2%)`; a level series such as the saving rate reads `(down from 4.4%)` / `(up from 4.4%)` / `(unchanged from 4.4%)`;
   - a **pair** of m/m and y/y series prints on one line: `• Headline PCE: +0.3% MoM / 3.4% YoY (vs. +0.3% / 3.3% exp, in line / above)`. When only one side has a consensus, each side is compared on its own terms and the two are joined with ` / `, for example `• Avg Hourly Earnings: +0.3% MoM / 3.1% YoY (vs. +0.3% exp, in line / prior 3.2%)`; a side with no consensus and no prior adds nothing;
   - two short series may share a line with ` | ` (personal income and saving rate) by giving them the same `row` key.
   Order is the spec's line order: core first, headline pair, then the extras.
2. **Computed lines**: `• Core CPI 3-month annualized: 2.4%` (CPI, PCE core index compounded over the last three months), `• Prior month revised: +120K from +142K` (jobs, from last month's posted rows, so it appears from the second report on).
3. **Quick Takeaway** (bold header, then `• **Label:** text` bullets) from `report/print_takeaway.py` (built 2026-09-30 in this thread): two or three bullets written by `gemini-3.1-flash-lite` from the print rows, the computed lines and the last ten days of bank research on the release, with the figure guard (every number must appear in those inputs), the voice rules, and the 60-word cap. A failed or empty takeaway is omitted. The post waits for it at most 20 seconds (`TAKEAWAY_TIMEOUT_S`), the accepted price of carrying it in the same embed.
4. **Source line**: `Source: bea.gov (NIPA tables 2.8.4, 2.6, 2.8.6) · consensus: ForexFactory`, one per release, with the agency link in angle brackets so Discord does not unfurl it. FOMC: `Source: federalreserve.gov, FOMC statement`.

Footer keeps `released 8:30 ET · <agency>`.

## Series per release

| Release | Core lines (must be present to post) | Extra lines (optional, omitted when the agency has not published them) |
|---|---|---|
| CPI (BLS) | Core CPI m/m, Core CPI y/y, headline pair CPI m/m + y/y | Shelter m/m `CUSR0000SAH1`, Energy m/m `CUSR0000SA0E`, Food m/m `CUSR0000SAF1`; computed: core 3-month annualized |
| Jobs (BLS) | Nonfarm payrolls, unemployment rate, AHE pair m/m + y/y | Participation rate `LNS11300000` (level, "up from"); computed: prior-month revision |
| PCE (BEA) | Core PCE pair m/m + y/y, headline pair m/m + y/y (table `T20804`) | Real consumer spending m/m `DPCERX` (table `T20806`), Personal income m/m `A065RC` and Saving rate level `A072RC` (table `T20600`, shared row); computed: core 3-month annualized |
| FOMC (Fed) | Target range, action, vote | Statement changes: up to three sentences that differ from the previous statement stored in the ledger; appears from the second decision on |

Consensus comes from the ForexFactory rows already mapped (`Core PCE Price Index m/m`, `Personal Income m/m`, `Personal Spending m/m`, `CPI m/m`, `Core CPI m/m`, `CPI y/y`, `Core CPI y/y`, `Non Farm Payrolls` after the feed's own name mapping, `Unemployment Rate`, `Average Hourly Earnings m/m`). Real spending has no feed row; nominal `Personal Spending m/m` is shown as its own bullet when the feed carries it, so the "exp" comparison is like for like.

`release_ready` tests only the core lines. An extra line whose series is missing from the agency payload is dropped from the body and logged; `parse_bea` already logs what a table carries, so a wrong series code shows on the first live run without blocking the post. The BEA fetch groups series by table; the BLS fetch stays one request.

## Ledger

`mark_posted` records, per release: the rendered body lines, the rows with numeric actuals (for next month's revision line), and for the FOMC the statement text (for next meeting's changes). `already_posted` is unchanged.

## Research ranking for the takeaway

`print_takeaway.research_for_release` orders notes tier-1 first (Goldman Sachs, Morgan Stanley, JPMorgan, Citi, BofA, Deutsche Bank, UBS, Barclays), then others, and within a tier prefers items whose macro indicator has status `forecast` for the release's reference period. At most two non-tier-1 notes. The prompt gains one rule: no adverbs of degree ("perfectly", "significantly").

## Out of scope

More releases (owner: the existing four), the X post, the calendar sheet, changes to the pulse's WHAT TO WATCH.

## Testing

- `render_release` on the real 9/11 BLS fixture: bullet strings for CPI and jobs exactly as specified, pair lines, prior fallbacks, level "down from".
- PCE with the synthetic BEA fixture extended with `T20600`/`T20806` rows: shared income/saving line, real spending line, optional lines omitted when their series is absent.
- `release_ready` ignores optional lines.
- Revision line from a stored prior ledger; absent on the first month.
- FOMC statement changes from two fixture statements; absent when no prior is stored.
- The job posts the body with the takeaway when `print_takeaway.generate` returns bullets and without it when it returns `[]` (patched), and the ledger carries rows and statement text.
- Existing tests (`test_print_watch.py`, `test_print_watch_format.py`) updated to the new body; the monospace table renderer is removed.
