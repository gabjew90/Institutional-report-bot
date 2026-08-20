# Claim-card synthesis redesign — spec

**Date:** 2026-08-20
**Status:** approved for sequencing; the redesign itself ships only if the
shadow pilot passes the pre-registered rubric in §8.
**Origin:** three-round adversarial design review (proposal → critique →
revision → critique → revision → convergence). This document is the
converged v3 plus the three final patches (§3.1 reader cadence, §5
gate keying, §9.1 leans continuity).
**Owner calls locked at defaults** (overridable before pilot day 1):
10-trading-day pilot window, 10% fragmentation threshold, owner
tiebreaks grader disagreements.

## 1. Why

Four structural problems in the current synthesis core, each documented
in this repo's own changelog:

1. **The extraction ceiling.** Flash Lite fills a rigid JSON schema per
   PDF and every downstream stage works from that copy. The clustering
   and adjudication machinery exists mostly to compensate for noise
   introduced there, and the worst incidents (SpaceX +119%, the CPI
   forecast-as-print recap) trace to it.
2. **Copy-vs-copy verification.** The adjudication lint verifies quotes
   against Gemini's extraction, not against the source PDF.
3. **The rewrite cascade.** DRAFT → STITCH → EDIT → SCRUB → FIXUP: each
   full-document rewrite introduces errors the next layer must catch.
   STEP 5.75 exists solely to catch what EDIT/SCRUB introduced. STITCH
   once deleted the entire `## _LEANS` block.
4. **Ignored gates.** Control flow lives in routine prose a model can
   skim. The changelog documents the skipped-gate class three times.

## 2. Ingestion — incremental, tiered readers, two-part artifact

Dropbox polling, the per-document processing tick, and per-document
failure isolation stay exactly as they are. These are load-bearing
properties, not implementation details.

The worker cannot call Claude, so readers run as a **dedicated Claude
reader routine on its own cron**, draining the accumulated queue through
the existing opus_bridge rail. Tiering:

| Tier | Reader | Notes |
|---|---|---|
| HIGH | Opus | full document |
| MEDIUM | cheaper reader (same artifact contract) | pressure valve: first-N-pages policy available per-tier in the driver |
| LOW | dropped after the one surviving cheap Gemini triage call | |

Each processed document emits **two linked artifacts**:

- **Document brief** — 100-200 words compressing the note while
  preserving its internal causal chain ("5K underlying job growth →
  participation-driven unemployment → 4-5 of 12 voters"). Compression,
  not extraction. Argument structure is what the best pulses are made
  of; atomizing it destroys the thing being sold.
- **Claim cards** — every figure, level, target, call, and stance:
  `{bank, document, claim, verbatim anchor quote, status
  (released/forecast/target), instrument(s), direction, conviction,
  timeframe}`. The brief carries reasoning; the cards carry everything
  checkable.

### 2.1 Reader cadence is upload-weighted, not uniform

Bank research is not uniform across the day: the heavy window is
~5-10 AM ET (the Goldman morning stack, Hartnett, desk notes). A
uniform 2-4h cron would leave the morning catch-up read (§4) covering a
third to half of the day's HIGH volume inside the pulse window — the
mid-pulse spike this design exists to avoid, reintroduced at smaller
scale. Therefore: **dense cron during 5-10 AM ET (hourly, plus a
dedicated ~9:15 run), sparse overnight (every 4h)**. The catch-up read
is then genuinely marginal.

## 3. Verification at ingestion — the guarantee at its true size

Python validates every card anchor against the extracted source text
with **normalized matching** (dehyphenation, whitespace collapse,
ligature stripping) — literal substring matching false-fails on
two-column exhibit-heavy PDFs. A failed card gets **one re-ask, inline,
within the same reader session** (a re-ask deferred to the next bridge
round-trip is a card lost for hours), then drops.

**The honest scope, stated in docs and in the QC rubric:**

- Cards are verified to source.
- Briefs are model compression and cannot be substring-verified.
- The published claim is: *every number and every attributed call in
  the pulse traces to a literal string in a source PDF; the reasoning
  is model-written and adversarially checked.*

### 3.1 Daily brief spot-audit (core design, not garnish)

Without this, the copy-vs-copy problem relocates to the brief layer:
the gate would verify pulse-vs-brief while brief-vs-source is checked
by no one. Every day, **3-5 briefs are audited directly against their
extracted source text** by the adversarial pass — weighted toward
whatever the MAIN EVENT cites, sampled across both reader tiers so the
cheap tier cannot drift unwatched. This converts the unverified layer
from a structurally open loop into a probabilistically monitored one
and produces the longitudinal drift data.

## 4. Morning assembly — catch-up read, then a ledger that warns but never gates

The pulse routine's first act is a catch-up read of anything landed
since the last reader drain (small by construction, per §2.1), so a
9:40 AM PDF still makes the 10 AM pulse and freshness does not regress.

Python builds the ledger from all accumulated briefs and cards:

- bank-deduplicated for/against counts **keyed hard on instruments and
  figures** (exact grouping)
- reader topic labels **grouped soft** (they will fragment sometimes)
- concentration stats alongside every count (e.g. Goldman's corpus
  share)

**Structural rule: grouping filters nothing.** The editor receives
every brief and the full card ledger regardless. Imperfect grouping
degrades a backstop warning behind a model that read everything,
instead of deciding upstream what the writer ever sees — the current
system's original sin (a clustering mis-merge once presented a 15-bank
Hormuz subject as five thin themes).

