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

Three-stage pipeline in this fire:

1. **Adjudicate** the top themes via parallel sub-agents. Each sub-agent sees only one theme's evidence and emits structured JSON. Lint rejects any sub-agent output that fabricates evidence quotes, bank attributions, or stance counts.
2. **DRAFT** the analytical pulse from research analyses + the adjudicated themes block.
3. **AUDIT** the draft against live market data, news, and the calendar. Final readability pass.

Two files commit at the end:
- `pulse-output/pending/<ts>.md` — the pulse markdown (consumed by the bridge worker, posted to Discord)
- `pulse-output/pending-adjudications/<ts>.json` — the adjudication audit/diff artifact (matched to the pulse by base name, archived alongside the pulse markdown by the worker)

**Constants**
```
REPO: gabjew90/Institutional-report-bot
BRANCH: pulse-data
GH_TOKEN: ${GH_TOKEN}
```

## STEP 1 — Read prompts

`cat ai_analysis/prompts.py` and locate `ADJUDICATION_SYSTEM`, `ADJUDICATION_USER`, `DRAFT_SYSTEM`, `DRAFT_USER`, `AUDIT_SYSTEM`, `AUDIT_USER` (full triple-quoted strings).

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

## STEP 5 — Generate AUDIT (Stage 2)

Apply `AUDIT_USER` substitutions and `AUDIT_SYSTEM`. Apply ALL rules. Final readability pass: walk every sentence in INSIGHTS bodies. For any opener of '[Bank] [verb]s that...' (Market Ear says, Mizuho keeps hammering, Goldman's mid-day color, etc.), rewrite. Move the attribution to a parenthetical at sentence end or strip it entirely. Save to `/tmp/final.md`.

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
    result = commit(draft_path, draft_content, f'routine: pre-audit draft {ts}')
    print('committed draft:', draft_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/draft.md — skipping draft commit (DRAFT step did not save it)')

print(f'pdf_count: {ctx["pdf_count"]}, output_tokens_est: {output_tokens_est}, target: ALL configured channels (production)')
PYEOF
```

## STEP 7 — Confirm

Report:
- Pulse filename + commit sha
- Adjudication filename + commit sha
- pdf_count and final word count
- Top 3 INSIGHT theme headers
- Adjudication summary: `<N> validated, <N> discarded` (and the discard reasons if any — those are signals worth surfacing)

## Critical rules

- Synthesis happens in YOUR reasoning. No external LLM calls except the parallel adjudication sub-agents in Step 3.5.2, which use the `Agent` tool.
- Use Python for ALL file I/O.
- Follow ALL the rules above.
- If any step fails, report what failed and stop. The pulse markdown commit at the end is the gate — don't commit a pulse you weren't able to fully verify.
- The adjudication file is paired to the pulse by base name (`<ts>.md` ↔ `<ts>.json`). Use the same `ts` for both commits; do not regenerate the timestamp between them.
