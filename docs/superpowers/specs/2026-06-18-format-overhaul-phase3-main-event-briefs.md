# Format Overhaul Phase 3 — THE MAIN EVENT + BRIEFS

**Date:** 2026-06-18
**Status:** approved (user picked "New labeled blocks, web-safe")

## Goal

Split the monolithic `## 2. INSIGHTS & ALPHA` essay block into the
inverted-pyramid pair from the 7-section architecture:

- **④ THE MAIN EVENT** — ONE deep essay on the day's dominant story.
  Full mechanism teaching, named bank-vs-bank debate, the invalidation,
  the trade. Today's slot-1 quality, concentrated. This is where the
  voice lives.
- **⑤ BRIEFS** — 2-4 compressed themes. 3-4 sentences each, no shared
  template, each ends with a lean + invalidation.

Net effect: ~current length, front-loaded. The reader gets the one
story that matters in depth, then the rest at a skim.

## Key decision (user)

New labeled top-level blocks in Discord (`## THE MAIN EVENT`,
`## BRIEFS`) — the full inverted-pyramid look — WITHOUT requiring any
change in the Stock-market-dashboard repo. We achieve web-safety by
mapping both headers to the existing `.insights` CSS class in our
fragment renderer. The dashboard's CSS already styles `.insights` /
`.insights-body`, so both new sections inherit that styling with no
coordination needed. (CLAUDE.md cross-repo contract honored — no
selector rename; the new sections piggyback the stable `.insights`
hook. BRIEFS carries `class="insights briefs"` so it styles as insights
but doesn't collide on the `#pulse-insights` deep-link id.)

## Change surface (deterministic-split approach — chosen for low risk)

The production synthesis path is the Claude.ai routine executing
`DRAFT_USER → AUDIT_USER → SCRUB_USER` + `pulse_lint.py` +
`pulse_draft_validate.py`, all of which are tightly tuned to the single
`## 2. INSIGHTS & ALPHA` header (theme-count floor, top-3-by-bank-count,
slot-overlap lint, dropped-theme accounting). Renaming the header at the
prompt level would ripple through ~7 files of QC machinery. Instead we
keep all of that machinery operating on INSIGHTS and **split
deterministically at bridge post-time** — the same place Phases 1/2
inject WHAT CHANGED / DESK SIGNAL / TRADE BOARD.

1. **`report/pulse_sections.py`** — new `split_main_event_briefs(md)`:
   renames the INSIGHTS H2 → `## N. THE MAIN EVENT` (keeps the first
   `### ` slot), inserts `## N+1. BRIEFS` before the 2nd slot, renumbers
   WHAT TO WATCH. Idempotent; <2 slots → MAIN EVENT only; 0 slots →
   untouched. `extract_leans_from_markdown` / `inject_sections` need NO
   change because they run BEFORE the split (on INSIGHTS).

2. **`github_bridge/jobs.py`** — call `split_main_event_briefs` as the
   LAST transform after `inject_sections`, so the final order is RECAP →
   WHAT CHANGED → DESK SIGNAL → MAIN EVENT → BRIEFS → TRADE BOARD →
   WHAT TO WATCH. The post-split markdown is what's archived, so the web
   fragment and Discord stay consistent.

3. **`ai_analysis/prompts.py` (DRAFT_USER + AUDIT_SYSTEM)** — the ONE
   content change: position-dependent depth. DRAFT writes the lead theme
   deep (~250-350 w, full five-movement arc → MAIN EVENT) and the rest
   compressed (~80-120 w, 3-4 sentences, lean+invalidation → BRIEFS).
   AUDIT told to preserve that profile (don't pad briefs / trim lead).
   Header names, theme-count floor, top-3 rule, lint, validator all
   UNCHANGED — they still see INSIGHTS upstream.

4. **`report/formatter.py`** — `SECTION_COLORS` gains `"MAIN EVENT"`
   (deep blue) and `"BRIEFS"` (lighter blue).

5. **`scripts/pulse_dashboard.py`** — `_classify` maps `main event` →
   `insights` and `brief` → `insights briefs`; wrap matcher fixed to
   read the first class token. No dashboard-repo change.

The dead `DAILY_SYNTHESIS_USER`/`_SYSTEM` legacy single-shot prompts
(imported but never `.format()`-ed) were also updated to the new
structure for consistency; they do not affect production.

## Testing

`scripts/smoke_format_overhaul_phase3.py`: lean extraction finds leans
under MAIN EVENT + BRIEFS headers; inject anchors correctly before THE
MAIN EVENT; formatter colors the new sections; the DRAFT prompt emits
the new structure (and no longer says "THREE sections"); classifier
maps both new headers to insights. Full suite stays green.

## Out of scope (later phases)

Phase 4: ① TAPE reshape (5 lines + FRED actuals), ⑦ corpus levels on
WATCH, ② PT-change flips, retiring the old essay-count machinery.
