# Contracts this repo exposes to the Opus pulse routine

> **CORRECTION (2026-07-29): there is no separate routine repo.** This
> doc's "two repos" framing below is stale. The executable routine is
> **`docs/superpowers/routines/synthesis-routine.md` in THIS repo** (a
> Claude.ai bootstrap `curl`s and runs it verbatim each fire), and it
> already invokes the validators described here:
> `scripts/pulse_draft_validate.py` (STEP 4.5), `scripts/pulse_stitch.py`
> (STEP 5), `scripts/pulse_lint.py` (STEP 5.5/5.7). The prompt constants
> it runs (`ADJUDICATION_*`, `DRAFT_*`, `AUDIT_*`, `SCRUB_*`, `QC_*`)
> also live here in `ai_analysis/prompts.py`. So consumer-side work
> listed below is largely DONE — verify against the routine file before
> treating any item as outstanding. Read "second repo" as "the Claude.ai
> routine runtime" throughout.

The pulse pipeline is split across **two repos**:

| Repo | What runs there |
|---|---|
| **`Institutional-report-bot`** (this one) | clustering, theme_map, contrarian detection, sibling fold, theme_coverage rendering, lint patterns, voice rules, DRAFT validator |
| **Opus routine repo** (separate) | DRAFT, STITCH, EDIT, AUDIT, LINT dispatch, SCRUB dispatch, publish-to-Discord orchestration |

The QC reviewer's recommendations regularly land at the **boundary** between
these two — and several have recurred across 3+ reviews because the data
contract this side exposes is not consumed by the routine side. This
document is the single source of truth for what this repo publishes
to the routine. Every QC recommendation that touches one of these
artifacts should land in the routine repo on the consumer side, not
here.

If you (or another Claude session) work in the routine repo, the
checklist at the bottom enumerates what to wire up.

---

## Data artifacts published by this repo

### 1. `theme_map` fields (in `ctx.json` / pulse-context)

| Field | Set by | Consumer responsibility |
|---|---|---|
| `banks: int` | `_classify_themes` Phase A | Display, rank, threshold checks |
| `pdfs: int` | `_classify_themes` Phase A | Same |
| `sources: list[str]` | Phase A | Display, tier-1 detection |
| `supportive`, `skeptical`, `neutral` counts | Phase A | **Drive named-bank-vs-bank disagreement when split ≥ 2/2** |
| `discovered: bool` | Phase B | Render as separate bucket; route to WATCH |
| `non_bank_only: bool` | Phase A | Don't lead INSIGHTS with these |
| `sibling_canonicals: list[str]` | Two-tier merge cap | **Render as ONE INSIGHTS section, sub-bullet the siblings** |
| `underweighted_candidate: bool` | Synthesizer post-Phase-A | **Surface AT LEAST ONE as WATCH bullet or threaded into INSIGHTS** |
| `contrarian_to_lead: bool` | Synthesizer post-Phase-B (NEW) | **Promote as own INSIGHTS section, do NOT bury in lead's bear-case appendix** |
| `contrarian_signal_labels: list[str]` | NEW | Pass to DRAFT alongside the contrarian theme so it has the specific frames to write |
| `contrarian_titles: list[str]` | NEW | Sample titles for DRAFT to reference |
| `close_style: str | None` | Synthesizer post-Phase-A | DRAFT picks a structural close per theme; rotates day-over-day |

### 2. `discovery_audit` fields (also in ctx.json)

| Field | Purpose |
|---|---|
| `phase_b_ran`, `pdfs_in_window`, `pdfs_with_contextual_mentions`, `total_mentions` | Phase B run stats |
| `promoted`, `near_miss` | Phase B cluster decisions with reasons |
| `promoted[].member_banks` | NEW 2026-08-04 — per-member ground-truth attribution: `{member_string: [banks that actually said it]}`. The routine MUST source stance attribution from this map, never by cycling the cluster's bank union — round-robin attribution let DRAFT print "Bank X argues Y" where X never said Y |
| `two_tier_merges`, `two_tier_augment_count` | Two-tier merge outcomes |
| `two_tier_cap_blocked` | Cap-fire log — sibling_canonicals is the consumable form of this |
| `sibling_groups` | Connected-component view of cap-blocked siblings |
| `pdf_dedup` | NEW — raw vs deduped PDF count + drop reasons |
| `contrarian_scan` | NEW — match count, bank count, promoted flag, signal labels |

### 3. `scripts/pulse_lint.py` exit code contract

Exit codes are the **single signal** for whether SCRUB should be dispatched:

| Exit | Meaning | Routine should |
|---:|---|---|
| `0` | Clean — no lint issues | **SKIP SCRUB** |
| `1` | Input file missing / config error | Surface error, skip dispatch |
| `2` | Invalid CLI args | Same |
| `3` | Hard issues found | **DISPATCH SCRUB** |
| `4` | Soft issues only (jargon-bare, top-3-theme-missing) | SCRUB dispatch is **optional** |

Also writes a sidecar JSON next to `<output_json>`:

```json
// <output_json>.decision
{
    "scrub_recommended": false,
    "hard_issue_count": 0,
    "soft_issue_count": 2,
    "total_issue_count": 2,
    "reason": "2 soft issue(s) only — routine may surface but SCRUB rewrite optional",
    "exit_code": 4,
    "soft_kinds": ["discovered-theme-missing", "jargon-bare", "slot-lean-overlap", "slot-stat-overlap", "top-3-theme-missing"]
}
```

`soft_kinds` (NEW 2026-08-04) is the authoritative soft-issue list — the
routine filters SCRUB's input to hard issues with it and must NEVER
re-derive the classification inline (the old re-derivation treated only
the top-3-coverage kind as soft, disagreed with these five kinds, and
dispatched SCRUB on soft-only lint).

