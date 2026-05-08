# Daily Market Pulse Synthesis Routine (with Adjudication)

> **Version-controlled source for the Claude.ai scheduled-routine prompt.**
> When you edit this file, copy the body (everything after the `---` divider) into the live routine config on Claude.ai. Diff against this file before pushing changes; it is the canonical record.
>
> **Secrets:** the `${GH_TOKEN}` placeholder below is filled in by the live routine config on Claude.ai — do NOT paste the actual token into this file. The token is a fine-grained PAT with write access to `gabjew90/Institutional-report-bot`; rotate it whenever it leaves a controlled environment.

**Routine surface assumptions (verified before this prompt was written):**
- Bash + Python 3 available.
- `Agent` tool available with `subagent_type` (general-purpose used here) and parallel dispatch (multiple Agent calls in one tool block).
- File I/O via `/tmp/...` paths persists for the duration of the fire.

---

Daily Market Pulse synthesis using GitHub as the message bus. **PRODUCTION RUN** — commits without `target_channels` filter so the pulse goes to all configured Discord channels.

Pipeline stages in this fire:

1. **Adjudicate** the top themes via parallel sub-agents. Each sub-agent sees only one theme's evidence and emits structured JSON. Lint rejects any sub-agent output that fabricates evidence quotes, bank attributions, or stance counts.
2. **DRAFT** the analytical pulse from research analyses + the adjudicated themes block.
3. **STITCH + EDIT** the draft (mechanical normalization + AUDIT sub-agent fresh-eyes editorial pass).
4. **LINT + SCRUB** voice rules (deterministic regex scan + lint-driven sub-agent rewrite, max 2 iterations).
5. **Commit** the pulse + adjudication + intermediate artifacts.
6. **QC self-review** — sub-agent reviews this run end-to-end and writes a structured critique. Non-blocking; pulse is already posted by then.

Files committed during the run:
- `pulse-output/pending/<ts>.md` — the pulse markdown (consumed by the bridge worker, posted to Discord)
- `pulse-output/pending-adjudications/<ts>.json` — the adjudication audit/diff artifact (matched to the pulse by base name, archived alongside the pulse markdown by the worker)
- `pulse-output/drafts/<ts>.md`, `pulse-output/stitched/<ts>.md`, `pulse-output/scrubbed/<ts>.md` — intermediate forensics artifacts (see STEP 6)
- `pulse-output/lint/<ts>.json` — lint report (issue count + breakdown)
- `pulse-output/qc-reviews/<ts>.md` — post-run QC self-review (see STEP 7)

**Constants**
```
REPO: gabjew90/Institutional-report-bot
BRANCH: pulse-data
GH_TOKEN: ${GH_TOKEN}
```

## STEP 1 — Read prompts

`cat ai_analysis/prompts.py` and locate `ADJUDICATION_SYSTEM`, `ADJUDICATION_USER`, `DRAFT_SYSTEM`, `DRAFT_USER`, `AUDIT_SYSTEM`, `AUDIT_USER`, `SCRUB_SYSTEM`, `SCRUB_USER`, `QC_SYSTEM`, `QC_USER` (full triple-quoted strings).

Follow the prompts verbatim. Particularly important rules to triple-check before commit:

- **Plain English by default.** No 'duration', 'breakevens', 'long-end', 'GICS', 'consensus underweights', 'navigate', 'leverage' (verb), 'robust', 'delve' without immediate translation.
- **No source-prefix story-connectors.** ZERO 'The Market Ear says/noted/adds', 'Mizuho keeps hammering', 'Goldman's mid-day color', 'JPM's morning desk', '[Bank] [verb]s that...' patterns. The bank name only earns ink when paired with a specific number/call. Walk every sentence opener after writing and strip preambles.
- **No subheadings inside insights.** Only the italicized one-line punchline at the top is structure. NO '**The Setup:**', '**Key data:**', '**Bottom line:**', '**Trade Implication:**', '**Hint:**'.
- **No em-dashes (—). No semicolons (;).** Use commas, periods, parentheses, or split into two sentences.
- **No AI-tells.** Strip 'it's worth noting', 'importantly', 'notably', 'meanwhile', 'moreover', 'that said', 'of course', 'overall', 'in summary'.
- **Theme coherence.** Every sentence in a theme body must serve the theme's central thesis.
- **'What drove the tape' = breaking market news only.** Geopolitics with measurable reaction, big earnings already reported, big macro prints already released. NOT scheduled events like Treasury QRA (those go in WHAT TO WATCH).
- **Session-aware framing.** The `session_status` field tells you the mode. At 9 AM ET, you're in PRE-MARKET. Traditional ETF %s in the snapshot are YESTERDAY'S close. Frame as 'Heading into today's open' / '$SPY closed yesterday at...'. Crypto trades 24/7 — frame as live.
- **Insight body structure (5 movements, NO labels in output).** Italicized one-line punchline, then 3-5 bullet data points (no header above), then mechanism prose paragraph (2-3 sentences arguing from bullets), then bull/pushback/defense/positioning paragraph with visible transition phrases ('The bull case...', 'The pushback we would anticipate...', 'Even granting that pushback...', 'The cleanest read...'). NO movement labels visible in output.
- **Trade variety across themes.** No two themes use the same primary instrument.
- **Foreign cashtag scrub.** $TSCO/$AD/$CNA/$BA/$BT/$RR/$III/$IMB/$CCL/$ORANGE/$REP/$ORA → names.
- **Cross-bank consensus must lead INSIGHTS** (top 3 by bank count must appear).

