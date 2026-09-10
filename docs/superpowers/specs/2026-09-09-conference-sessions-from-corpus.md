# Conference sessions from the corpus — spec

**Date:** 2026-09-09
**Status:** approved and built 2026-09-10. Owner calls taken that day,
overriding the §10 defaults where they differ: NASDAQ-100 admission
(default); one entry per conference per day showing the ET **start**
time only, the conference name, and the admitted companies **in
market-cap order** (no time range, no per-slot list, no agenda line);
rendered in a two-column events section, Economic left and Industry
Events right, names wrapping rather than truncating; rendering shipped
directly with no shadow period, the anchor and printed-date rules
being the safety. Code: `ai_analysis/conference_sessions.py`,
`db.conference_sessions_for_date`, `report/calendar_data.ConfRow` /
`build_conference_rows` / `NDX_TICKERS`, `calendar_render._events_block`;
tests in `tests/test_conference_sessions.py` with the 15746 and 15752
texts as fixtures.
**Origin:** a source investigation the owner asked for (EDGAR 8-K
submissions and newswire RSS as conference feeds), which measured both
against the live corpus and found the answer already inside it. Every
number below was measured read-only against production on 2026-09-09;
nothing is estimated.
**Owner calls, defaults proposed in §10:** admission universe
(NASDAQ-100), one row per conference per day, PT/ET display, shadow
period before rendering.

## 1. Why

The omni-calendar carries econ prints and earnings. It has no row for
the other scheduled catalyst that moves NDX names: a CEO or CFO on
stage at a bank's technology conference. Companies guide, pre-announce
and reprice at these, and the room trades them blind.

We investigated four external sources and one internal one.

| source | carries the signal | date | time | new dependency | verifiable against source text |
|---|---|---|---|---|---|
| SEC EDGAR submissions (8-K 7.01/8.01) | **no** — 0 of 8 companies in the JSON, 0 of 16 documents in the body | — | — | yes (User-Agent contact, CIK map, 6-K for foreign issuers) | no |
| Yahoo Finance | **no** — earnings/dividends only; `quoteSummary`, `v7/quote`, `v7/options` now 401 without a session crumb | — | — | yes | no |
| PR Newswire / Business Wire RSS | yes | yes | sometimes | yes, and it is a firehose that changes shape without notice | no |
| Bank agenda pages (scrape) | yes | yes | yes | yes, per-bank HTML | no |
| **The research corpus we already ingest** | **yes** | **yes** | **yes** | **none** | **yes, via `anchor_check`** |