Routine should **read the sidecar OR the exit code**, never both — they're
guaranteed consistent. SCRUB dispatching when `scrub_recommended: false`
is the recurring 2026-05-28+29+06-01 QC flag and a wasted Gemini call.

### 4. `scripts/pulse_draft_validate.py` exit code contract (NEW)

Runs AFTER DRAFT against `(draft.md, ctx.json)`:

| Exit | Meaning | Routine should |
|---:|---|---|
| `0` | DRAFT passes all structural checks | Proceed to STITCH |
| `3` | Hard violation (duplicate sibling sections, contrarian buried) | **Re-roll DRAFT** with violations as feedback |
| `4` | Soft violation (underweighted not surfaced, stance-split not named, numeric-scope-drift) | Lint-warn, proceed; carry any `numeric-scope-drift` into EDIT as a verify-this |

Output JSON at `<output_json>`:

```json
{
    "violations": [
        {"kind": "duplicate-sibling-sections", "severity": "hard",
         "message": "...", "theme": "...", "sibling": "..."},
        {"kind": "stance-split-no-named-debate", "severity": "soft", ...}
    ],
    "hard_count": 1,
    "soft_count": 1,
    "exit_code": 3
}
```

This is the structural fix for "the DRAFT layer can ignore the
theme_map advisory fields" — if DRAFT didn't honor sibling_canonicals
or contrarian_to_lead, the validator catches it and the routine re-rolls.

---

## Routine-side wiring checklist

Open the **Opus routine repo** and verify each of these. If any are
missing, that's why a QC recommendation keeps recurring:

- [x] **SCRUB dispatch reads `<lint_output>.decision`** and skips when
      `scrub_recommended: false`. DONE 2026-08-04 — STEP 5.7.1 now
      reads the sidecar and filters SCRUB's input to hard issues via
      its `soft_kinds`; the inline re-derivation that dispatched SCRUB
      on soft-only lint (the 2026-05-29 "basis points" → "hundredths
      of a percent" cosmetic-regression class) is gone.

- [x] **Final structural re-validation (STEP 5.75)** — DONE 2026-08-04:
      `pulse_draft_validate.py` re-runs on `/tmp/final.md` after
      EDIT + SCRUB (before the `## _LEANS` strip). NEW hard violations
      get one FIXUP pass; a deleted `## _LEANS` is spliced back from
      the draft deterministically; residuals commit to
      `pulse-output/lint/<ts>.final-validation.json` so they are
      visible to QC rather than shipping silently. Rationale: EDIT
      injects live market data and SCRUB rewrites the full document —
      until this step, everything they introduced shipped unchecked.

- [ ] **DRAFT prompt template renders `sibling_canonicals` as
      sub-bullets** under their primary theme (already shipped on
      synthesizer side — verify DRAFT actually reads the structure).

- [ ] **DRAFT prompt template surfaces `underweighted_candidate`
      themes** as a distinct category, with instruction to surface
      ≥1 as a WATCH bullet or threaded.

- [ ] **DRAFT prompt template surfaces `contrarian_to_lead` themes**
      as a distinct category, with explicit instruction NOT to fold
      into lead's bear-case appendix. The `contrarian_signal_labels`
      and `contrarian_titles` fields give DRAFT the specific frames
      to write.

- [ ] **DRAFT prompt template uses `close_style` per theme** — the
      synthesizer assigns a rotating style (bull_risk_resolution,
      falsifiable_window, ranked_list, single_question, asymmetry)
      and theme_coverage renders the assignment. DRAFT needs to honor
      the assignment to break the identical-template fatigue.

- [ ] **DRAFT prompt instructs Bank-A-vs-Bank-B naming** when a theme
      has supportive ≥2 AND skeptical ≥2. Stance counts are already
      in theme_coverage; the prompt just needs to require their use.

- [ ] **`pulse_draft_validate.py` runs after DRAFT** and re-rolls on
      hard violations (or surfaces a re-roll request).

- [ ] **AUDIT sub-agent's `falsifiable_prediction.bank` validator**
      whitelists agency names (IEA, Reuters, Bloomberg, EIA, OPEC) OR
      rewrites the rule to "fact source must be a real entity (bank
      OR agency)." 2026-06-01 lost two 4-bank themes (`middle east
      conflict impacts`, `US Strategic Petroleum Reserve drawdown`)
      to the IEA-not-in-bank-names error.

- [ ] **Phase B 2-bank promotion** path for high-sim Tier-1
      institutional themes (this side could ship; deferred pending
      decision on threshold tradeoff). Until shipped, expect
      "Kevin Warsh Fed Chair nomination" class themes to keep
      dropping silently.

---

## Why this doc exists

Every prior QC review that recurred had a recommendation living on
the consumer side of one of these contracts. The recurring failures:

- SCRUB on 0-lint input (3+ runs) — consumer not reading the exit code
- Sibling-pair shipping as duplicate sections (1 run; structurally
  vulnerable) — consumer reads sibling_canonicals advisory but no
  enforcement until pulse_draft_validate runs
- Identical close template (3+ runs) — consumer not honoring close_style
- Bank-vs-bank disagreement missing (2 runs) — consumer not driving
  Bank-A-vs-Bank-B naming off stance counts
- Underweighted candidates silently dropped (2 runs) — consumer
  treating them as suggestions

The cross-repo seam is the recurring root cause. This doc + the
draft validator are the structural fix: contracts are explicit,
enforcement is automatic, recurrence requires conscious removal of
the gate rather than silent drift.