## STEP 2 — Fetch context

```bash
curl -sS -H "Authorization: token $GH_TOKEN" \
  https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/pulse-data/pulse-context/latest.json \
  -o /tmp/ctx.json
python3 -c 'import json; d=json.load(open("/tmp/ctx.json")); print("pdfs:", d["pdf_count"], "window:", d.get("window_label"), "session:", d.get("session_status"), "themes:", len(d.get("theme_map", {})))'
```

Replace `$GH_TOKEN` with the actual token above.

## STEP 3 — Inspect theme coverage

```bash
python3 -c 'import json; print(json.load(open("/tmp/ctx.json"))["theme_coverage"])'
```

## STEP 3.5 — Adjudicate selected themes (parallel sub-agents)

### Step 3.5.1 — Prepare per-theme inputs

```bash
python3 << 'PYEOF'
import json
import re

ctx = json.load(open('/tmp/ctx.json'))
analyses = json.loads(ctx['analyses_json'])
# Defensive: theme_map was added to the context payload alongside this
# routine. If a fire happens before the next dump-context cycle includes
# it, fall through with no themes — adjudication writes an empty file
# and DRAFT runs as before.
theme_map = ctx.get('theme_map') or {}
if not theme_map:
    print('WARNING: theme_map missing from context payload — skipping adjudication this fire')
    import sys; sys.exit(0)

# Rank themes for selection: bank count first, then PDF count, with a small
# nudge for net-directional themes (supportive - skeptical). Keep the floor
# permissive (banks >= 2) so cross-bank theme tail isn't lost; the lint pass
# will discard themes that can't be adjudicated cleanly anyway.
def weight(info):
    return (
        info.get('banks', 0)
        + 0.1 * (info.get('supportive', 0) - info.get('skeptical', 0))
        + 0.05 * info.get('pdfs', 0)
    )

ranked = sorted(
    [(t, info) for t, info in theme_map.items() if info.get('banks', 0) >= 2],
    key=lambda kv: -weight(kv[1]),
)

# Top 8 themes is the upper bound — real selection often comes back smaller
# after lint. INSIGHTS in the final pulse target 4-6 themes; we adjudicate a
# few more so the prose layer has headroom.
selected = ranked[:8]
print(f"Selected {len(selected)} themes for adjudication:")
for theme, info in selected:
    print(f"  - {theme}: {info.get('banks',0)} banks / {info.get('pdfs',0)} PDFs / "
          f"stance {info.get('supportive',0)}-{info.get('skeptical',0)}-{info.get('neutral',0)}")

# For each selected theme, collect ONLY that theme's evidence:
#   - theme_stances entries whose theme matches (case-insensitive, stripped)
#   - tension_points whose theme matches
#   - key_data_points from PDFs that contributed a stance to this theme
#     (data points carry no theme label themselves; sourcing PDFs are the
#     proxy for relevance)
def norm(s: str) -> str:
    """Mirror report.synthesizer._normalize_theme_tag exactly so per-theme
    matching against theme_map keys works.

    theme_map keys are produced by _normalize_theme_tag in synthesizer.py:
    lowercase, drop leading articles, replace hyphens/slashes/underscores
    with spaces, strip non-word punctuation, collapse whitespace. The raw
    theme_stances[*].theme labels in analyses_json keep hyphens and casing
    ('AI hyperscaler capex super-cycle'). Without mirroring the full
    normalization, the per-theme filter silently matches zero stances for
    every multi-word hyphenated theme (e.g. 'super-cycle', 'rate-cut',
    're-accelerating') and adjudication runs vacuously.
    """
    if not s:
        return ''
    t = s.lower().strip()
    for art in ('the ', 'a ', 'an '):
        if t.startswith(art):
            t = t[len(art):]
    t = re.sub(r'[/\-_]', ' ', t)
    t = re.sub(r'[^\w\s]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

inputs_per_theme = {}
for theme_name, info in selected:
    tn = norm(theme_name)
    theme_stances = []
    tension_points = []
    key_data_points = []
    contributing_sources = set()

    for a in analyses:
        src = a.get('source') or 'Unknown'
        contributed = False
        for ts in a.get('theme_stances', []) or []:
            if norm(ts.get('theme')) == tn:
                theme_stances.append({
                    'source': src,
                    'theme': ts.get('theme'),
                    'stance': ts.get('stance'),
                    'conviction': ts.get('conviction'),
                    'key_argument': ts.get('key_argument'),
                    'primary_instruments': ts.get('primary_instruments') or [],
                    'vs_consensus': ts.get('vs_consensus'),
                    'evidence': ts.get('evidence'),
                })
                contributed = True
        for tp in a.get('tensions', []) or []:
            if norm(tp.get('theme')) == tn:
                tension_points.append({
                    'source': src,
                    'theme': tp.get('theme'),
                    'bull_case': tp.get('bull_case'),
                    'bear_case': tp.get('bear_case'),
                    'what_invalidates': tp.get('what_invalidates'),
                })
                contributed = True
        if contributed:
            contributing_sources.add(src)

    # Data points from any PDF that contributed evidence to this theme
    for a in analyses:
        if (a.get('source') or 'Unknown') in contributing_sources:
            for kdp in a.get('data_points', []) or []:
                key_data_points.append({
                    'source_bank': kdp.get('source_bank') or a.get('source'),
                    'figure': kdp.get('figure'),
                    'metric': kdp.get('metric'),
                    'context': kdp.get('context'),
                })

    inputs_per_theme[theme_name] = {
        'theme': theme_name,
        'stance_counts': {
            'supportive': info.get('supportive', 0),
            'skeptical': info.get('skeptical', 0),
            'neutral': info.get('neutral', 0),
        },
        'theme_stances': theme_stances,
        'tension_points': tension_points,
        'key_data_points': key_data_points,
    }

# Per-theme match diagnostics — surfaces silent vacuous-match bugs (e.g.,
# theme_map vs theme_stances normalization mismatches) by reporting the
# actual evidence counts each theme picked up.
print(f"\nAdjudication input diagnostics:")
empty_themes = []
for theme_name, inputs in inputs_per_theme.items():
    n_st = len(inputs['theme_stances'])
    n_tp = len(inputs['tension_points'])
    n_dp = len(inputs['key_data_points'])
    print(f"  {theme_name}: {n_st} stances, {n_tp} tensions, {n_dp} data_points")
    if n_st == 0:
        empty_themes.append(theme_name)
if empty_themes:
    print(f"\nWARNING: {len(empty_themes)} theme(s) matched ZERO stances — likely a normalization mismatch between theme_map keys and theme_stances labels. Themes: {empty_themes}")

# Early-exit guard: prune themes with no usable inputs BEFORE dispatching
# sub-agents. A theme with zero stances/tensions/data_points cannot
# produce a meaningful adjudication — the sub-agent would emit empty
# arrays and the lint would pass vacuously (every rule has nothing to
# validate against). Prune saves ~10s per empty theme of latency and
# turns silent failures into explicit drops.
pruned = {
    t: i for t, i in inputs_per_theme.items()
    if len(i['theme_stances']) > 0
       or len(i['tension_points']) > 0
       or len(i['key_data_points']) > 0
}
dropped_for_emptiness = [t for t in inputs_per_theme if t not in pruned]
if dropped_for_emptiness:
    print(f"\nDropping {len(dropped_for_emptiness)} theme(s) before dispatch — no usable inputs: {dropped_for_emptiness}")
inputs_per_theme = pruned

with open('/tmp/adjudication_inputs.json', 'w') as f:
    json.dump(inputs_per_theme, f, indent=1)
print(f"\nWrote adjudication inputs for {len(inputs_per_theme)} themes -> /tmp/adjudication_inputs.json")
PYEOF
```