The decisive artifact is Goldman's morning note of 2026-09-09
(`pdf_files` 15746, "What Matters Today — Communacopia Day 1 Recap and
Agenda Today"). Its extracted text contains, verbatim:

```
Day 2 Schedule – Wednesday, September 9th (all times in PT):
11:10 AM: Opening Remarks & Global Macroeconomics Outlook (Jan Hatzius)
11:50 AM: Adyen NV, CMCSA, PINS, AMKR, ROP, CHKP, DT
12:30 PM: MSFT
 1:10 PM: XYZ, BKNG, NET, COIN, LYV, 100.HK, FFIV
 1:50 PM: NOW, HOOD, CDNS, ETSY, CHYM, CCI, CDR.PW
 2:30 PM: DIS, SNDK, WMT, NU, OKTA, LIME, IMAX
 3:25 PM: DELL
 4:05 PM: T, IBM, JKHY, ST, BCO, STUB, VERX
 4:45 PM: FOXA, HUBS, CART, TER, TNE.AU, RELY
 5:25 PM: AMAT, PYPL, AMT, EXPE, FLEX, AKAM, G24.GR
 6:05 PM: CRM, IREN, KLAR, UPWK, IHRT, YELP
 6:45 PM: SPOT, ADSK, CHTR, SITM, CSGP, MQ, LIFE
 7:25 PM: TSMC, Applied Intuition, ZM, SIRI, SBAC, PPLI, WK
```

Date, timezone, slot time, tickers per slot, published by the host the
morning of. The deep analysis of that same document reduced it to
*"Communacopia Day 1 sentiment was bullish"*: `report_type`
`morning_briefing`, 63 entities, zero dates, zero times. The schedule
was in the pipeline and was discarded because the extraction schema has
no field for it.

Scale, measured over the whole corpus (16,066 analyses, 2026-04-13 to
2026-09-09): 87 conference events with at least one NDX name would have
qualified under a naive regex, roughly four a week, clustering in June
and September. That count is inflated by the regex (it sweeps in
"press conference" and treats every JPM tech note as a conference); the
curated set in §5 is nearer 50-60. Over the last 14 days alone, 96
documents named a conference, 44 of them with an NDX ticker, touching
59 distinct NDX constituents.

**The design in one sentence: stop discarding what the host bank
already sends us, and verify every row against the text it came from.**

## 2. What is extracted

One new list in the deep-analysis schema (`ai_analysis/prompts.py`,
alongside `entities_mentioned` and `key_data_points`), one new
dataclass in `ai_analysis/models.py`, parsed through the existing
`_safe_dataclass` path in `ai_analysis/analyzer.py`:

```python
@dataclass
class ConferenceSession:
    conference: str      # as the document names it: "GS Communacopia + Technology"
    date_iso: str        # "2026-09-09", resolved per §3.2, never inferred
    time_local: str      # "12:30 PM" exactly as printed; "" when the document gives none
    tz: str              # "PT" / "ET" / "" exactly as the document states it
    tickers: list[str]   # US-listed symbols in this slot, uppercase, no $; [] when none
    anchor: str          # VERBATIM schedule line, 6-25 words, machine-verified (§3.1)
```

Prompt contract, in the same register as the existing `anchor` field
(`prompts.py:176`):

> `conference_sessions`: ONLY when the document contains a dated
> schedule of conference sessions (an agenda, a "Day N schedule", a
> line-up with times). One entry per time slot. `date_iso` comes from a
> date PRINTED in the document; never infer one from "Day 1" or "next
> week". `time_local` and `tz` exactly as printed, empty when absent.
> `anchor` is the schedule line itself, copied character-for-character.
> A document with no such schedule returns `[]`. Most documents return
> `[]`.

The same US-listed-only ticker rule as `entities_mentioned` applies.
`Adyen NV`, `100.HK`, `TNE.AU`, `G24.GR` and "Applied Intuition" in
the sample above produce no ticker and are dropped from `tickers`; the
slot survives if any other name in it is US-listed.

Cost: a few hundred prompt tokens per PDF at Flash Lite rates, on
150-200 PDFs a day. Under a cent a day. The analysis prompt is not
under the `/ask` prompt's size ceiling; CLAUDE.md's "deterministic
first" policy applies and is satisfied, because the field is verified
in code (§3), not by more prompt text.

## 3. Verification — what makes it foolproof

### 3.1 Every session is anchored to the source

`ai_analysis/anchor_check.check_anchors(points, source_text)` already
exists, already accepts any list of objects with an `.anchor`, and is
already called in the analyzer at the one moment the extracted text is
in memory (`analyzer.py` ~506). It is called a second time on
`conference_sessions`. Unlike the `key_data_points` call, which is
WARN-ONLY by design (redesign step 2), this one is ENFORCING: a session
whose anchor is not a normalized substring of the extracted text is
**dropped before the analysis is stored**, and the drop is logged.

That is the whole foolproofing. An invented session has no anchor. A
paraphrased slot fails normalization. A hallucinated ticker that the
model appended to a real slot still fails, because the anchor is the
printed line and the printed line does not contain it.

### 3.2 Dates are printed or they do not exist

`date_iso` is accepted only when the document's text contains the
month and day (the sample prints "Wednesday, September 9th"). The year
is the year of the PDF's own `dropbox_modified_at`; a document dated
December that schedules January is the one edge, handled by rolling
forward when the resolved date is more than 30 days in the past. A
session with no printable date is dropped. "Day 2" is a label, not a
date.

### 3.3 Image schedules produce nothing, correctly

Goldman's "Best of Day 1" note (15752) carries its schedule as a page
image; its extracted text reads `Day 1 Schedule` followed by a blank.
That document yields `[]`. The morning note (15746) carries the same
schedule as text and yields 13 slots. Two documents, one agenda, the
text-form one wins, the image one contributes nothing and claims
nothing. This is the correct outcome, not a gap: the design never reads
pixels.

### 3.4 Timezone is captured, never assumed

`tz` is stored exactly as printed. The calendar converts to ET only
when `tz` is one of a small known set (PT, CT, MT, ET and their
daylight variants). A session with an unrecognized or empty `tz`
renders **day-level** (no clock), the same degradation the earnings
rows already use for an unconfirmed session (`EarnRow.session_confirmed`).

## 4. Storage and query

No schema migration. `conference_sessions` rides inside
`pdf_analyses.analysis_json` like every other extracted list, and is
read with the latest-analysis-per-PDF CTE the codebase already uses
(`db_parts/pdf.py:1134`, `MAX(id) GROUP BY pdf_file_id`).

One new facade helper, in `db_parts/pdf.py` and re-exported from
`db.py`:

```python
def conference_sessions_for_date(date_iso: str, *, days_back: int = 7) -> list[dict]:
    """Sessions dated `date_iso` from any analysis stored in the last
    `days_back` days, latest analysis per PDF, deduplicated on
    (conference, date, time_local, ticker). Source file and analysis id
    ride along for the audit trail."""
```

Dedupe matters: on 2026-09-09 three Goldman notes described the same
conference. The morning note carries the schedule; the other two carry
themes. Where two documents both carry the schedule, the union is
taken and the tuple key collapses duplicates.

The consumer reads this one function and nothing else, so when the
claim-card redesign replaces `pdf_analyses` (spec 2026-08-20 §9.2, the
table "stops being written but is never dropped"), a session becomes a
card type and only this helper changes.

## 5. Calendar consumer

### 5.1 Admission

A session reaches the sheet when at least one of its `tickers` is in
the admission universe. **Default: NASDAQ-100 constituents**, a
committed list in `report/calendar_data.py` refreshed by hand (the
index reconstitutes annually in December; a stale list costs a new
entrant, never a false row). Non-admitted tickers in an admitted slot
are dropped from the rendered row so it stays readable; the full slot
is still in the data.

This is the same discipline as earnings: hard-filtered at the data
layer, because the sheet is only readable if most things are excluded.

### 5.2 Row shape — one row per conference per day

Thirteen timed slots do not fit a 4:5 image beside fifteen earnings
rows. The row is the conference, not the slot:

```
GS Communacopia + Technology  ·  11:50a–7:25p PT  ·  CMCSA MSFT BKNG CDNS AMAT PYPL ADSK CHTR ...
```

Time range from first to last admitted slot, in the document's
timezone with the label, and the ET equivalent in the footer note when
`tz` is recognized. Tickers in slot order. Bold (`important`) when any
admitted ticker is in `news_data._MAJOR_TICKERS`, the rule
`earn_is_important` already applies. Rows per day capped at 4; a fifth
conference on one day is a bug in the admission list, not a rendering
problem.

Per-slot times are preserved in the data for `/ask` ("when is MSFT on
at Communacopia") and for the pulse; only the sheet collapses them.

### 5.3 Rendering

`CalendarDay` gains `conferences: list[ConfRow]`. `render_calendar_png`
gains a **Conferences** band between section 5 (Economic) and section
6 (Before Open / After Close), drawn with the existing `_band` and the
econ row fonts, **only when the list is non-empty**. Unlike econ, which
prints "no notable US releases" on a quiet day, an empty conference
list draws nothing: absence is the normal state and the sheet should
not spend a line saying so.

### 5.4 The refresh must see it

`lineup_signature()` (`calendar_data.py` ~387) hashes econ and
earnings rows so the 7:30 AM refresh can tell a changed lineup from an
unchanged one. Conference rows join the signature. Without this the
refresh would never notice that the morning note arrived with today's
agenda, which is exactly the case §7 says it is for.

## 6. Lead time — what the sheet can and cannot promise

The sheet posts at 3:00 PM ET for the NEXT session. The host bank
publishes the agenda in one of two places:

- a **preview** a day or more ahead (15692, 2026-09-08, for Day 1 on
  the 8th — text form, but its line-up is prose, not a slot schedule);
- the **morning note** on the day (15746, 2026-09-09, "Agenda Today",
  full slot schedule).

So for a conference day, the 3 PM sheet the evening before will carry
the row when a slot-form preview exists, and otherwise the 7:30 AM
refresh will add it from the morning note. `CALENDAR_REFRESH_ENABLED`
is currently off pending the row-level merge in CLAUDE.md's TODO; this
spec adds a second reason to finish that merge, and does not depend on
it to ship (the evening sheet alone still catches previews).

Honest expectation from two weeks of data: roughly half of conference
days will be on the evening sheet, the rest need the refresh.

## 7. What this does not do

- It does not know about a conference no ingested bank wrote up. For
  NDX that set is small: GS, MS, JPM, Citi, DB, BofA, UBS and Barclays
  all host technology conferences and all publish agendas in the notes
  we ingest. It is still a coverage statement, not a completeness one.
- It does not read image-form schedules (§3.3).
- It does not put session times on the sheet (§5.2), only the range.
- It does not touch the pulse. A "Communacopia runs this week, these
  names present" line in WHAT TO WATCH is a natural follow-on and a
  separate change.
- It does not touch the pilot. `pilot_publish.py` publishes extracted
  text, not `analysis_json`; readers are unaffected.

## 8. Rollout

**Phase A — shadow (5 trading days).** Extraction, anchor enforcement
and the facade helper ship. Nothing renders. Each day's
`conference_sessions_for_date` output for the next session is written
to the log and to `pulse-output/conference-shadow/<date>.json` on
`pulse-data` by the bridge, beside the calendar post. Measured: rows
per day, anchor match rate, drops, and a by-hand read of whether each
row is a real session. Phase B does not start if the anchor match rate
is under 90% or any shadow row is not a real session.

**Phase B — render.** The band, the signature change and the admission
list ship together.

Fixture: the extracted text of 15746 is already committed on
`pilot-data` under `source-text/2026-09-09/`; a trimmed copy goes under
`tests/fixtures/` as the regression case, with 15752 (image schedule)
as the negative case.

## 9. Tests

- Parse: a real 15746-shaped analysis yields 13 sessions; the empty
  document yields `[]`; a malformed entry is dropped by
  `_safe_dataclass`, never raises.
- Anchor enforcement: a session whose anchor is not in the source text
  is absent from the stored analysis; one whose anchor is present
  survives; the log names the drop.
- Date: "Wednesday, September 9th" in a document modified 2026-09-09
  resolves to `2026-09-09`; "Day 2" alone resolves to nothing; a
  December document scheduling January rolls forward.
- Timezone: "PT" 12:30 PM renders 3:30 PM ET; unknown `tz` renders
  day-level.
- Admission: a slot of only non-NDX names produces no row; a mixed slot
  keeps the NDX names and drops the rest.
- Dedupe: two documents carrying the same slot produce one row.
- Renderer: the Conferences band is absent on an empty list and present
  on a populated one; `lineup_signature` changes when a conference row
  changes and is stable when it does not.
- Wiring: the facade re-exports the helper; `build_calendar_day` calls
  it (pinned on source, the way `test_ask_grounding_passthrough` pins
  phase wiring).

## 10. Owner decisions

| decision | proposed default | alternative |
|---|---|---|
| Admission universe | NASDAQ-100, committed list | `_MAJOR_TICKERS ∪ recently_covered_tickers`, which tracks what the research is actually discussing |
| Row granularity | one row per conference per day, time range | one row per slot (does not fit the image) |
| Time display | document tz with label, ET in the footer | ET only |
| Shadow length | 5 trading days | ship rendering directly |
| Finish the refresh merge first | no, ship the evening sheet alone | yes, so the morning note is caught from day one |

## 11. Size

Smaller than the econ dedupe shipped 2026-09-08: one dataclass, one
prompt paragraph, one enforcing call to a function that already exists,
one facade helper, one `CalendarDay` field, one band in the renderer,
one line in the signature, and the tests above. Two sessions of work
including the shadow-phase readout.