No embeddings, no cosine thresholds, no merge caps, no normalization
maps. That machinery is deleted with the architecture, not patched.

## 5. Synthesis — one write, two citation modes, patches not rewrites

Opus receives the complete brief set (~7-10K tokens for 50 documents),
the card ledger (~40K), live data, the calendar, and open reads —
comfortably one context — and writes the pulse under two rules:

1. every figure or attributed call cites a card ID (`[c142]`)
2. every mechanism paragraph cites its source brief(s) (`[d17]`)

Python resolves both. Card citations get **hard verification** (the
sentence's numbers and bank names must appear in the cited card). Brief
citations get **existence verification** plus a concrete target for the
gate ("does this paragraph faithfully represent brief d17"). Citation
markers are stripped before publish.

Voice lint runs in the same driver pass against the same rules file
(`ai_analysis/voice_rules.py`) that exists today. All failures —
factual and voice alike — become **sentence-level re-asks applied as
patches**, never a full-document rewrite, with a **one-sentence-radius
coherence check** around every patch so a changed number cannot orphan
the next sentence's segue.

> The patch applicator is string surgery of exactly the
> pulse_stitch-truncation class. It ships with a test suite on day one
> or not at all.

Honest layer count: one write, then lint, then a patch loop, then a
gate — three enforcement layers instead of five. The difference that
matters is not the count: **no layer is a full-document rewrite**, so
the introduce-errors-after-validation class is structurally gone rather
than caught later.

The editor also emits the `## _LEANS` block under today's exact
contract (see §9.1) — it is an output requirement of the write, checked
by the surviving `leans-block-missing` validator.

## 6. The gate — blocking, before commit, hard keys block / soft keys warn

A fresh sub-agent with no drafting history receives the final draft,
all briefs, the ledger, and the day's spot-audit source texts. Checks:

| Check | Keying | Action on violation |
|---|---|---|
| Unsupported sentence (citation doesn't back it) | card (hard) | **block** |
| Mechanism paragraph unfaithful to cited brief | brief | **block** |
| Brief unfaithful to source (spot-audit set) | source (hard) | **block** |
| Instrument/figure with N+ banks absent from the pulse | instrument (hard) | **block** |
| Sector over slot budget (AI-bias control as arithmetic) | instrument/sector (hard) | **block** |
| Topic label with N+ banks absent | topic label (soft) | **warn** |

The keying split is required by the design's own logic: blocking
arithmetic over soft labels gives arithmetic's confidence with the
labels' softness (a fragmented topic splits 6 banks into 3+3, both
below N, and the constraint silently never fires; a mis-grouped label
fires a false block and burns a repair round). Hard keys get hard
enforcement; soft keys get surfaced.

Two failed repair rounds ships with a labeled residual note — the same
ship-anyway philosophy as today, but the check sits **before Discord**
instead of documenting failures after them.

## 7. Control flow — a checked-in driver; the model dispatches

Every deterministic step lives in a driver script the routine executes.
It halts at dispatch points and states exactly which agents to launch,
with which prompt files, and where responses land. The model's surface
area is agent dispatch and MCP commits. Exit-code branching, retry
budgets, gates, and reverts are tested Python. This kills the
ignored-gate class and is **independent of everything else in this
spec** — it ships first regardless of the pilot's outcome.

## 8. Shadow pilot — rubric frozen before anyone sees output

**Setup.** 10 trading days *(owner-adjustable; shorter makes the
fragmentation estimate noise)*. opus_bridge readers on HIGH PDFs emit
the two-part artifact on the reader cron; cards accumulate in the DB.
Each morning a shadow editor pass writes a shadow pulse from the
ledger. Production runs untouched. Nothing is deleted during the pilot.

**Metrics (frozen now):**

1. **Grouping integrity** — fragmentation = % of card mass in
   label-groups a grader judges to be the same underlying subject as
   another group; mis-merge = distinct subjects under one label.
   *Pass: ≤10% fragmented mass, zero mis-merges that would have changed
   theme selection.*
2. **Fact fidelity** — 15 sentences sampled per shadow pulse, each
   traced to the **source PDF** (not the card), graded
   faithful/distorted/unsupported; same procedure on that day's
   production pulse. *Pass: shadow faithful-rate ≥ production, zero
   unsupported.*
   - **2a. Brief-vs-source fidelity** — the daily spot-audit grades,
     aggregated across the window, split by reader tier. Brief quality
     is the load-bearing unverified assumption; running the pilot
     without measuring it would be malpractice. *Pass: zero briefs
     graded "distorts the source's argument."*
3. **Mechanism preservation** — does the shadow pulse's lead theme
   reproduce the causal chain the source note actually argued, graded
   against the PDF? Binary per day. *Pass: ≥ production across the
   window.*
4. **Ledger attention** — distribution of cited card positions.
   *Flag, don't fail:* >70% of citations from the first and last
   quintiles of the ledger.
5. **Ops** — reader failure rate, quota per day (lumpy-batch shape),
   wall-clock. *Pass: no morning where reading would have collided
   with the pulse window.*

**Graders.** Two fresh agents per dimension, no drafting history,
rubric text frozen before day 1; disagreements tiebroken by the owner
*(default; owner may delegate)*. Head-to-head preference voting is
excluded — the artifacts cannot be blinded (format differences identify
them), so all grading is per-artifact against source.

**Decision rule (frozen now).** Expand to MEDIUM only if 1, 2, 2a, and
3 all pass. Kill if 2, 2a, or 3 regress. Anything else buys exactly one
iteration on the reader prompt, then re-run, then decide. No third
iteration: an architecture that needs three rounds of prompt surgery to
beat the incumbent has answered the question.

## 9. Migration seams (the unowned edges that kill migrations)

### 9.1 Leans and the settlement loop — the editor owns `## _LEANS`

Today DRAFT writes `## _LEANS` → `pulse_leans` → the open-reads
settlement rule and the TRADE BOARD's structural source. The
accountability loop (settling a tracked stake, especially when it went
the wrong way) is one of the product's most-loved properties. In the
new pipeline **the editor emits `## _LEANS` under today's exact
contract**; the `leans-block-missing` hard validator survives the
migration unchanged; the bridge strips the block at post time exactly
as now. The board renderer, its voice sanitation, and `pulse_leans`
tracking are untouched.

### 9.2 Other seams carried over unchanged

- Bridge transport, delivery idempotency, holiday/volume/press-time
  gates.
- The consensus ledger (`{prev_consensus_block}`) and the
  `consensus-amnesia` validator — the editor's RECAP duty is identical.
- `/status`, footer stats, archive/web-fragment publishing: the
  `daily_reports` row contract is unchanged; briefs/cards are new
  tables alongside `pdf_analyses`, which stops being written but is
  never dropped (history).
- The Discord surface: prose only — lead, 3-5 deep sections, no
  tables, no corpus meta-narration. The **coverage table** (one row per
  ledger topic: banks for/against, the one number that matters, named
  dissent) goes to the **web fragment only**. Five-minute completeness
  is depth on Discord plus breadth on the dashboard, not a markdown
  table forced through mobile rendering.

## 10. Cost model

- **Gemini** collapses to the triage floor: $15-20/month → toward the
  $5 Railway baseline. (/ask is unaffected and remains Gemini.)
- **Claude routine quota** rises ~3-8x, arriving as a handful of lumpy
  batch runs per day (reader drains + morning catch-up) — the shape
  that matters against plan limits. Mitigations: the tier valve (shift
  MEDIUM to the cheaper reader or first-N-pages) and the fact that
  drift toward a quota ceiling is visible weeks out and tunable
  per-tier in the driver, whereas the failure it replaces (dying
  mid-synthesis at 10:40 on the heaviest news day) was invisible until
  it happened.
- Net: dollars down, quota exposure up, degradation graceful and
  policy-controlled.

## 11. Sequencing

| # | Step | Depends on pilot? |
|---|---|---|
| 1 | Driver script for routine control flow | **No — ships regardless** |
| 2 | Verbatim anchors + normalized-match checker on the *current* extraction (builds the pilot's verification plumbing) | No — ships regardless |
| 3 | Blocking pre-commit adversarial check on the *current* final.md | No — ships regardless |
| 4 | Shadow pilot (§8) on the opus_bridge rail | — |
| 5 | Expand/kill per the frozen decision rule | Yes |
| 6 | Delete clustering machinery, retire DRAFT→…→FIXUP | Yes — only after §8 passes |

## 12. What this design does not fix (on the record)

Brief quality and editorial judgment remain model-bounded and
unverifiable. No design removes them; this one shrinks the unverified
surface to those two places and points the spot-audit and the gate
directly at both. The deleted clustering bugs were also fixable in
~twenty lines each — the deletion is justified by the architecture
(gate → guardrail), not by the bug count.