### Step 3.5.2 — Dispatch parallel sub-agents (one per theme)

For each theme key in `/tmp/adjudication_inputs.json`, dispatch a `general-purpose` Agent in parallel. Build the sub-agent prompt by combining `ADJUDICATION_SYSTEM` + `ADJUDICATION_USER` (after substitution) from `ai_analysis/prompts.py`.

Substitution into `ADJUDICATION_USER`:
- `{theme_label}` ← theme name
- `{stance_counts_json}` ← `json.dumps(inputs_per_theme[theme]['stance_counts'])`
- `{theme_inputs_json}` ← `json.dumps({k: inputs_per_theme[theme][k] for k in ('theme_stances','tension_points','key_data_points')}, indent=1)`

Dispatch all selected themes' Agent calls in **one tool block** so they run in parallel. Each sub-agent receives the assembled prompt and returns ONLY the JSON adjudication object. Save each sub-agent's text response to `/tmp/adj_raw_<theme_slug>.json` (slug = theme lowercased, spaces→`_`, non-alphanumeric stripped).

### Step 3.5.3 — Lint and assemble

```bash
python3 << 'PYEOF'
import json, os, re, datetime, glob

ctx = json.load(open('/tmp/ctx.json'))
inputs_per_theme = json.load(open('/tmp/adjudication_inputs.json'))

def slug(theme: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', theme.lower()).strip('_')

validated = []
discarded = []

for theme, inputs in inputs_per_theme.items():
    raw_path = f'/tmp/adj_raw_{slug(theme)}.json'
    if not os.path.exists(raw_path):
        discarded.append({'theme': theme, 'reason': 'sub_agent_no_output'})
        continue

    raw = open(raw_path).read().strip()
    # Sub-agents sometimes emit fenced JSON; strip a leading/trailing fence.
    if raw.startswith('```'):
        raw = re.sub(r'^```[a-zA-Z]*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
    try:
        adj = json.loads(raw)
    except json.JSONDecodeError as e:
        discarded.append({'theme': theme, 'reason': f'json_decode: {e}'})
        continue

    # Lint -------------------------------------------------------------------
    valid_evidence = {ts['evidence'] for ts in inputs['theme_stances']
                       if ts.get('evidence')}
    valid_sources = {ts.get('source') for ts in inputs['theme_stances']
                     if ts.get('source')}
    valid_what_invalidates = ' || '.join(
        (tp.get('what_invalidates') or '') for tp in inputs['tension_points']
    )
    valid_data_strings = ' || '.join(
        f"{kdp.get('figure','')} | {kdp.get('metric','')} | {kdp.get('context','')}"
        for kdp in inputs['key_data_points']
    )

    fail_reason = None

    # Rule 0: a theme with empty inputs cannot legitimately produce non-empty
    # output. Defense in depth — the early-exit guard at the end of 3.5.1
    # should prevent this from reaching the sub-agent at all, but if a
    # theme somehow slipped through with empty inputs AND the agent emitted
    # any facts/predictions, the only honest reading is fabrication.
    inputs_empty = (
        not inputs['theme_stances']
        and not inputs['tension_points']
        and not inputs['key_data_points']
    )
    output_nonempty = (
        bool(adj.get('facts_agreed'))
        or bool(adj.get('facts_contested'))
        or bool(adj.get('falsifiable_predictions'))
    )
    if inputs_empty and output_nonempty:
        fail_reason = 'inputs empty but adjudication has non-empty output arrays — fabrication suspected'

    # Rule 1: evidence_quotes must exact-match a theme_stances.evidence
    if not fail_reason:
        pass  # fall through to existing checks below
    for fa in adj.get('facts_agreed', []) or []:
        for q in fa.get('evidence_quotes', []) or []:
            if q not in valid_evidence:
                fail_reason = f'facts_agreed evidence_quote not in inputs: {q!r}'
                break
        if fail_reason: break
    if not fail_reason:
        for fc in adj.get('facts_contested', []) or []:
            for k in ('for_evidence', 'against_evidence'):
                v = fc.get(k)
                if v and v not in valid_evidence:
                    fail_reason = f'facts_contested {k} not in inputs: {v!r}'
                    break
            if fail_reason: break

    # Rule 2: banks must appear in input sources
    if not fail_reason:
        for fa in adj.get('facts_agreed', []) or []:
            for bank in fa.get('banks_for', []) or []:
                if bank not in valid_sources:
                    fail_reason = f'facts_agreed banks_for not in input sources: {bank!r}'
                    break
            if fail_reason: break
    if not fail_reason:
        for fc in adj.get('facts_contested', []) or []:
            for k in ('banks_for', 'banks_against'):
                for bank in fc.get(k, []) or []:
                    if bank not in valid_sources:
                        fail_reason = f'facts_contested {k} not in input sources: {bank!r}'
                        break
                if fail_reason: break
            if fail_reason: break

    # Rule 3: falsifiable_predictions must trace
    if not fail_reason:
        for pred in adj.get('falsifiable_predictions', []) or []:
            claim = pred.get('claim') or ''
            if claim and (claim not in valid_what_invalidates and
                          claim not in valid_data_strings):
                fail_reason = f'falsifiable_prediction claim not in inputs: {claim!r}'
                break
            bank = pred.get('bank')
            if bank and bank not in valid_sources:
                fail_reason = f'falsifiable_prediction bank not in inputs: {bank!r}'
                break

    # Rule 4: stance_counts must match
    if not fail_reason:
        if adj.get('stance_counts') != inputs['stance_counts']:
            fail_reason = (f"stance_counts mismatch: "
                           f"{adj.get('stance_counts')} vs {inputs['stance_counts']}")

    # Rule 5: facts_agreed entries must have >= 2 banks_for. A "fact agreed"
    # by a single bank is just "what one bank said" — not consensus, and
    # already covered by per-PDF inputs. Drop single-bank entries in place
    # rather than failing the whole theme; if every entry was single-bank,
    # facts_agreed becomes [] and the rest of the adjudication still ships.
    if not fail_reason and adj.get('facts_agreed'):
        before = len(adj['facts_agreed'])
        adj['facts_agreed'] = [
            fa for fa in adj['facts_agreed']
            if len(fa.get('banks_for') or []) >= 2
        ]
        dropped = before - len(adj['facts_agreed'])
        if dropped:
            print(f"  facts_agreed: dropped {dropped} single-bank entr{'y' if dropped == 1 else 'ies'} from {theme!r}")

    # Rule 6: falsifiable_predictions[*].deadline must be either an ISO
    # date prefix OR a short conditional substring. Catches the failure
    # mode where the sub-agent puts a description ("Anthropic backlog
    # account for GOOGL") in the deadline field. Filter bad entries in
    # place rather than failing the theme.
    if not fail_reason and adj.get('falsifiable_predictions'):
        before = len(adj['falsifiable_predictions'])
        def _deadline_ok(pred):
            dl = (pred.get('deadline') or '').strip()
            if not dl:
                return False
            # ISO date at the start (with optional rest like time)
            if re.match(r'^\d{4}-\d{2}-\d{2}', dl):
                return True
            # Short conditional/relative — must be <= 30 chars and look
            # like a deadline cue, not a noun phrase description. The
            # description-shape heuristic: contains an article + verb-ish
            # word combination ("the X of Y", "for Y", "account for Y").
            if len(dl) > 40:
                return False
            description_smell = (
                ' for ' in dl.lower()
                or 'account' in dl.lower()
                or 'estimate' in dl.lower()
                or dl.lower().startswith('the ')
            )
            if description_smell:
                return False
            return True
        adj['falsifiable_predictions'] = [
            p for p in adj['falsifiable_predictions'] if _deadline_ok(p)
        ]
        dropped = before - len(adj['falsifiable_predictions'])
        if dropped:
            print(f"  falsifiable_predictions: dropped {dropped} entr{'y' if dropped == 1 else 'ies'} with bad deadline format from {theme!r}")

    # Final emptiness check after Rule 5 / 6 filtering — if the entire
    # adjudication block is now empty (no agreed, no contested, no
    # predictions), the theme adjudication has no remaining signal value.
    # Discard it so DRAFT doesn't waste prompt budget on an empty block.
    if not fail_reason:
        post_filter_empty = (
            not adj.get('facts_agreed')
            and not adj.get('facts_contested')
            and not adj.get('falsifiable_predictions')
        )
        if post_filter_empty:
            fail_reason = 'after Rule 5/6 filtering, theme has no remaining content (single-bank facts dropped, bad deadlines dropped)'

    if fail_reason:
        discarded.append({'theme': theme, 'reason': fail_reason})
    else:
        validated.append(adj)

adj_file = {
    'pulse_date': ctx.get('today'),
    'window_label': ctx.get('window_label', ''),
    'themes': validated,
    'discarded_themes': discarded,
}
with open('/tmp/adjudication.json', 'w') as f:
    json.dump(adj_file, f, indent=2)

print(f"Adjudication: {len(validated)} validated, {len(discarded)} discarded")
for d in discarded:
    print(f"  DISCARDED {d['theme']}: {d['reason']}")
PYEOF
```

If `validated` is empty, log a warning and continue — DRAFT will fall back to its existing per-PDF JSON inputs. The pulse still ships.

## STEP 4 — Generate DRAFT (Stage 1)

Apply `DRAFT_USER` substitutions and `DRAFT_SYSTEM`. **If `/tmp/adjudication.json` exists and its `themes` array is non-empty**, inject the adjudicated themes block as added structured input that the prose can use to ground its claims. **If it doesn't exist, or its `themes` array is empty** (e.g., adjudication was skipped due to missing `theme_map`, or every theme failed lint), skip the injection entirely and run DRAFT against the existing per-PDF JSON inputs only — the pulse still ships with no degradation in surface output, just without the structured adjudication grounding.

```
ADJUDICATED THEMES (use these consensus_view, facts_agreed, and falsifiable_predictions
to ground the prose; do not contradict them; do not invent stronger claims than the
evidence supports):

<contents of /tmp/adjudication.json's `themes` array, pretty-printed>
```

Save to `/tmp/draft.md` via Python.

## STEP 5 — Stitch + Edit (Stage 2, two sub-passes)

The post-DRAFT pass is split into a deterministic STITCH (Python, no LLM) followed by a judgment-based EDIT dispatched as a separate sub-agent for fresh-eyes review. Splitting these forces each pass to do one job well: STITCH cannot accidentally drop a theme; EDIT cannot accidentally miss a foreign cashtag.

### Step 5a — STITCH (mechanical fixes, no LLM)

```bash
python3 scripts/pulse_stitch.py /tmp/draft.md /tmp/stitched.md
```

Foreign cashtag scrub (`$TSCO` → "Tesco", `$CNA` → "Centrica", etc.) and ETF normalization (`$SPX` → `$SPY`, `$NDX` → `$QQQ`, `$RUT` → `$IWM`). Single source of truth: `scripts/pulse_stitch.py` constants. The script prints what it changed; review the log briefly to confirm nothing surprising got rewritten.

### Step 5b — EDIT (judgment-based, dispatched as a single sub-agent)

EDIT runs in a fresh `general-purpose` Agent session — NOT in the orchestrator's accumulated context. The sub-agent has no DRAFT history, no adjudication memory, just the stitched pulse + live data. That fresh-eyes property is the entire reason for the dispatch: an in-context AUDIT re-reads its own work; an out-of-context EDIT actually reviews.

Build the sub-agent prompt by combining `AUDIT_SYSTEM` + `AUDIT_USER` (with substitutions) from `ai_analysis/prompts.py`. Substitute `{draft_markdown}` with the contents of `/tmp/stitched.md` (post-STITCH, NOT the raw `/tmp/draft.md`). All other substitutions (`{today}`, `{now}`, `{session_status}`, `{market_snapshot}`, `{news_snapshot}`, `{earnings_calendar}`, `{economic_calendar}`) come from `/tmp/ctx.json`.

Dispatch ONE Agent call with the assembled prompt. The sub-agent applies the full AUDIT pipeline (RECAP rebuild, Pass A cull, Pass A.5 density, Pass B close, voice scrub) and returns the revised markdown. Save the response to `/tmp/final.md`.

Do not pass any tools to the sub-agent — it doesn't need file access; the prompt is fully self-contained.

## STEP 5.5 — Lint final markdown (deterministic regex scan)

Mechanical check before commit. Single source of truth: `ai_analysis/voice_rules.py` defines the banned-phrase / banned-punctuation / source-prefix lists; both the AUDIT prompt and this linter import from there. Updating a banned pattern in voice_rules.py propagates to both — no drift.

The repo is already cloned in the routine sandbox via `session_context.sources`, so `scripts/pulse_lint.py` runs directly with `python3` and resolves its `from ai_analysis.voice_rules import ...` against the cloned tree.

```bash
python3 scripts/pulse_lint.py /tmp/final.md /tmp/lint_report.json /tmp/ctx.json
```

The script prints a human-readable summary inline (issue count, breakdown by kind, first 20 examples with line + snippet). Full structured issues are written to `/tmp/lint_report.json` for STEP 6 to commit alongside the pulse.

## STEP 5.7 — Voice scrub (sub-agent dispatch, lint-driven)

If STEP 5.5's lint report has any HARD issues (any `kind` other than `top-3-theme-missing`), dispatch a SCRUB sub-agent whose ONLY job is to rewrite the flagged sentences. SCRUB does not add or remove themes, change facts, or restructure paragraphs — it walks the lint report and rewrites the specific flagged sentences in place.

This is the layer that closes the voice-enforcement gap: the EDIT sub-agent handles editorial judgment but doesn't reliably iterate over every sentence to enforce voice rules; SCRUB has no other concerns competing for attention and is driven by structured lint output rather than self-supervision.

### Step 5.7.1 — Decide whether to dispatch

Read `/tmp/lint_report.json`. Count the issues whose `kind` is NOT `top-3-theme-missing`:

```bash
python3 -c "
import json
issues = json.load(open('/tmp/lint_report.json'))
hard = [i for i in issues if i.get('kind') != 'top-3-theme-missing']
print(f'hard issues: {len(hard)}')
print(f'soft issues: {len(issues) - len(hard)}')
"
```

If hard issues == 0, SKIP STEP 5.7 entirely (proceed to STEP 6). If hard issues > 0, continue to 5.7.2.

### Step 5.7.2 — Dispatch SCRUB sub-agent

Build the SCRUB prompt by combining `SCRUB_SYSTEM` + `SCRUB_USER` (with substitutions) from `ai_analysis/prompts.py`. Substitutions into `SCRUB_USER`:

- `{issue_count}` ← number of hard issues (computed in 5.7.1)
- `{lint_report_json}` ← contents of `/tmp/lint_report.json` (the full report — SCRUB filters to hard issues itself)
- `{pulse_markdown}` ← contents of `/tmp/final.md`

Dispatch ONE `general-purpose` Agent with the assembled prompt. The sub-agent runs in fresh context (no DRAFT/EDIT history), sees only the pulse markdown + the structured lint report, and returns the rewritten markdown.

Save the sub-agent's response to `/tmp/scrubbed.md` first (so we keep a copy of the SCRUB output for forensics), then overwrite `/tmp/final.md` with the same content (so STEP 6's commit picks up the scrubbed version).

### Step 5.7.3 — Re-lint and decide on retry

Re-run the lint scan against the scrubbed markdown:

```bash
python3 scripts/pulse_lint.py /tmp/final.md /tmp/lint_report.json /tmp/ctx.json
```

Check the new hard-issue count:

- **0 hard issues** → great, proceed to STEP 6.
- **>0 hard issues, but fewer than before** → SCRUB made progress. Dispatch ONE more SCRUB pass (same prompt, fresh UUID, with the new lint report). Re-lint. Accept whatever lint reports after this second pass — proceed to STEP 6 even if residuals exist. The residual lint report ships with the pulse for inspection.
- **>0 hard issues, no progress** → log `WARNING: SCRUB did not reduce lint issues` and proceed to STEP 6 anyway. Don't loop forever — the pulse must ship.

**Maximum 2 SCRUB iterations.** If lint still has hard issues after the second pass, commit the pulse with the residual lint report; don't block delivery on perfect voice compliance.

If validated SCRUB output is materially different from the EDIT output, that's good — the system is doing its job. If SCRUB returns nearly the same markdown, that's a sign the sub-agent didn't engage with the lint report; flag it in STEP 7's report so we can debug.

If lint reports issues, the SCRUB pass (above) is supposed to handle them automatically — manual rewriting of `/tmp/final.md` is no longer the workflow. The lint report is mechanical and trusted; SCRUB is the agent that acts on it.

## STEP 6 — Compose with frontmatter and commit BOTH files (PRODUCTION — ALL CHANNELS)

```bash
python3 << 'PYEOF'
import json, base64, urllib.request, datetime, os
GH_TOKEN = os.environ.get('GH_TOKEN', '')  # filled by the routine surface
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'

ctx = json.load(open('/tmp/ctx.json'))
final_md = open('/tmp/final.md').read()

input_tokens_est = 120000
output_tokens_est = max(1, len(final_md) // 4)

import os
frontmatter_lines = [
    '---',
    f'pdf_count: {ctx["pdf_count"]}',
    f'input_tokens: {input_tokens_est}',
    f'output_tokens: {output_tokens_est}',
    f'dumped_at_utc: {ctx.get("dumped_at_utc", "")}',
]
# === TEST/PROD via TARGET_CHANNELS env var ===
# Default (env var unset): no target_channels line emitted -> all configured
# channels (production behavior). For test fires, the routine body itself
# (the wrapper prompt on Claude.ai, NOT this markdown) sets
# `export TARGET_CHANNELS='test-channel'` before invoking us, and the bridge
# worker filters Discord delivery to channels matching that substring.
# Switching test/prod is therefore a RemoteTrigger update on the routine
# body, not an edit to this file -- no git push, no recomment-before-cron
# trap. This file always stays in prod-safe state.
if os.environ.get('TARGET_CHANNELS'):
    frontmatter_lines.append(f"target_channels: {os.environ['TARGET_CHANNELS']}")
frontmatter_lines.append('---')
frontmatter = '\n'.join(frontmatter_lines) + '\n\n'

file_content = frontmatter + final_md
ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
# Persist `ts` for later steps (QC review). Without this, STEP 7 would
# compute a fresh timestamp ~tens-of-seconds later and the QC review
# filename would no longer pair with the pulse markdown filename for
# forensic cross-reference.
with open('/tmp/pulse_ts.txt', 'w') as f:
    f.write(ts)
pulse_path = f'pulse-output/pending/{ts}.md'
adj_path = f'pulse-output/pending-adjudications/{ts}.json'

def commit(path: str, content_bytes: bytes, message: str):
    body = {
        'message': message,
        'content': base64.b64encode(content_bytes).decode(),
        'branch': BRANCH,
    }
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/contents/{path}',
        data=json.dumps(body).encode(),
        headers={
            'Authorization': f'token {GH_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        method='PUT',
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())

# Commit pulse markdown FIRST. The bridge worker matches by base name when
# fetching the sibling adjudication file, so committing the adjudication
# first risks a worker poll cycle picking up the pulse before the
# adjudication has propagated. Markdown then JSON keeps that ordering safe
# (worker only acts on .md files in pending/).
result = commit(pulse_path, file_content.encode(), f'routine: pulse {ts} ({ctx["pdf_count"]} PDFs)')
print('committed pulse:', pulse_path)
print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])

# Commit adjudication JSON. Worker will match this to the pulse by base name
# when it next polls and archive both atomically.
adj_content = open('/tmp/adjudication.json').read().encode()
result = commit(adj_path, adj_content, f'routine: adjudication {ts}')
print('committed adjudication:', adj_path)
print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])

# Commit /tmp/draft.md as the pre-AUDIT artifact. Diff against the final
# pulse to measure what AUDIT changed — invaluable for tuning the AUDIT
# prompt and detecting regressions (e.g., AUDIT silently dropping a theme
# DRAFT included, or vice versa). The bridge worker doesn't process this
# directory; it's an audit/forensics record only.
import os
if os.path.exists('/tmp/draft.md'):
    draft_path = f'pulse-output/drafts/{ts}.md'
    draft_content = open('/tmp/draft.md').read().encode()
    result = commit(draft_path, draft_content, f'routine: pre-stitch draft {ts}')
    print('committed draft:', draft_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/draft.md — skipping draft commit (DRAFT step did not save it)')

# Commit /tmp/stitched.md as the post-STITCH / pre-EDIT artifact. Together
# with /tmp/draft.md and the final pulse, this gives a three-stage
# forensics chain: draft -> stitched -> final. Diff stitched vs final
# isolates the LLM's editorial changes from the mechanical preprocessing.
if os.path.exists('/tmp/stitched.md'):
    stitched_path = f'pulse-output/stitched/{ts}.md'
    stitched_content = open('/tmp/stitched.md').read().encode()
    result = commit(stitched_path, stitched_content, f'routine: post-stitch pre-edit {ts}')
    print('committed stitched:', stitched_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/stitched.md — skipping stitched commit (STITCH step did not run)')

# Commit /tmp/lint_report.json so the issue count + pattern breakdown
# travels with the pulse. Useful for tracking voice/quality drift over
# time — a sudden spike in one pattern category is a signal the prompts
# need tightening.
if os.path.exists('/tmp/lint_report.json'):
    lint_path = f'pulse-output/lint/{ts}.json'
    lint_content = open('/tmp/lint_report.json').read().encode()
    result = commit(lint_path, lint_content, f'routine: lint report {ts}')
    print('committed lint:', lint_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/lint_report.json — skipping lint commit')

# Commit /tmp/scrubbed.md if STEP 5.7 dispatched a SCRUB pass. This is
# the post-EDIT pre-final artifact (intermediate between EDIT output and
# the final pulse). Diff scrubbed.md vs the final pulse markdown to see
# what SCRUB rewrote — if they're nearly identical, SCRUB didn't engage
# with the lint findings and that's a quality signal worth investigating.
if os.path.exists('/tmp/scrubbed.md'):
    scrubbed_path = f'pulse-output/scrubbed/{ts}.md'
    scrubbed_content = open('/tmp/scrubbed.md').read().encode()
    result = commit(scrubbed_path, scrubbed_content, f'routine: post-edit scrubbed {ts}')
    print('committed scrubbed:', scrubbed_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/scrubbed.md — SCRUB pass was skipped (zero hard lint issues)')

print(f'pdf_count: {ctx["pdf_count"]}, output_tokens_est: {output_tokens_est}, target: ALL configured channels (production)')
PYEOF
```

## STEP 7 — QC self-review (sub-agent dispatch)

After the pulse + adjudication + intermediate artifacts are committed in STEP 6, dispatch a QC sub-agent to review this run end-to-end and produce a structured critique. The critique is committed to `pulse-output/qc-reviews/<ts>.md` so a human reviewer can read it later and decide what to change for the next run.

This step is **non-blocking and must not be skipped on errors**. The pulse has already been committed and the bridge worker will post it to Discord regardless. If QC fails, log the failure and proceed to STEP 8 — never block delivery on a failed QC.

### Step 7.1 — Build QC inputs

```bash
python3 << 'PYEOF'
import json, os, datetime

ctx = json.load(open('/tmp/ctx.json'))

# Adjudication file (may be empty if STEP 3.5 produced no validated themes)
adj_file = {}
if os.path.exists('/tmp/adjudication.json'):
    try:
        adj_file = json.load(open('/tmp/adjudication.json'))
    except Exception:
        adj_file = {}

# Lint report (final, post-SCRUB if SCRUB ran). Cap at 50 entries to keep
# the QC prompt under control on lint-heavy runs.
lint_report = []
if os.path.exists('/tmp/lint_report.json'):
    try:
        lint_report = json.load(open('/tmp/lint_report.json'))
    except Exception:
        lint_report = []
lint_summary = lint_report[:50]

# Intermediate artifacts. Each missing artifact gets a placeholder so the
# QC prompt always has the same shape.
def _read_or(path: str, default: str) -> str:
    if os.path.exists(path):
        try:
            return open(path).read()
        except Exception:
            return default
    return default

draft_md = _read_or('/tmp/draft.md', '(draft not produced)')
stitched_md = _read_or('/tmp/stitched.md', '(stitched not produced)')
final_md = _read_or('/tmp/final.md', '(final not produced)')

# Phase-B discovery audit: what the discovery layer promoted vs near-miss.
# Empty if the synthesizer's discovery_audit was not surfaced in the
# context payload (older context dumps), in which case QC reviews
# coverage qualitatively without the structured data.
discovery_audit = ctx.get('discovery_audit') or {}

# Adjudication discard reasons in compact form
discarded = adj_file.get('discarded_themes', []) or []
discard_reasons = '; '.join(
    f"{(d.get('theme') or '?')}: {(d.get('reason') or '?')}"
    for d in discarded
) or '(none)'

# Use the SAME timestamp the pulse was committed under so the QC review
# filename pairs cleanly with the pulse markdown filename.
ts = open('/tmp/pulse_ts.txt').read().strip() if os.path.exists('/tmp/pulse_ts.txt') \
     else datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')

qc_inputs = {
    'timestamp': ts,
    'theme_coverage': ctx.get('theme_coverage', '(missing)'),
    'discovery_audit_json': json.dumps(discovery_audit, indent=1),
    'n_validated': len(adj_file.get('themes', []) or []),
    'n_discarded': len(discarded),
    'discard_reasons': discard_reasons,
    'lint_summary_json': json.dumps(lint_summary, indent=1),
    'draft_md': draft_md,
    'stitched_md': stitched_md,
    'final_md': final_md,
}
with open('/tmp/qc_inputs.json', 'w') as f:
    json.dump(qc_inputs, f, indent=1)

print(f"QC inputs prepared:")
print(f"  promoted discoveries: {len(discovery_audit.get('promoted', []) or [])}")
print(f"  near-miss clusters:   {len(discovery_audit.get('near_miss', []) or [])}")
print(f"  lint issues in scope: {len(lint_summary)} (of {len(lint_report)} total)")
print(f"  adjudication: {qc_inputs['n_validated']} validated, {qc_inputs['n_discarded']} discarded")
PYEOF
```

### Step 7.2 — Dispatch QC sub-agent

Build the QC prompt by combining `QC_SYSTEM` + `QC_USER` (with substitutions from `/tmp/qc_inputs.json`) from `ai_analysis/prompts.py`. Substitute every placeholder in `QC_USER`:

- `{timestamp}`
- `{theme_coverage}`
- `{discovery_audit_json}`
- `{n_validated}`, `{n_discarded}`, `{discard_reasons}`
- `{lint_summary_json}`
- `{draft_md}`, `{stitched_md}`, `{final_md}`

Dispatch ONE `general-purpose` Agent with the assembled prompt. The sub-agent runs in fresh context — no DRAFT/EDIT/SCRUB history, just the artifacts the prompt provides. It returns the QC review markdown (no preamble, no JSON wrapper).

Save the sub-agent's response to `/tmp/qc_review.md`.

If the sub-agent errors out OR returns empty content, log a warning and proceed to STEP 7.3 (which will skip the commit cleanly). Do NOT retry — the pulse is already posted and a missing QC review is recoverable; a stuck QC retry blocking STEP 8 confirmation is not.

### Step 7.3 — Commit QC review

```bash
python3 << 'PYEOF'
import os, base64, urllib.request, json

GH_TOKEN = os.environ.get('GH_TOKEN', '')
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'

if not os.path.exists('/tmp/qc_review.md'):
    print('no /tmp/qc_review.md — QC sub-agent produced no output, skipping commit')
    raise SystemExit(0)

content_str = open('/tmp/qc_review.md').read().strip()
if not content_str:
    print('QC review file is empty — skipping commit')
    raise SystemExit(0)

ts = open('/tmp/pulse_ts.txt').read().strip() if os.path.exists('/tmp/pulse_ts.txt') \
     else json.load(open('/tmp/qc_inputs.json'))['timestamp']
qc_path = f'pulse-output/qc-reviews/{ts}.md'

body = {
    'message': f'routine: QC review {ts}',
    'content': base64.b64encode(content_str.encode()).decode(),
    'branch': BRANCH,
}
req = urllib.request.Request(
    f'https://api.github.com/repos/{REPO}/contents/{qc_path}',
    data=json.dumps(body).encode(),
    headers={
        'Authorization': f'token {GH_TOKEN}',
        'Accept': 'application/vnd.github+json',
        'Content-Type': 'application/json',
        'X-GitHub-Api-Version': '2022-11-28',
    },
    method='PUT',
)
with urllib.request.urlopen(req, timeout=30) as resp:
    result = json.loads(resp.read())
print('committed QC review:', qc_path)
print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
PYEOF
```

## STEP 8 — Confirm

Report:
- Pulse filename + commit sha
- Adjudication filename + commit sha
- pdf_count and final word count
- Top 3 INSIGHT theme headers
- Adjudication summary: `<N> validated, <N> discarded` (and the discard reasons if any — those are signals worth surfacing)
- QC review filename + commit sha (or "QC review skipped" if STEP 7 didn't commit)

## Critical rules

- Synthesis happens in YOUR reasoning. No external LLM calls except the parallel adjudication sub-agents in Step 3.5.2, which use the `Agent` tool.
- Use Python for ALL file I/O.
- Follow ALL the rules above.
- If any step fails, report what failed and stop. The pulse markdown commit at the end is the gate — don't commit a pulse you weren't able to fully verify.
- The adjudication file is paired to the pulse by base name (`<ts>.md` ↔ `<ts>.json`). Use the same `ts` for both commits; do not regenerate the timestamp between them.
