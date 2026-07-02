# Daily Market Pulse Synthesis Routine (with Adjudication)

> **Live source for the Claude.ai scheduled-routine prompt — auto-fetched.**
> The production routine prompt on Claude.ai is the small bootstrap in `routine-bootstrap.md`. Every fire, it `curl`s THIS file from the working branch and executes it verbatim. **You do not need to paste anything into Claude.ai when you edit this file** — push to the working branch and the next fire picks it up. The bootstrap was set up once; after that, this file is the canonical, auto-deployed source.
>
> **If you need to change the branch the bootstrap fetches from, OR rotate the GitHub PAT**, edit `routine-bootstrap.md` and paste its body into the live routine config. That's the only time you touch the live prompt.
>
> **Secrets:** the `${GH_TOKEN}` reference in the Constants block below is documentation — the live value is the env var that the bootstrap exports at fire start. Do NOT paste the actual token into this file. The token is a fine-grained PAT with write access to `gabjew90/Institutional-report-bot`; rotate it whenever it leaves a controlled environment.

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

---

## ⚠️ COMMIT TRANSPORT — READ BEFORE ANY COMMIT (environment override, 2026-06-30)

**The PAT / `api.github.com` write path is BLOCKED in this execution environment.** This routine runs behind the Claude Code agent proxy, which rejects every `api.github.com` request with `HTTP 403 — "GitHub access is not enabled for this session. An org admin must connect the Claude GitHub App for this organization."` — *before the PAT is even evaluated*. The fine-grained PAT is correctly scoped (all repos, Contents read/write) but **inert here**. Confirmed on 2026-06-30 across three consecutive fires. This is NOT a token problem and NOT transient — do not waste retries on it, and do not report it as a missing file or a bad token.

**Therefore: perform EVERY commit through the `mcp__github__*` GitHub tools, NOT the `urllib`/`curl` PUT.** Those tools route through the connected GitHub App, which DOES have write access in this session — that is how `pulse-context/latest.json` reads succeed and how the 2026-06-30 failure marker was committed. You have already done exactly this once; do it for every artifact.

**This OVERRIDES the commit mechanics in the per-step heredocs below.** Concretely:
- The Python heredocs that build artifacts stay authoritative for **what** to commit — the destination `path`, the file **content**, `branch: pulse-data`, and the commit **message**. Each step already writes its artifact to a `/tmp` file (`/tmp/final.md` for the pulse, `/tmp/draft.md`, `/tmp/scrubbed.md`, `/tmp/lint_report.json`, the adjudication JSON, etc.).
- For each commit the routine specifies, **call the GitHub MCP create-or-update-file tool** (or a multi-file push tool if your toolset has one, to land several artifacts in one commit) with `owner: gabjew90`, `repo: Institutional-report-bot`, `branch: pulse-data`, the `path`, the file content (read it from the step's `/tmp` file), and the message. If a commit's content exists only in heredoc memory, have the heredoc dump it to a `/tmp` file first, then commit that file via MCP.
- Do **NOT** execute the heredoc's `urllib`/`curl` PUT and do **NOT** retry it after a 403 — it cannot succeed here. The PAT heredocs are retained only as the content spec; the write goes through MCP.
- The load-bearing commit is `pulse-output/pending/<ts>.md` (STEP 6) — the bridge worker posts ONLY what lands there. If that MCP commit fails, the pulse does not ship: treat it as terminal and write the failure marker (also via MCP).

If a future environment restores direct PAT access this override is harmless — the heredocs still describe the correct commits, and either transport may be used.

---

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

## STEP 2 — Fetch context (with retry + failure reporting)

The fetch is the highest-risk failure point in the routine because it depends on (a) the bridge worker having recently dumped pulse-context, (b) GitHub raw cache having propagated, and (c) network reachability from the routine sandbox. We retry with exponential backoff and commit a failure marker on terminal failure so the human reviewer sees a structured cause rather than an opaque 404.

This step also bootstraps the routine's observability layer:
- `/tmp/routine.log` — append-only log; tee'd from each step's stdout/stderr. Read by `commit_failure` for context. Truncated at start.
- `/tmp/pulse_ts.txt` — pulse timestamp. Used by progress + commit + QC steps so all artifacts pair on the same `<ts>`.
- `/tmp/progress.py` — helper that subsequent steps invoke after major stages to commit progress events to `pulse-output/progress/<ts>.json`.

```bash
# Truncate routine log at fire start. Subsequent step heredocs pipe their
# stdout+stderr through `tee -a /tmp/routine.log` so commit_failure can
# include the last N lines as failure context.
: > /tmp/routine.log

python3 << 'PYEOF' 2>&1 | tee -a /tmp/routine.log
import urllib.request, urllib.error, time, os, json, base64, datetime, traceback, glob

# Env vars do NOT persist across Bash tool calls — the routine body's
# `export GH_TOKEN=...` sets it in one ephemeral shell; each subsequent
# step runs in a fresh shell with empty env. The body therefore writes
# /tmp/gh_token.txt (and /tmp/target_channels.txt) on startup; every step
# reads from those files as the canonical source. We still consult the
# env var first for backward compat with bodies that haven't been updated.
def _read_token() -> str:
    v = (os.environ.get('GH_TOKEN') or '').strip()
    if v:
        return v
    try:
        return open('/tmp/gh_token.txt').read().strip()
    except FileNotFoundError:
        return ''

def _read_target_channels() -> str:
    v = (os.environ.get('TARGET_CHANNELS') or '').strip()
    if v:
        return v
    try:
        return open('/tmp/target_channels.txt').read().strip()
    except FileNotFoundError:
        return ''

GH_TOKEN = _read_token()
if not GH_TOKEN:
    print('FATAL: no GH_TOKEN in env or /tmp/gh_token.txt — body did not bootstrap auth correctly')
    raise SystemExit(2)

# Re-export so any subprocess this Python invokes (and the rest of this
# heredoc's logic) sees a consistent value. This does NOT propagate to
# the next Bash tool call — that's why we also keep the file copy.
os.environ['GH_TOKEN'] = GH_TOKEN
tc = _read_target_channels()
if tc:
    os.environ['TARGET_CHANNELS'] = tc

URL = 'https://raw.githubusercontent.com/gabjew90/Institutional-report-bot/pulse-data/pulse-context/latest.json'
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'

def _tail(path: str, n: int = 120) -> str:
    """Return the last N lines of a file, or '(missing)' if absent."""
    try:
        with open(path, 'r', errors='replace') as f:
            lines = f.readlines()
        return ''.join(lines[-n:])
    except FileNotFoundError:
        return '(missing)'
    except Exception as e:
        return f'(read error: {e})'

def _tmp_listing() -> str:
    """List /tmp/ artifacts with sizes + mtimes — shows which steps got
    far enough to write their outputs before failure.
    """
    try:
        rows = []
        for p in sorted(glob.glob('/tmp/*')):
            try:
                st = os.stat(p)
                size = st.st_size
                mt = datetime.datetime.utcfromtimestamp(st.st_mtime).strftime('%H:%M:%SZ')
                rows.append(f'  {mt}  {size:>9}  {p}')
            except Exception:
                rows.append(f'  ???        ?         {p}')
        return '\n'.join(rows) or '(empty)'
    except Exception as e:
        return f'(listing error: {e})'

def _progress_events() -> str:
    """Return the progress events committed so far for this pulse (if any).

    Reads pulse-output/progress/<ts>.json from GitHub. The file is the
    routine's own running log of step completions; including it in the
    failure marker shows exactly which steps ran cleanly before the abort.
    """
    try:
        ts = open('/tmp/pulse_ts.txt').read().strip()
    except Exception:
        return '(no /tmp/pulse_ts.txt — fire failed before STEP 2 completed setup)'
    try:
        url = f'https://raw.githubusercontent.com/{REPO}/{BRANCH}/pulse-output/progress/{ts}.json'
        req = urllib.request.Request(url, headers={'Authorization': f'token {GH_TOKEN}'})
        with urllib.request.urlopen(req, timeout=8) as resp:
            d = json.load(resp)
        ev = d.get('events') or []
        if not ev:
            return '(no progress events committed yet)'
        return '\n'.join(f"  {e.get('time','?')}  {e.get('step','?')}  {e.get('detail','')}" for e in ev)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return '(no progress file — fire failed before any step committed progress)'
        return f'(progress fetch HTTP {e.code})'
    except Exception as e:
        return f'(progress fetch error: {e})'

def _env_summary() -> str:
    """Selected env vars relevant to routine state. Token presence shown
    as ('set'|'missing'), never the value.
    """
    keys = ['GH_TOKEN', 'TARGET_CHANNELS', '_ROUTINE_REPO', '_ROUTINE_BRANCH', 'PWD', 'HOSTNAME']
    rows = []
    for k in keys:
        v = os.environ.get(k)
        if k == 'GH_TOKEN':
            rows.append(f'  {k}={"set" if v else "MISSING"}')
        else:
            rows.append(f'  {k}={v if v else "(unset)"}')
    return '\n'.join(rows)

def commit_failure(stage: str, reason: str, detail: str = '') -> None:
    """Write a rich failure marker to pulse-output/qc-reviews/<ts>.md.

    The marker captures everything a human or automated watcher needs to
    diagnose WHY the routine aborted without spelunking the Claude.ai
    routine session log:

      - stage + reason + Python traceback (the immediate exception)
      - last 120 lines of /tmp/routine.log (tee'd output from prior steps)
      - /tmp/ file listing (which artifacts existed at failure time)
      - committed progress events (which steps ran cleanly before abort)
      - env summary (token presence, target_channels, etc.)

    Idempotent: if the commit itself fails, we swallow — the routine's own
    session log still records the original failure. Stops bad cascading.
    """
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
    log_tail = _tail('/tmp/routine.log', 120)
    tmp_listing = _tmp_listing()
    progress_events = _progress_events()
    env_summary = _env_summary()
    body = f"""# QC Review — {ts}

## Status: FAILED at {stage}

- **Time (UTC):** {ts}
- **Stage:** {stage}
- **Reason:** {reason}

## Exception traceback

```
{detail}
```

## Last 120 lines of /tmp/routine.log

```
{log_tail}
```

## /tmp artifacts at failure time

```
{tmp_listing}
```

## Progress events committed before failure

```
{progress_events}
```

## Environment summary

```
{env_summary}
```
"""
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{REPO}/contents/pulse-output/qc-reviews/{ts}.md',
            data=json.dumps({
                'message': f'routine: FAILURE at {stage} ({ts})',
                'content': base64.b64encode(body.encode()).decode(),
                'branch': BRANCH,
            }).encode(),
            headers={
                'Authorization': f'token {GH_TOKEN}',
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
            },
            method='PUT',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f'committed failure marker: pulse-output/qc-reviews/{ts}.md')
    except Exception as e:
        print(f'WARNING: could not commit failure marker: {e}')


def fetch_with_retry(url: str, attempts: int = 3) -> bytes:
    """Fetch with exponential backoff. 404 may be transient when the bridge
    just committed pulse-context and GitHub raw cache hasn't propagated.
    """
    delays = [5, 15, 30]
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                headers={'Authorization': f'token {GH_TOKEN}'},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            print(f'attempt {i+1}/{attempts}: HTTP {e.code} {e.reason}')
            if i < attempts - 1 and e.code in (404, 502, 503, 504):
                time.sleep(delays[i])
                continue
            raise
        except Exception as e:
            last_err = e
            print(f'attempt {i+1}/{attempts}: {type(e).__name__}: {e}')
            if i < attempts - 1:
                time.sleep(delays[i])
                continue
            raise
    raise RuntimeError(f'fetch_with_retry exhausted: {last_err}')


# Persist the failure-commit helper for later steps that may need it. Stored
# as a JSON-serializable shim — we re-define commit_failure inline whenever
# a downstream step needs it (see STEP 6, STEP 7), but the LOGIC is here.
# A copy of GH_TOKEN + REPO + BRANCH lives in env so subsequent steps don't
# need to re-derive it.
os.environ['_ROUTINE_REPO'] = REPO
os.environ['_ROUTINE_BRANCH'] = BRANCH


# Generate the pulse timestamp NOW (rather than at STEP 6 commit time) so
# progress events for this fire all key off the same ts. STEP 6 reads this
# instead of generating its own. /tmp/pulse_ts.txt is the source of truth.
pulse_ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
with open('/tmp/pulse_ts.txt', 'w') as f:
    f.write(pulse_ts)


# Write /tmp/progress.py — a small helper subsequent steps invoke after
# major stages to commit a progress event to pulse-output/progress/<ts>.json.
# Read-modify-write with SHA tracking; one accumulating file per pulse.
# Watcher polls this file and surfaces the latest event for live visibility
# into a 20-min routine run.
PROGRESS_HELPER = '''import sys, os, json, base64, urllib.request, urllib.error, datetime
if len(sys.argv) < 2:
    sys.exit(0)
step = sys.argv[1]
detail = sys.argv[2] if len(sys.argv) > 2 else ""
try:
    ts = open("/tmp/pulse_ts.txt").read().strip()
except Exception:
    sys.exit(0)
GH_TOKEN = os.environ.get("GH_TOKEN", "")
REPO = "gabjew90/Institutional-report-bot"
BRANCH = "pulse-data"
path = f"pulse-output/progress/{ts}.json"
url = f"https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}"
events = []
sha = None
try:
    req = urllib.request.Request(url, headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.load(resp)
        sha = d.get("sha")
        try:
            events = json.loads(base64.b64decode(d["content"]).decode()).get("events", [])
        except Exception:
            events = []
except urllib.error.HTTPError as e:
    if e.code != 404:
        pass
except Exception:
    pass
now = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
events.append({"step": step, "time": now, "detail": detail})
new_content = json.dumps({"ts": ts, "events": events}, indent=1)
body = {"message": f"routine: progress {step}", "content": base64.b64encode(new_content.encode()).decode(), "branch": BRANCH}
if sha:
    body["sha"] = sha
try:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/contents/{path}", data=json.dumps(body).encode(), headers={"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"progress: {step}")
except Exception as e:
    print(f"WARNING: progress commit failed for {step}: {e}")
'''
with open('/tmp/progress.py', 'w') as f:
    f.write(PROGRESS_HELPER)


try:
    body = fetch_with_retry(URL)
    with open('/tmp/ctx.json', 'wb') as f:
        f.write(body)
    ctx = json.loads(body)
    print(
        f"pdfs: {ctx['pdf_count']}  "
        f"window: {ctx.get('window_label')}  "
        f"session: {ctx.get('session_status')}  "
        f"themes: {len(ctx.get('theme_map', {}))}"
    )
    discovery = ctx.get('discovery_audit') or {}
    print(
        f"discovery: {len(discovery.get('promoted', []) or [])} promoted, "
        f"{len(discovery.get('near_miss', []) or [])} near-miss"
    )
except Exception as e:
    detail = traceback.format_exc()
    commit_failure(
        stage='STEP 2 (fetch context)',
        reason=f'{type(e).__name__}: {e}',
        detail=detail,
    )
    raise SystemExit(1)
PYEOF
```

If the fetch fails after all retries, the routine commits a failure marker to `pulse-output/qc-reviews/<ts>.md` and aborts with `SystemExit(1)`. Subsequent steps (DRAFT, EDIT, etc.) do not run; downstream `pulse-output/pending/` stays empty.

After STEP 2 succeeds, commit a progress event so the watcher sees the routine is past the highest-risk failure point:

```bash
python3 /tmp/progress.py "STEP_2_DONE"
```

### STEP 2.5 — Press-time freshness check (mandatory)

The context is a SNAPSHOT from `dumped_at_utc`; the pulse posts at fire time. Anything that happened in between — most importantly an 8:30 AM ET data print before a ~9:05 post — is invisible to the snapshot, and the calendar inside it still says "upcoming" for events that have since occurred. (2026-07-02 failure: the dump job froze at 09:25 UTC, the pulse consumed 4-hour-stale context and told readers to WATCH the 8:30 payrolls print at 9:06 AM.) Reconcile at press time:

```bash
python3 << 'PYEOF' 2>&1 | tee -a /tmp/routine.log
import json, datetime, re
ctx = json.load(open('/tmp/ctx.json'))
now = datetime.datetime.utcnow()
dumped = datetime.datetime.fromisoformat(
    (ctx.get('dumped_at_utc') or '').replace('Z', '')[:19]
)
age_min = (now - dumped).total_seconds() / 60
notes = []
if age_min > 75:
    et = dumped - datetime.timedelta(hours=4)  # EDT approximation
    notes.append(
        f"STALE SNAPSHOT: this context was captured {age_min:.0f} minutes "
        f"ago (~{et.strftime('%-I:%M %p')} ET). Live prices, news, and "
        f"'this morning' framings in the context are AS OF THAT TIME. "
        f"Timestamp any live level you cite ('as of ~{et.strftime('%-I:%M %p')} "
        f"ET') and do not present them as the current tape."
    )
# Events whose scheduled ET time falls between the dump and NOW have
# already printed even though the snapshot's calendar says upcoming.
cal = ctx.get('economic_calendar') or ''
for line in cal.splitlines():
    m = re.match(r"\s*(\d{2})-(\d{2}) (\d{2}):(\d{2}) ET \| \[US\] ([^|]+)\|?", line)
    if not m:
        continue
    mo, dy, hh, mm, name = m.groups()
    try:
        sched_et = datetime.datetime(now.year, int(mo), int(dy), int(hh), int(mm))
        sched_utc = sched_et + datetime.timedelta(hours=4)
    except ValueError:
        continue
    if dumped < sched_utc <= now and 'ACTUAL=' not in line:
        notes.append(
            f"PRESS-TIME EVENT: '{name.strip()}' printed at {hh}:{mm} ET — "
            f"BEFORE this pulse posts but AFTER this snapshot. The actual "
            f"number is NOT in your context. Frame it as 'printed this "
            f"morning — number still propagating at press time; watch the "
            f"reaction', NEVER as upcoming, and NEVER invent or guess the "
            f"actual. In WHAT TO WATCH, the item is the REACTION, not the print."
        )
if notes:
    open('/tmp/press_time_note.txt', 'w').write(
        "[PRESS-TIME NOTE — binding, overrides the calendar's "
        "upcoming/released split]\n" + "\n".join(f"- {n}" for n in notes)
    )
    print("press-time note written:", len(notes), "item(s)")
else:
    print("press-time check clean — snapshot fresh, no straddled events")
PYEOF
```

**If `/tmp/press_time_note.txt` exists, append its full content to the DRAFT prompt input in STEP 4** (after the adjudication block, before the analyses). The note is binding on DRAFT's framing.

## STEP 3 — Inspect theme coverage

```bash
python3 -c 'import json; print(json.load(open("/tmp/ctx.json"))["theme_coverage"])'
python3 /tmp/progress.py "STEP_3_DONE"
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

# Phase A's embedding clustering produces canonical labels (`theme_map`
# keys) that may merge MULTIPLE raw stance labels into one cluster.
# Without the normalization map, this loop would only match stances
# whose normalized tag exactly equals a cluster key — losing every
# stance whose raw label was a cluster member but not the canonical
# itself. The 2026-05-08T22-50-50Z run hit this: the 8-bank "AI
# hyperscaler capex" cluster matched only 1 stance because 7 banks'
# raw labels (e.g. "AI capex", "hyperscaler buyback collapse",
# "AI infrastructure spending") all merged into one canonical key.
#
# Fix: build a reverse lookup `norm(stance.theme) → canonical_cluster_label`
# from the theme_normalization map the synthesizer surfaced. Match by
# resolving stance.theme through this map FIRST, then comparing to the
# theme_name we're collecting inputs for.
norm_to_canonical = (ctx.get('theme_normalization') or {}).get('norm_to_canonical') or {}

def stance_belongs_to(stance_theme: str, theme_name: str) -> bool:
    """Return True if a raw stance label maps to the same canonical
    cluster as `theme_name`. Falls back to direct normalized equality
    when the normalization map is empty (older context dumps that
    pre-date the theme_normalization field — keeps the routine
    backward-compatible)."""
    sn = norm(stance_theme)
    tn = norm(theme_name)
    if not sn or not tn:
        return False
    if sn == tn:
        return True
    # Resolve through cluster map: stance might be a cluster member
    # whose canonical label equals theme_name.
    canonical_for_stance = norm_to_canonical.get(sn)
    canonical_for_theme = norm_to_canonical.get(tn, tn)
    if canonical_for_stance and canonical_for_stance == canonical_for_theme:
        return True
    return False

inputs_per_theme = {}
for theme_name, info in selected:
    theme_stances = []
    tension_points = []
    key_data_points = []
    contributing_sources = set()

    for a in analyses:
        src = a.get('source') or 'Unknown'
        contributed = False
        for ts in a.get('theme_stances', []) or []:
            if stance_belongs_to(ts.get('theme', ''), theme_name):
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
            if stance_belongs_to(tp.get('theme', ''), theme_name):
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
        'discovered': bool(info.get('discovered')),
        'stance_counts': {
            'supportive': info.get('supportive', 0),
            'skeptical': info.get('skeptical', 0),
            'neutral': info.get('neutral', 0),
        },
        'theme_stances': theme_stances,
        'tension_points': tension_points,
        'key_data_points': key_data_points,
    }

# UNIFIED ADJUDICATION INPUT CONTRACT
#
# For EVERY theme, regardless of whether it's primary-Phase-A or
# discovery-Phase-B, regardless of stance count, the sub-agent's input
# is the union of:
#
#   1. Real Phase A stance entries — collected above via the
#      stance_belongs_to() loop. PDFs that stance-labeled this theme.
#
#   2. Phase B mention evidence — from EVERY Phase B cluster that maps
#      to this theme. Two sources:
#        - The cluster whose canonical IS this theme (discovery-promoted
#          themes; this is how Trump-Xi-class topics get evidence).
#        - Every "covered" near-miss cluster whose nearby_phase_a list
#          contains this theme (this is how heavyweight primary themes
#          with thin Phase A seeds get their 16-bank engagement reflected
#          in adjudication input, not just in theme_map bank counts).
#
# This replaces three earlier code paths (real-stance-only, discovery-
# fallback for empty themes, thin-primary augment for ≤2-stance themes)
# with one always-on rule. The bug it fixes:
#
#   Adjudication kept discarding heavyweight themes (16 banks Hormuz,
#   14 banks Trump-Xi) because their per-theme inputs looked like 1-bank
#   themes — only 1 PDF gave a stance, the other 15 banks just mentioned.
#   Rule 5 correctly applied the thin-theme filter to what was correctly
#   detected as thin INPUT. The bug was never Rule 5; it was the input
#   contract dropping 15 of 16 banks' content before adjudication saw it.
#
#   The fix is upstream: feed adjudication evidence proportional to the
#   actual bank engagement, not just what Phase A's stance-labeled PDFs
#   happened to carry. Once the input is correct, Rule 5 stops being
#   a forcing function for special cases and just works.
#
# Bank attribution: synthetic stance entries cycle across the union of
# all contributing banks (deduplicated across clusters). Members
# deduplicated by string identity so the same mention isn't surfaced
# twice from two different clusters covering the same topic.
discovery_promoted = (ctx.get('discovery_audit') or {}).get('promoted') or []
discovery_near_miss = (ctx.get('discovery_audit') or {}).get('near_miss') or []

# Build the lookup: theme_name -> list of Phase B clusters that map to it.
# Promoted clusters: their canonical IS the theme name.
# Covered near-miss clusters: their nearby_phase_a list contains the
# Phase A theme(s) they cover (one cluster can map to multiple themes
# when it bridges fragmented Phase A labels).
phase_b_clusters_by_theme: dict[str, list[dict]] = {}

def _add_phase_b_cluster(theme: str, cluster: dict) -> None:
    phase_b_clusters_by_theme.setdefault(theme, []).append(cluster)

for cluster in discovery_promoted:
    canonical = cluster.get('canonical')
    if canonical:
        _add_phase_b_cluster(canonical, cluster)

for cluster in discovery_near_miss:
    if cluster.get('reason') != 'covered':
        continue
    # Phase B's own promotion bar is 3 banks; below that the cluster is
    # too thin to be considered a real cross-bank signal. Reusing that
    # bar here for consistency — single- and two-bank "coverage" isn't
    # evidence worth augmenting from.
    if cluster.get('n_banks', 0) < 3:
        continue
    nearby = cluster.get('nearby_phase_a') or []
    for entry in nearby:
        # nearby_phase_a items are [label, sim] pairs in the audit JSON.
        label = entry[0] if isinstance(entry, (list, tuple)) else entry
        if label:
            _add_phase_b_cluster(label, cluster)

# Total real+synthetic cap per theme. Keeps the sub-agent prompt
# bounded; if real stances already fill the cap, synthetic augment
# is a no-op (no dilution). Headroom = MAX_TOTAL_STANCE_ENTRIES -
# real_count, computed per theme at augment time.
MAX_TOTAL_STANCE_ENTRIES = 25


def _synthesize_stances_from_clusters(
    target_inputs: dict,
    clusters: list[dict],
    theme_name: str,
    cap: int,
) -> int:
    """Append synthetic neutral-stance entries to theme_stances from one
    or more Phase B clusters that map to this theme. Members are
    deduplicated by string identity across clusters. Bank attribution
    cycles across the union of all contributing banks. Also folds in
    key_data_points from any PDF whose source bank is in the union.
    Returns the number of synthetic stance entries appended.
    """
    if cap <= 0 or not clusters:
        return 0
    seen_members: set[str] = set()
    pooled_members: list[str] = []
    all_banks: set[str] = set()
    for cluster in clusters:
        for b in (cluster.get('banks') or []):
            if b:
                all_banks.add(b)
        for m in (cluster.get('members') or []):
            if not m or m in seen_members:
                continue
            seen_members.add(m)
            pooled_members.append(m)
            if len(pooled_members) >= cap:
                break
        if len(pooled_members) >= cap:
            break

    if not pooled_members or not all_banks:
        return 0

    bank_list = sorted(all_banks)
    for i, member in enumerate(pooled_members):
        bank = bank_list[i % len(bank_list)]
        target_inputs['theme_stances'].append({
            'source': bank,
            'theme': theme_name,
            'stance': 'neutral',
            'conviction': None,
            'key_argument': member,
            'primary_instruments': [],
            'vs_consensus': None,
            'evidence': member,
        })

    # Fold in key_data_points from any PDF whose source bank is in the
    # union of contributing banks (same proxy as before — analyses_json
    # doesn't carry pdf_file_id, so bank-match is our best PDF filter).
    for a in analyses:
        if (a.get('source') or 'Unknown') in all_banks:
            for kdp in a.get('data_points', []) or []:
                target_inputs['key_data_points'].append({
                    'source_bank': kdp.get('source_bank') or a.get('source'),
                    'figure': kdp.get('figure'),
                    'metric': kdp.get('metric'),
                    'context': kdp.get('context'),
                })
    return len(pooled_members)


# Apply uniformly. No source-flag branching, no thin-stance threshold,
# no fully-empty special case.
#   - 5 real stances + no Phase B coverage  -> no-op, real stances stand alone.
#   - 5 real stances + 16-bank Phase B cover -> up to 20 synthetic added.
#   - 1 real stance  + 16-bank Phase B cover -> up to 24 synthetic added.
#   - 0 real stances + promoted Phase B     -> up to 25 synthetic added.
# Pruning below still drops themes with zero inputs after this loop.
for theme_name in list(inputs_per_theme):
    inputs = inputs_per_theme[theme_name]
    clusters = phase_b_clusters_by_theme.get(theme_name) or []
    if not clusters:
        continue
    n_real = len(inputs['theme_stances'])
    headroom = MAX_TOTAL_STANCE_ENTRIES - n_real
    if headroom <= 0:
        continue  # real stances already fill the cap
    appended = _synthesize_stances_from_clusters(
        inputs, clusters, theme_name, cap=headroom,
    )
    if appended:
        total_phase_b_banks = sum(c.get('n_banks', 0) for c in clusters)
        print(
            f"  unified augment for '{theme_name}': "
            f"{n_real} real stances + {appended} mention-derived "
            f"(from {len(clusters)} Phase B cluster(s), {total_phase_b_banks} pooled bank-entries)"
        )

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

    # Agency / wire-service / Tier-2 bank whitelist. Two distinct
    # categories pooled here as a fallback for when the adjudicator
    # correctly cites a source that isn't in this theme's
    # theme_stances:
    #
    # (a) Non-bank entities that produce market-moving data (oil
    #     supply, monetary policy, trade flows). The 2026-06-01 QC
    #     found two 4-bank discovered themes (`middle east conflict
    #     impacts`, `US Strategic Petroleum Reserve drawdown`)
    #     discarded for the identical error
    #     `falsifiable_prediction bank not in inputs: 'IEA'` —
    #     adjudicator correctly identified IEA as the oil-supply
    #     source, but IEA isn't a bank.
    #
    # (b) Known Tier-2 European bank desks that frequently appear in
    #     the corpus but may not land in theme_stances for every
    #     theme (Phase B contextual-mention promotion is capped at
    #     MAX_TOTAL_STANCE_ENTRIES headroom; if Phase A real stances
    #     already use the budget, the Tier-2 desk's mention doesn't
    #     make it into theme_stances even though it's in the corpus).
    #     The 2026-06-04 QC and 2026-06-08 QC both flagged the same
    #     false-positive class: Berenberg (4 PDFs in 06-08 corpus)
    #     discarded from `fed policy outlook` on `bank not in inputs:
    #     'Berenberg'` because the synthetic-stance budget had been
    #     consumed by other banks first. Same pattern hit Scotiabank
    #     on 06-04. Adding the known European Tier-2 desks to this
    #     fallback whitelist is conceptually a "known-source allowlist"
    #     rather than a strict agency list — the comment/name now
    #     reflects that.
    AGENCY_WHITELIST = frozenset({
        # (a) Agencies / wire services
        'IEA', 'International Energy Agency',
        'EIA', 'US EIA', 'Energy Information Administration',
        'OPEC', 'OPEC+',
        'Bloomberg', 'Reuters',
        'BIS', 'Bank for International Settlements',
        'IMF', 'International Monetary Fund',
        'World Bank',
        'BLS', 'Bureau of Labor Statistics',
        'BEA', 'Bureau of Economic Analysis',
        'Federal Reserve', 'FOMC', 'Fed',
        'ECB', 'BOJ', 'BoE', 'PBOC',
        'OECD', 'WTO',
        # (b) Known Tier-2 European + APAC bank desks that frequently
        # appear in the corpus via contextual mentions (Phase B) and
        # may not land in theme_stances on every theme due to the
        # synthetic-stance budget cap. These are real banks already
        # validated as input PDF sources on past pulses; allowlisting
        # prevents the "bank not in inputs" false-positive observed
        # on Berenberg (2026-06-08) and Scotiabank (2026-06-04).
        'Berenberg',
        'UniCredit',
        'Mizuho International', 'Mizuho',
        'Crédit Agricole CIB', 'Credit Agricole CIB', 'CA-CIB',
        'ANZ Research', 'ANZ',
        'SEB',
        'Lloyds', 'Lloyds Banking Group',
        'Scotiabank',
        'Nordea',
        'Danske Bank', 'Danske',
        'Rabobank',
        'ING',
        'BNP Paribas', 'BNP',
        'Société Générale', 'SocGen',
        'Natixis',
        'Commerzbank',
        'Standard Chartered', 'StanChart',
        'TS Lombard',
        'BCA Research', 'BCA',
        'The Market Ear', 'TME',
    })

    def _is_valid_source(name: str) -> bool:
        if not name:
            return True
        return name in valid_sources or name in AGENCY_WHITELIST

    # Rule 2: banks must appear in input sources (or agency whitelist)
    if not fail_reason:
        for fa in adj.get('facts_agreed', []) or []:
            for bank in fa.get('banks_for', []) or []:
                if not _is_valid_source(bank):
                    fail_reason = f'facts_agreed banks_for not in input sources or agency whitelist: {bank!r}'
                    break
            if fail_reason: break
    if not fail_reason:
        for fc in adj.get('facts_contested', []) or []:
            for k in ('banks_for', 'banks_against'):
                for bank in fc.get(k, []) or []:
                    if not _is_valid_source(bank):
                        fail_reason = f'facts_contested {k} not in input sources or agency whitelist: {bank!r}'
                        break
                if fail_reason: break
            if fail_reason: break

    # Rule 3: falsifiable_predictions must trace (banks or agency whitelist)
    if not fail_reason:
        for pred in adj.get('falsifiable_predictions', []) or []:
            claim = pred.get('claim') or ''
            if claim and (claim not in valid_what_invalidates and
                          claim not in valid_data_strings):
                fail_reason = f'falsifiable_prediction claim not in inputs: {claim!r}'
                break
            bank = pred.get('bank')
            if bank and not _is_valid_source(bank):
                fail_reason = f'falsifiable_prediction bank not in inputs or agency whitelist: {bank!r}'
                break

    # Rule 4: stance_counts must match
    if not fail_reason:
        if adj.get('stance_counts') != inputs['stance_counts']:
            fail_reason = (f"stance_counts mismatch: "
                           f"{adj.get('stance_counts')} vs {inputs['stance_counts']}")

    # Rule 5 (LOOSENED twice): facts_agreed entries are kept if ANY of
    #   (a) the fact itself has ≥2 banks_for, OR
    #   (b) the THEME has ≥2 banks supportive, OR
    #   (c) the THEME has ≥2 banks neutral (DISCOVERY branch — see below).
    #
    # First loosen (b) — the original rule (drop facts cited by only one
    # bank) silently nuked the most-covered theme on the 2026-05-08T23-33-34Z
    # run. The 8-bank `ai hyperscaler capex` theme had Goldman's $755B
    # number, UniCredit's $710-725B number, BofA's AI Big 10 stat — each
    # cited by only one bank but ALL backing a theme with 6 supportive
    # stances. Original Rule 5 dropped them all → theme had no remaining
    # content → discarded.
    #
    # Second loosen (c) — discovered themes (Phase B promoted) have
    # supportive=0 / neutral=N because no bank took a primary stance.
    # Branch (b) silently nukes them too. The 2026-05-13 Trump-Xi theme
    # (12 banks neutral, 0 supportive) lost every adjudicated fact under
    # the post-first-loosen rule and was dropped from the pulse. Branch (c)
    # treats high neutral count as equivalent consensus signal — banks
    # engaged with the topic at the mention level even without a stance.
    theme_supportive_count = (inputs.get('stance_counts') or {}).get('supportive', 0)
    theme_neutral_count = (inputs.get('stance_counts') or {}).get('neutral', 0)
    theme_consensus_floor = max(theme_supportive_count, theme_neutral_count)
    if not fail_reason and adj.get('facts_agreed'):
        before = len(adj['facts_agreed'])
        adj['facts_agreed'] = [
            fa for fa in adj['facts_agreed']
            if (
                len(fa.get('banks_for') or []) >= 2
                or theme_consensus_floor >= 2
            )
        ]
        dropped = before - len(adj['facts_agreed'])
        if dropped:
            print(f"  facts_agreed: dropped {dropped} single-bank entr{'y' if dropped == 1 else 'ies'} from {theme!r} (theme consensus floor: {theme_consensus_floor} = max(supportive={theme_supportive_count}, neutral={theme_neutral_count}))")

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

    # Self-discard: the model marked `selected: false` itself (the
    # ADJUDICATION_SYSTEM prompt now instructs it to do so when evidence
    # is too thin for honest commitment). Trust that signal.
    if not fail_reason and adj.get('selected') is False:
        fail_reason = 'self-discarded by adjudicator (insufficient evidence to commit)'

    # Ungrounded-claims check: facts_agreed has entries but NONE of them
    # carry a single verbatim evidence quote. That means the adjudicator
    # composed claims it couldn't ground in the corpus — exactly the
    # rubber-stamp failure mode we want to catch. Even one quote across
    # all entries is fine; zero across all entries is the signal.
    if not fail_reason:
        fa = adj.get('facts_agreed') or []
        if fa:
            total_quotes = sum(
                len(entry.get('evidence_quotes') or []) for entry in fa
            )
            if total_quotes == 0:
                fail_reason = 'ungrounded: facts_agreed has entries but no evidence_quotes across any of them'

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

```bash
python3 /tmp/progress.py "STEP_3_5_DONE"
```

## STEP 4 — Generate DRAFT (Stage 1)

Apply `DRAFT_USER` substitutions and `DRAFT_SYSTEM`. **If `/tmp/press_time_note.txt` exists (STEP 2.5), append its full content to the DRAFT input** — it is binding on how straddled data prints and stale live prices are framed. **If `/tmp/adjudication.json` exists and its `themes` array is non-empty**, inject the adjudicated themes block as added structured input that the prose can use to ground its claims. **If it doesn't exist, or its `themes` array is empty** (e.g., adjudication was skipped due to missing `theme_map`, or every theme failed lint), skip the injection entirely and run DRAFT against the existing per-PDF JSON inputs only — the pulse still ships with no degradation in surface output, just without the structured adjudication grounding.

```
ADJUDICATED THEMES (use these consensus_view, facts_agreed, and falsifiable_predictions
to ground the prose; do not contradict them; do not invent stronger claims than the
evidence supports):

<contents of /tmp/adjudication.json's `themes` array, pretty-printed>
```

Save to `/tmp/draft.md` via Python.

```bash
python3 /tmp/progress.py "STEP_4_DONE"
```

## STEP 4.5 — Structural validate DRAFT (deterministic check, mandatory)

Run `scripts/pulse_draft_validate.py` against `/tmp/draft.md` + `/tmp/ctx.json`. The validator checks for six classes of structural violation that the DRAFT prompt is supposed to prevent but historically hasn't reliably honored:

- **HARD `duplicate-sibling-sections`**: cap-blocked sibling theme pair shipped as two separate INSIGHTS sections instead of folded into one. The 2026-05-29 pulse shipped this; the 2026-06-01 fix held but the fold instruction is advisory at the prompt level — this is the code-level enforcement.
- **HARD `contrarian-buried-in-appendix`**: a `contrarian_to_lead` theme exists in theme_map but wasn't given its own INSIGHTS section or WATCH bullet. The 2026-06-01 corpus had 5 contrarian titles all folded into the AI bear-case appendix; the contrarian detector ships them as a distinct category but DRAFT can still bury them.
- **HARD `main-event-lean-missing`**: the MAIN EVENT (lead `###` theme) proposes a trade whose instruments are absent from the `## _LEANS` block, so the board (built mechanically from _LEANS) silently drops the pulse's headline call. The 2026-06-26 pulse shipped a lead "Long $RSP/$IWM, $GLD" rotation that never reached the TRADE BOARD because _LEANS listed only the briefs' trades.
- **HARD `leans-block-missing`**: no usable `## _LEANS` block (absent or zero lean lines). The board is built ONLY from this block — the prose-scrape fallback was removed 2026-06-30 after it shipped truncated / wrong-ticker rationales ("Long $TLT — Until then the bias is") — so without the block the board is empty. Emit `## _LEANS` with one line per trade, MAIN EVENT first.
- **SOFT `underweighted-all-dropped`**: zero underweighted_candidate themes appear anywhere in the pulse.
- **SOFT `stance-split-no-named-debate`**: theme with ≥2 support AND ≥2 skeptical didn't name Bank-A-vs-Bank-B in its section.

```bash
python3 scripts/pulse_draft_validate.py /tmp/draft.md /tmp/ctx.json /tmp/draft_validation.json
VALIDATOR_EXIT=$?
echo "DRAFT validator exit code: $VALIDATOR_EXIT"
```

Decision logic (read literal exit code; do NOT infer from prose):

- **Exit 0** — clean. Proceed to STEP 5.
- **Exit 3** — HARD violations. Re-dispatch DRAFT with the violations surfaced as fix-this feedback. Read the violation messages from `/tmp/draft_validation.json` and append them to the DRAFT prompt as:

  ```
  STRUCTURAL-VIOLATIONS — your previous DRAFT failed validation. Each violation
  describes what you need to change:

  - [duplicate-sibling-sections] Theme 'X' and its cap-blocked sibling 'Y'
    shipped as separate sections. Fold them into ONE section.
  - [contrarian-buried-in-appendix] Contrarian theme 'consensus-contrarian'
    is not given its own section. Promote it to a dedicated INSIGHTS slot.
    Sample titles: Nobody Wants NVDA / Sell in May / What To Buy If Not AI.

  Re-roll the DRAFT addressing these violations and rerun validation.
  ```

  Maximum 2 re-rolls. If validation still fails after the second re-roll, log a WARNING and proceed to STEP 5 with the residual violations recorded in the QC artifact. Don't loop forever — the pulse must ship even if DRAFT can't get structural compliance perfect.

- **Exit 4** — SOFT violations only. Log them and proceed to STEP 5. Soft violations land in the QC review as advisory items but don't block the publish.

- **Exit 1 or 2** — validator error (missing file, bad ctx.json). Log and proceed to STEP 5 with no violations recorded.

```bash
python3 /tmp/progress.py "STEP_4_5_VALIDATE_DONE"
```

## STEP 5 — Stitch + Edit (Stage 2, two sub-passes)

The post-DRAFT pass is split into a deterministic STITCH (Python, no LLM) followed by a judgment-based EDIT dispatched as a separate sub-agent for fresh-eyes review. Splitting these forces each pass to do one job well: STITCH cannot accidentally drop a theme; EDIT cannot accidentally miss a foreign cashtag.

### Step 5a — STITCH (mechanical fixes, no LLM)

```bash
python3 scripts/pulse_stitch.py /tmp/draft.md /tmp/stitched.md
python3 /tmp/progress.py "STEP_5A_STITCH_DONE"
```

Foreign cashtag scrub (`$TSCO` → "Tesco", `$CNA` → "Centrica", etc.) and ETF normalization (`$SPX` → `$SPY`, `$NDX` → `$QQQ`, `$RUT` → `$IWM`). Single source of truth: `scripts/pulse_stitch.py` constants. The script prints what it changed; review the log briefly to confirm nothing surprising got rewritten.

### Step 5b — EDIT (judgment-based, dispatched as a single sub-agent)

EDIT runs in a fresh `general-purpose` Agent session — NOT in the orchestrator's accumulated context. The sub-agent has no DRAFT history, no adjudication memory, just the stitched pulse + live data. That fresh-eyes property is the entire reason for the dispatch: an in-context AUDIT re-reads its own work; an out-of-context EDIT actually reviews.

Build the sub-agent prompt by combining `AUDIT_SYSTEM` + `AUDIT_USER` (with substitutions) from `ai_analysis/prompts.py`. Substitute `{draft_markdown}` with the contents of `/tmp/stitched.md` (post-STITCH, NOT the raw `/tmp/draft.md`). All other substitutions (`{today}`, `{now}`, `{session_status}`, `{market_snapshot}`, `{news_snapshot}`, `{earnings_calendar}`, `{economic_calendar}`) come from `/tmp/ctx.json`.

Also substitute `{adjudicated_themes_list}` with a human-readable list of every validated theme from `/tmp/adjudication.json`. Format: one bullet per theme, including the canonical name and bank count. This is what powers the AUDIT prompt's DROPPED-THEME AUDIT (anti-DRAFT-compression) section — without this substitution, EDIT can't audit dropped themes.

```python
# Build the adjudicated_themes_list substitution from /tmp/adjudication.json.
# Format: one bullet per validated theme with name + bank count, so EDIT can
# walk the list and check each present in the draft.
adj_themes_list_text = ""
try:
    adj_file = json.load(open('/tmp/adjudication.json'))
    validated = adj_file.get('themes', []) or []
    if validated:
        lines = []
        for t in validated:
            theme_name = t.get('theme', '(unnamed)')
            sc = t.get('stance_counts', {}) or {}
            total_banks = (sc.get('supportive', 0) + sc.get('skeptical', 0) + sc.get('neutral', 0))
            lines.append(f"- **{theme_name}** ({total_banks} banks: {sc.get('supportive',0)} supportive, {sc.get('skeptical',0)} skeptical, {sc.get('neutral',0)} neutral)")
        adj_themes_list_text = "\n".join(lines)
    else:
        adj_themes_list_text = "(none — adjudication produced no validated themes this run)"
except (FileNotFoundError, json.JSONDecodeError):
    adj_themes_list_text = "(adjudication file unavailable — DROPPED-THEME AUDIT can't run this fire)"
```

Save the assembled prompt (SYSTEM + USER concatenated) to `/tmp/agent_io/edit-prompt.txt` BEFORE dispatching, so the QC review can verify substitutions resolved correctly:

```bash
mkdir -p /tmp/agent_io
# (write the assembled prompt to /tmp/agent_io/edit-prompt.txt via Python)
```

Dispatch ONE Agent call with the assembled prompt. The sub-agent applies the full AUDIT pipeline (RECAP rebuild, Pass A cull, Pass A.5 density, Pass B close, voice scrub) and returns the revised markdown. Save the response to `/tmp/final.md`.

Do not pass any tools to the sub-agent — it doesn't need file access; the prompt is fully self-contained.

```bash
python3 /tmp/progress.py "STEP_5B_EDIT_DONE"
```

## STEP 5.5 — Lint final markdown (deterministic regex scan)

Mechanical check before commit. Single source of truth: `ai_analysis/voice_rules.py` defines the banned-phrase / banned-punctuation / source-prefix lists; both the AUDIT prompt and this linter import from there. Updating a banned pattern in voice_rules.py propagates to both — no drift.

The repo is already cloned in the routine sandbox via `session_context.sources`, so `scripts/pulse_lint.py` runs directly with `python3` and resolves its `from ai_analysis.voice_rules import ...` against the cloned tree.

```bash
python3 scripts/pulse_lint.py /tmp/final.md /tmp/lint_report.json /tmp/ctx.json
python3 /tmp/progress.py "STEP_5_5_LINT_DONE"
```

The script prints a human-readable summary inline (issue count, breakdown by kind, first 20 examples with line + snippet). Full structured issues are written to `/tmp/lint_report.json` for STEP 6 to commit alongside the pulse.

## STEP 5.7 — Voice scrub (sub-agent dispatch, lint-driven)

If STEP 5.5's lint report has any HARD issues (any `kind` other than `top-3-theme-missing`), dispatch a SCRUB sub-agent whose ONLY job is to rewrite the flagged sentences. SCRUB does not add or remove themes, change facts, or restructure paragraphs — it walks the lint report and rewrites the specific flagged sentences in place.

This is the layer that closes the voice-enforcement gap: the EDIT sub-agent handles editorial judgment but doesn't reliably iterate over every sentence to enforce voice rules; SCRUB has no other concerns competing for attention and is driven by structured lint output rather than self-supervision.

### Step 5.7.1 — Decide whether to dispatch (BINDING GATE)

Read `/tmp/lint_report.json`. Count the issues whose `kind` is NOT `top-3-theme-missing`, and emit an explicit `SCRUB_DECISION` token that the next sub-step keys off:

```bash
python3 << 'PYEOF'
import json, sys
issues = json.load(open('/tmp/lint_report.json'))
hard = [i for i in issues if i.get('kind') != 'top-3-theme-missing']
print(f'hard_lint_issues: {len(hard)}')
print(f'soft_lint_issues: {len(issues) - len(hard)}')
# Single source of truth for the gate. Write to /tmp so subsequent
# steps can read instead of re-deriving.
decision = 'dispatch' if hard else 'skip'
with open('/tmp/scrub_decision.txt', 'w') as f:
    f.write(decision)
print(f'SCRUB_DECISION: {decision}')
PYEOF
```

**HARD GATE — read the output literally:**

- If the output ends with `SCRUB_DECISION: skip`, you MUST skip STEP 5.7 entirely. Do NOT dispatch the SCRUB sub-agent. Do NOT run STEP 5.7.2 or 5.7.3. Proceed directly to the end-of-STEP-5.7 progress event and then STEP 6. Dispatching SCRUB when there are zero hard lint issues wastes a sub-agent call and produces net-zero quality delta — the 2026-05-14T20-01-08Z test fire did exactly this (SCRUB ran on 0 issues, returned ~70 chars of cosmetic edits). Don't repeat.

- If the output ends with `SCRUB_DECISION: dispatch`, continue to 5.7.2.

This gate is intentionally redundant with the bash output AND a `/tmp/scrub_decision.txt` file because earlier runs interpreted the prose "If hard issues == 0, SKIP" as advisory rather than mandatory. Read the literal token; do not infer.

### Step 5.7.2 — Dispatch SCRUB sub-agent

**BEFORE invoking the SCRUB sub-agent**, save the current `/tmp/final.md` (which is the EDIT output, the artifact SCRUB is about to receive) to `/tmp/pre_scrub_final.md`. This preserves the pre-SCRUB state so the QC review at STEP 7 can diff "what SCRUB received" vs "what SCRUB returned" — without that copy, the EDIT output is lost the moment SCRUB overwrites `/tmp/final.md`.

```bash
cp /tmp/final.md /tmp/pre_scrub_final.md
```

Also save the SCRUB sub-agent's full prompt to `/tmp/agent_io/scrub-prompt.txt` (mkdir -p the dir first) so the QC review can verify the prompt was constructed correctly. Pre-SCRUB final.md and the SCRUB prompt together give QC complete handoff visibility.

```bash
mkdir -p /tmp/agent_io
```

Build the SCRUB prompt by combining `SCRUB_SYSTEM` + `SCRUB_USER` (with substitutions) from `ai_analysis/prompts.py`. Substitutions into `SCRUB_USER`:

- `{issue_count}` ← number of hard issues (computed in 5.7.1)
- `{lint_report_json}` ← contents of `/tmp/lint_report.json` (the full report — SCRUB filters to hard issues itself)
- `{pulse_markdown}` ← contents of `/tmp/final.md`

Save the assembled prompt (SYSTEM + USER concatenated) to `/tmp/agent_io/scrub-prompt.txt` BEFORE dispatching.

Dispatch ONE `general-purpose` Agent with the assembled prompt. The sub-agent runs in fresh context (no DRAFT/EDIT history), sees only the pulse markdown + the structured lint report, and returns the rewritten markdown.

Save the sub-agent's response to `/tmp/scrubbed.md` first (so we keep a copy of the SCRUB output for forensics), then overwrite `/tmp/final.md` with the same content (so STEP 6's commit picks up the scrubbed version).

### Step 5.7.3 — Post-hoc skip enforcement + re-lint

**FIRST: deterministic post-hoc skip enforcement.** The SCRUB_DECISION gate in 5.7.1 has been ignored on at least three runs (2026-06-02 / 2026-06-03 / 2026-06-04 QC reviews all flagged a SCRUB run on 0-lint input producing cosmetic delta). Belt-and-suspenders: if the decision was `skip` but `/tmp/final.md` no longer matches the EDIT output (i.e., SCRUB was dispatched against the gate), revert. This guarantees the skip is enforced regardless of whether the routine-runner honored the instruction.

```bash
if [[ "$(cat /tmp/scrub_decision.txt 2>/dev/null)" == "skip" ]] && [[ -f /tmp/pre_scrub_final.md ]]; then
    if ! cmp -s /tmp/final.md /tmp/pre_scrub_final.md; then
        echo "STEP 5.7.3: SCRUB was dispatched against SCRUB_DECISION=skip; reverting /tmp/final.md to pre-SCRUB state"
        cp /tmp/pre_scrub_final.md /tmp/final.md
    fi
fi
```

This is intentionally redundant with the 5.7.1 gate AND the 5.7.2 conditional dispatch — three layers of enforcement because each previous layer has been bypassed at least once.

Re-run the lint scan against the (possibly reverted) markdown:

```bash
python3 scripts/pulse_lint.py /tmp/final.md /tmp/lint_report.json /tmp/ctx.json
```

Check the new hard-issue count:

- **0 hard issues** → great, proceed to STEP 6.
- **>0 hard issues, but fewer than before** → SCRUB made progress. Dispatch ONE more SCRUB pass (same prompt, fresh UUID, with the new lint report). Re-lint. Accept whatever lint reports after this second pass — proceed to STEP 6 even if residuals exist. The residual lint report ships with the pulse for inspection.
- **>0 hard issues, no progress** → log `WARNING: SCRUB did not reduce lint issues` and proceed to STEP 6 anyway. Don't loop forever — the pulse must ship.

After SCRUB completes (or if SCRUB was skipped), commit a progress event:

```bash
python3 /tmp/progress.py "STEP_5_7_SCRUB_DONE"
```

**Maximum 2 SCRUB iterations.** If lint still has hard issues after the second pass, commit the pulse with the residual lint report; don't block delivery on perfect voice compliance.

If validated SCRUB output is materially different from the EDIT output, that's good — the system is doing its job. If SCRUB returns nearly the same markdown, that's a sign the sub-agent didn't engage with the lint report; flag it in STEP 7's report so we can debug.

If lint reports issues, the SCRUB pass (above) is supposed to handle them automatically — manual rewriting of `/tmp/final.md` is no longer the workflow. The lint report is mechanical and trusted; SCRUB is the agent that acts on it.

## STEP 5.8 — Strip internal-notes sections (mechanical, mandatory)

EDIT and DRAFT prompts emit `## _DRAFT NOTES` and `## _EDIT NOTES` sections containing editorial-decision metadata. These are useful for the QC reviewer in STEP 7 to see editorial intent — but they MUST be removed before the pulse ships. They were leaking through to publish because no routine step actually stripped them.

This step is mechanical: a regex-based scan that removes any H2 header beginning with `## _` and its body up to the next non-internal H2 (or EOF). Idempotent — safe to run multiple times.

```bash
python3 scripts/pulse_strip_internal_notes.py /tmp/final.md 2>&1 | tee -a /tmp/routine.log
```

The script prints either `stripped N chars from /tmp/final.md` or `no internal-notes sections found in /tmp/final.md`. The latter is fine — the EDIT/DRAFT prompts may not have emitted notes on a clean run.

**Do not skip this step.** Even if your eyeball pass of `/tmp/final.md` shows no obvious internal blocks, run it. It's deterministic and cheap, and the consequence of skipping is the same failure mode that happened on 2026-05-15T13-11-32Z (published pulse shipped with a verbatim `## _EDIT NOTES (internal, strip before publish)` block at the bottom).

After the strip, the QC review can still see the editorial intent because EDIT/DRAFT NOTES are emitted to `/tmp/pre_scrub_final.md` and `/tmp/draft.md` respectively, which are committed to `pulse-output/pre-scrub/` and `pulse-output/drafts/` in STEP 6 — only the **archive** copy (the published pulse) gets the strip. QC artifacts retain the editorial trail.

Commit a progress event:

```bash
python3 /tmp/progress.py "STEP_5_8_STRIP_DONE"
```

## STEP 6 — Compose with frontmatter and commit BOTH files (PRODUCTION — ALL CHANNELS)

> **TRANSPORT (see the COMMIT TRANSPORT override near the top):** the `urllib` PUT in the heredoc below WILL 403 in this environment. Use it to BUILD the pulse content and write it to `/tmp/final_with_frontmatter.md`, then commit that file to `pulse-output/pending/<ts>.md` (and each other artifact to its path) via the `mcp__github__*` GitHub tool. The bridge posts only what reaches `pulse-output/pending/`, so this commit is terminal-critical.

```bash
python3 << 'PYEOF' 2>&1 | tee -a /tmp/routine.log
import json, base64, urllib.request, datetime, os, traceback

def _read_token() -> str:
    v = (os.environ.get('GH_TOKEN') or '').strip()
    if v:
        return v
    try:
        return open('/tmp/gh_token.txt').read().strip()
    except FileNotFoundError:
        return ''

GH_TOKEN = _read_token()
if not GH_TOKEN:
    print('FATAL: STEP 6 has no GH_TOKEN in env or /tmp/gh_token.txt — cannot commit')
    raise SystemExit(2)
os.environ['GH_TOKEN'] = GH_TOKEN
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'


def commit_failure(stage: str, reason: str, detail: str = '') -> None:
    """Mirror of the helper from STEP 2 — re-defined here because each
    routine step is a separate heredoc with its own Python namespace.
    Commits a structured failure marker to pulse-output/qc-reviews/ so a
    human reviewer (or the watcher) sees the cause.
    """
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
    body = f"""# QC Review — {ts}

## Status: FAILED at {stage}

- **Time (UTC):** {ts}
- **Stage:** {stage}
- **Reason:** {reason}

## Detail

```
{detail}
```
"""
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{REPO}/contents/pulse-output/qc-reviews/{ts}.md',
            data=json.dumps({
                'message': f'routine: FAILURE at {stage} ({ts})',
                'content': base64.b64encode(body.encode()).decode(),
                'branch': BRANCH,
            }).encode(),
            headers={
                'Authorization': f'token {GH_TOKEN}',
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
            },
            method='PUT',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f'committed failure marker: pulse-output/qc-reviews/{ts}.md')
    except Exception as e:
        print(f'WARNING: could not commit failure marker: {e}')


try:
    ctx = json.load(open('/tmp/ctx.json'))
    final_md = open('/tmp/final.md').read()
except Exception as e:
    commit_failure(
        stage='STEP 6 (read inputs)',
        reason=f'{type(e).__name__}: {e}',
        detail=traceback.format_exc(),
    )
    raise SystemExit(1)

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
# === TEST/PROD via TARGET_CHANNELS ===
# Default (file empty / absent): no target_channels line emitted -> all
# configured channels (production behavior). For test fires, the routine
# body writes the test-channel substring to /tmp/target_channels.txt; the
# bridge worker filters Discord delivery to channels matching that
# substring. Env vars do NOT persist across separate Bash tool calls, so
# we read from the file as the canonical source. Switching test/prod is a
# RemoteTrigger update on the routine body, not an edit to this file --
# no git push, no recomment-before-cron trap.
def _read_target_channels() -> str:
    v = (os.environ.get('TARGET_CHANNELS') or '').strip()
    if v:
        return v
    try:
        return open('/tmp/target_channels.txt').read().strip()
    except FileNotFoundError:
        return ''

target_channels = _read_target_channels()
if target_channels:
    frontmatter_lines.append(f"target_channels: {target_channels}")
frontmatter_lines.append('---')
frontmatter = '\n'.join(frontmatter_lines) + '\n\n'

file_content = frontmatter + final_md
# Reuse the pulse timestamp pinned by STEP 2 — STEP 7 (QC review) needs
# the SAME ts for filename pairing, and STEP 2 wrote it to /tmp/pulse_ts.txt
# at fire start. Fall back to a fresh ts if the file is missing (older
# routine.md without STEP 2's pin).
try:
    ts = open('/tmp/pulse_ts.txt').read().strip()
except FileNotFoundError:
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
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

# Commit /tmp/adjudication_inputs.json — the per-theme evidence bundles
# fed to each adjudication sub-agent. WITHOUT this artifact, a QC review
# (per-run or cross-run) can only see what adjudication EMITTED, never
# what it RECEIVED — which means it blames the stage that emitted the
# bad result and misses the upstream input-contract bug.
#
# Concretely: today's heavyweight-theme-discard pattern (16-bank theme
# getting nuked by Rule 5/6) is invisible from outputs alone; it
# diagnoses correctly only by reading the per-theme inputs and seeing
# that adjudication got 1 evidence entry for a 16-bank theme. This
# commit makes that diagnosis possible retrospectively.
if os.path.exists('/tmp/adjudication_inputs.json'):
    adj_inputs_path = f'pulse-output/qc-inputs/{ts}.adjudication-inputs.json'
    adj_inputs_content = open('/tmp/adjudication_inputs.json').read().encode()
    result = commit(adj_inputs_path, adj_inputs_content, f'routine: adjudication per-theme inputs {ts}')
    print('committed adjudication inputs:', adj_inputs_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])
else:
    print('no /tmp/adjudication_inputs.json — skipping (adjudication step may have been skipped this run)')

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

# Commit /tmp/pre_scrub_final.md if SCRUB ran (preserves the EDIT output
# before SCRUB rewrote sentences — needed for forensic diff EDIT vs SCRUB).
# Absent when SCRUB was skipped (EDIT output equals final committed pulse).
if os.path.exists('/tmp/pre_scrub_final.md'):
    pre_scrub_path = f'pulse-output/pre-scrub/{ts}.md'
    pre_scrub_content = open('/tmp/pre_scrub_final.md').read().encode()
    result = commit(pre_scrub_path, pre_scrub_content, f'routine: pre-scrub edit-output {ts}')
    print('committed pre-scrub:', pre_scrub_path)
    print('commit sha:', (result.get('commit') or {}).get('sha', '')[:12])

# Commit /tmp/agent_io/* sub-agent prompts (EDIT, SCRUB) so the human
# reviewer can inspect what each sub-agent actually received. The QC
# review summarizes sizes/deltas from these; full prompts here let a
# reviewer dig in when QC flags a handoff issue.
import glob
for prompt_path in sorted(glob.glob('/tmp/agent_io/*.txt')):
    name = os.path.basename(prompt_path)  # e.g., 'edit-prompt.txt'
    target = f'pulse-output/agent-io/{ts}/{name}'
    try:
        prompt_content = open(prompt_path, 'rb').read()
        # Cap at 200 KB to avoid pathological prompt sizes blowing up the branch
        if len(prompt_content) > 200_000:
            prompt_content = prompt_content[:200_000] + b'\n\n[... truncated for size ...]'
        result = commit(target, prompt_content, f'routine: agent prompt {name} ({ts})')
        print(f'committed agent-io {name}:', target)
    except Exception as e:
        print(f'WARNING: agent-io commit failed for {name}: {e}')

print(f'pdf_count: {ctx["pdf_count"]}, output_tokens_est: {output_tokens_est}, target: ALL configured channels (production)')
PYEOF
```

```bash
python3 /tmp/progress.py "STEP_6_COMMIT_DONE"
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
# Pre-SCRUB final.md = EDIT output before SCRUB rewrote sentences. Saved
# by STEP 5.7.2 before SCRUB overwrote /tmp/final.md. If SCRUB didn't run
# (zero hard lint issues), this file is absent and the EDIT output equals
# the final committed output — handoffs_summary marks SCRUB as skipped.
pre_scrub_md = _read_or('/tmp/pre_scrub_final.md', '(SCRUB did not run; EDIT output == final)')

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

# Sub-agent handoff summary — what each dispatched sub-agent received and
# returned. QC uses this to assess whether prompts were constructed
# correctly, whether sub-agents engaged with their inputs, and whether
# transformation magnitude (line/char delta) suggests a working pass.
def _sz(s: str) -> str:
    """Format size as '<chars> chars / <lines> lines'."""
    if not isinstance(s, str):
        return '?'
    return f"{len(s)} chars / {s.count(chr(10)) + 1} lines"

def _canon_number(tok: str) -> float | None:
    """Normalize a numeric token to a canonical float magnitude so that
    differently-formatted-but-equal values match: "$222 million" → 222e6,
    "$222M" → 222e6, "$3 billion" → 3e9, "$3B" → 3e9, "$81,086" → 81086,
    "4.4%" → 4.4, "50bps" → 50, "0.3x" → 0.3. Returns None if the token
    doesn't parse (the caller falls back to exact-string matching).
    """
    import re as _re
    t = tok.strip().lower().replace(',', '').replace('$', '').replace('+', '')
    m = _re.match(r'^(-?\d+(?:\.\d+)?)\s*(million|billion|thousand|trillion|m|b|k|t|bps?|x|%)?\b', t)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2) or ''
    mult = {
        'thousand': 1e3, 'k': 1e3,
        'million': 1e6, 'm': 1e6,
        'billion': 1e9, 'b': 1e9,
        'trillion': 1e12, 't': 1e12,
    }.get(unit, 1.0)
    return val * mult

def _extract_numbers(text: str) -> set[str]:
    """Extract numeric tokens worth fact-checking. Filters to financial
    units (dollar amounts incl. spelled-out "million/billion", percentages,
    basis points, year-like 4-digit numbers, multiples) since those are the
    high-leverage hallucination targets. Skips short integers (1, 2, 3) and
    decimal-only floats which are too generic to track.
    """
    import re as _re
    patterns = [
        r'\$\s?[\d,]+\.?\d*\s*(?:million|billion|trillion|thousand)?\b',  # $222 million, $755B-style handled below too
        r'\$[\d,]+\.?\d*[BMKTbmkt]?',        # $755B, $1.8B, $1,234
        r'[+-]?\d+\.?\d*\s*%',                # +5%, -0.3%, 4.4 %
        r'\d+\s*bp[s]?\b',                    # 50bps, 25 bp
        r'\b\d{4,}\b',                        # 4+ digit numbers (years, levels like 5800)
        r'\d+\.\d+x\b',                       # multiples like 0.3x, 5.4x
    ]
    out: set[str] = set()
    for p in patterns:
        out.update(m.strip() for m in _re.findall(p, text))
    return out

def _edit_introduced_numbers() -> tuple[list[str], int]:
    """Return (truly_unverified_numbers, count_explained_by_live_data).

    Compares post-SCRUB final.md against the EDIT input (stitched.md) and
    the live-data context (market/news/calendars/corpus). A number in
    final that isn't in either set is a candidate hallucination — BUT
    before flagging it, we also check whether its CANONICAL MAGNITUDE
    matches any allowed value within a rounding tolerance. This kills the
    false positives the prior version produced: "$222 million" in the
    news_snapshot vs "$222M" in the final, or "$81,086" in the
    market_snapshot vs "$81,100" rounded in the RECAP. Only numbers that
    don't match by EXACT STRING and don't match by CANONICAL MAGNITUDE
    (within ±1%) get flagged.
    """
    stitched = _read_or('/tmp/stitched.md', '')
    final = _read_or('/tmp/final.md', '')
    if not stitched or not final:
        return [], 0
    stitched_nums = _extract_numbers(stitched)
    final_nums = _extract_numbers(final)
    live_blob = '\n'.join([
        ctx.get('market_snapshot') or '',
        ctx.get('news_snapshot') or '',
        ctx.get('economic_calendar') or '',
        ctx.get('earnings_calendar') or '',
        ctx.get('analyses_json') or '',  # corpus contents — analyst-cited numbers
    ])
    live_nums = _extract_numbers(live_blob)
    allowed_strings = stitched_nums | live_nums
    # Canonical magnitudes of every allowed value, for rounding-tolerant match.
    allowed_canon = []
    for s in allowed_strings:
        c = _canon_number(s)
        if c is not None:
            allowed_canon.append(c)

    new_in_final = final_nums - stitched_nums
    explained = 0
    truly_unverified: list[str] = []
    for tok in sorted(new_in_final):
        if tok in live_nums:
            explained += 1
            continue
        c = _canon_number(tok)
        if c is not None and any(
            abs(c - a) <= max(abs(a), abs(c)) * 0.01  # ±1% tolerance
            for a in allowed_canon
        ):
            explained += 1
            continue
        truly_unverified.append(tok)
    return truly_unverified, explained

# Adjudication: count dispatched and outcome
n_dispatched = len(adj_file.get('themes', []) or []) + len(discarded)
adj_themes_list = ', '.join(
    (t.get('theme') or '?') for t in (adj_file.get('themes', []) or [])
) or '(none validated)'

# Pre-SCRUB exists iff SCRUB ran (STEP 5.7.2 made the copy before
# overwriting /tmp/final.md).
scrub_ran = os.path.exists('/tmp/pre_scrub_final.md')

# Pre-EDIT artifact (what EDIT received) is the post-STITCH stitched.md.
# Post-EDIT artifact is /tmp/pre_scrub_final.md (if SCRUB ran) OR
# /tmp/final.md (if SCRUB skipped).
post_edit_md = pre_scrub_md if scrub_ran else final_md

# Sub-agent prompt files (saved by their respective steps before dispatch)
edit_prompt = _read_or('/tmp/agent_io/edit-prompt.txt', '(prompt not saved)')
scrub_prompt = _read_or('/tmp/agent_io/scrub-prompt.txt', '(prompt not saved)')

handoffs_summary = f"""ADJUDICATION sub-agents:
  - Dispatched: {n_dispatched} (one per selected theme)
  - Validated: {len(adj_file.get('themes', []) or [])}
  - Discarded: {len(discarded)}
  - Validated themes: {adj_themes_list}
  - Discard reasons: {discard_reasons}

EDIT sub-agent (single dispatch):
  - Prompt size: {len(edit_prompt)} chars (saved to /tmp/agent_io/edit-prompt.txt)
  - Input  (stitched.md):     {_sz(stitched_md)}
  - Output (post-EDIT md):    {_sz(post_edit_md)}

SCRUB sub-agent:
  - Status: {'RAN' if scrub_ran else 'SKIPPED (zero hard lint issues from EDIT output)'}"""

if scrub_ran:
    handoffs_summary += f"""
  - Prompt size: {len(scrub_prompt)} chars (saved to /tmp/agent_io/scrub-prompt.txt)
  - Input  (pre-SCRUB / EDIT output): {_sz(post_edit_md)}
  - Output (post-SCRUB / final):      {_sz(final_md)}
  - Lint issues input:  {len(lint_report)} total (capped at {len(lint_summary)} in this view)"""

# EDIT-introduced numbers (fact-provenance check). Numbers in final.md that
# weren't in stitched.md or in the live-data context. Each entry is a
# candidate hallucination worth verifying.
unverified_numbers, explained_count = _edit_introduced_numbers()
handoffs_summary += f"""

EDIT FACT-PROVENANCE CHECK:
  - Numbers introduced by EDIT/SCRUB after stitched.md: {len(unverified_numbers) + explained_count} total
  - Explained by live-data context (market/news/calendar/corpus): {explained_count}
  - UNVERIFIED (not in stitched.md, not in live data — candidate hallucinations): {len(unverified_numbers)}"""
if unverified_numbers:
    handoffs_summary += "\n  - Unverified tokens: " + ", ".join(unverified_numbers[:20])
    if len(unverified_numbers) > 20:
        handoffs_summary += f", ... +{len(unverified_numbers) - 20} more"

# Use the SAME timestamp the pulse was committed under so the QC review
# filename pairs cleanly with the pulse markdown filename.
ts = open('/tmp/pulse_ts.txt').read().strip() if os.path.exists('/tmp/pulse_ts.txt') \
     else datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')

# --- Day-over-day comparison inputs ---------------------------------------
# Fetch the PREVIOUS scheduled pulse's final markdown + its QC review so the
# QC sub-agent can assess whether the recurring issues got fixed and whether
# the writing is trending better. "Previous scheduled pulse" = the most
# recent pulse-output/archive/<ts>.md that (a) isn't today's pulse, (b) isn't
# a test fire (no `target_channels:` in its frontmatter). Best-effort: if
# anything fails, prev_pulse_md / prev_qc_review default to a "(none)"
# placeholder and the QC review just skips the comparison section.
GH_TOKEN = _read_token()
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'

def _gh_get_json(path: str):
    url = f'https://api.github.com/repos/{REPO}/contents/{path}?ref={BRANCH}'
    req = urllib.request.Request(url, headers={'Authorization': f'token {GH_TOKEN}', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.load(resp)

def _gh_get_text(path: str) -> str:
    import base64 as _b64
    d = _gh_get_json(path)
    return _b64.b64decode(d.get('content', '')).decode('utf-8', errors='replace')

prev_pulse_md = '(none — no prior scheduled pulse found)'
prev_qc_review = '(none — no prior QC review found)'
prev_ts = None
try:
    listing = _gh_get_json('pulse-output/archive')
    archive_files = sorted(
        (f['name'] for f in listing if isinstance(f, dict) and f.get('name', '').endswith('.md')),
        reverse=True,
    )
    for fname in archive_files:
        base = fname[:-3]
        if base == ts:
            continue  # that's today's pulse (if the bridge already archived it)
        # Fetch + check frontmatter for target_channels (test-fire marker).
        try:
            content = _gh_get_text(f'pulse-output/archive/{fname}')
        except Exception:
            continue
        # crude frontmatter scan: target_channels line means it's a test fire
        head = content[:600]
        if 'target_channels:' in head:
            continue  # skip test fires
        prev_pulse_md = content
        prev_ts = base
        break
    if prev_ts:
        try:
            prev_qc_review = _gh_get_text(f'pulse-output/qc-reviews/{prev_ts}.md')
        except Exception:
            prev_qc_review = '(prior pulse found but its QC review is missing)'
except Exception as e:
    print(f'WARNING: day-over-day fetch failed: {e}')

# Cap the prior-pulse inputs so the QC prompt stays manageable.
if len(prev_pulse_md) > 12000:
    prev_pulse_md = prev_pulse_md[:12000] + '\n\n[... truncated for prompt size ...]'
if len(prev_qc_review) > 9000:
    prev_qc_review = prev_qc_review[:9000] + '\n\n[... truncated for prompt size ...]'
# -------------------------------------------------------------------------

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
    'pre_scrub_md': pre_scrub_md,
    'handoffs_summary': handoffs_summary,
    'prev_pulse_ts': prev_ts or '(none)',
    'prev_pulse_md': prev_pulse_md,
    'prev_qc_review': prev_qc_review,
}
with open('/tmp/qc_inputs.json', 'w') as f:
    json.dump(qc_inputs, f, indent=1)

print(f"QC inputs prepared:")
print(f"  promoted discoveries: {len(discovery_audit.get('promoted', []) or [])}")
print(f"  near-miss clusters:   {len(discovery_audit.get('near_miss', []) or [])}")
print(f"  lint issues in scope: {len(lint_summary)} (of {len(lint_report)} total)")
print(f"  adjudication: {qc_inputs['n_validated']} validated, {qc_inputs['n_discarded']} discarded")
print(f"  SCRUB ran: {scrub_ran}")
print(f"  EDIT prompt saved: {os.path.exists('/tmp/agent_io/edit-prompt.txt')}")
print(f"  SCRUB prompt saved: {os.path.exists('/tmp/agent_io/scrub-prompt.txt')}")
print(f"  prev scheduled pulse for comparison: {prev_ts or '(none found)'}")
PYEOF
```

### Step 7.2 — Dispatch QC sub-agent

Build the QC prompt by combining `QC_SYSTEM` + `QC_USER` (with substitutions from `/tmp/qc_inputs.json`) from `ai_analysis/prompts.py`. Substitute every placeholder in `QC_USER`:

- `{timestamp}`
- `{theme_coverage}`
- `{discovery_audit_json}`
- `{n_validated}`, `{n_discarded}`, `{discard_reasons}`
- `{lint_summary_json}`
- `{handoffs_summary}` — sub-agent dispatch + I/O sizes
- `{draft_md}`, `{stitched_md}`, `{pre_scrub_md}`, `{final_md}` — pre_scrub_md equals final if SCRUB skipped
- `{prev_pulse_ts}`, `{prev_pulse_md}`, `{prev_qc_review}` — the previous scheduled pulse's final markdown + its QC review, for the day-over-day comparison section. Both default to a "(none)" placeholder string when there's no prior scheduled pulse (first run, or only test fires preceding) — the QC review then just notes the comparison is N/A.

Dispatch ONE `general-purpose` Agent with the assembled prompt. The sub-agent runs in fresh context — no DRAFT/EDIT/SCRUB history, just the artifacts the prompt provides. It returns the QC review markdown (no preamble, no JSON wrapper).

Save the sub-agent's response to `/tmp/qc_review.md`.

If the sub-agent errors out OR returns empty content, log a warning and proceed to STEP 7.3 (which will skip the commit cleanly). Do NOT retry — the pulse is already posted and a missing QC review is recoverable; a stuck QC retry blocking STEP 8 confirmation is not.

### Step 7.3 — Commit QC review

```bash
python3 << 'PYEOF' 2>&1 | tee -a /tmp/routine.log
import os, base64, urllib.request, json, datetime, traceback

def _read_token() -> str:
    v = (os.environ.get('GH_TOKEN') or '').strip()
    if v:
        return v
    try:
        return open('/tmp/gh_token.txt').read().strip()
    except FileNotFoundError:
        return ''

GH_TOKEN = _read_token()
if not GH_TOKEN:
    print('FATAL: STEP 7.3 has no GH_TOKEN — cannot commit QC review')
    raise SystemExit(2)
os.environ['GH_TOKEN'] = GH_TOKEN
REPO = 'gabjew90/Institutional-report-bot'
BRANCH = 'pulse-data'


def _commit_failure(stage: str, reason: str, detail: str = '') -> None:
    """Lightweight failure marker — same shape as STEP 2's, redefined here
    so STEP 7 doesn't depend on prior heredoc namespaces. Captures
    the absence-of-output case (QC sub-agent produced nothing) AND any
    exception thrown during commit. Without this, a failed STEP 7 was
    invisible: no QC review file, no failure marker, just absence.
    """
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%dT%H-%M-%SZ')
    body = f"""# QC Review — {ts}

## Status: FAILED at {stage}

- **Time (UTC):** {ts}
- **Stage:** {stage}
- **Reason:** {reason}

## Detail

```
{detail}
```

## Routine log tail (last 80 lines)

```
"""
    try:
        with open('/tmp/routine.log', 'r', errors='replace') as f:
            tail = ''.join(f.readlines()[-80:])
    except Exception:
        tail = '(routine.log missing)'
    body += tail + "\n```\n"
    try:
        req = urllib.request.Request(
            f'https://api.github.com/repos/{REPO}/contents/pulse-output/qc-reviews/{ts}.md',
            data=json.dumps({
                'message': f'routine: FAILURE at {stage} ({ts})',
                'content': base64.b64encode(body.encode()).decode(),
                'branch': BRANCH,
            }).encode(),
            headers={
                'Authorization': f'token {GH_TOKEN}',
                'Accept': 'application/vnd.github+json',
                'Content-Type': 'application/json',
            },
            method='PUT',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f'committed failure marker: pulse-output/qc-reviews/{ts}.md')
    except Exception as e:
        print(f'WARNING: could not commit failure marker: {e}')


if not os.path.exists('/tmp/qc_review.md'):
    print('no /tmp/qc_review.md — QC sub-agent produced no output')
    _commit_failure(
        stage='STEP 7 (QC review)',
        reason='QC sub-agent produced no output (/tmp/qc_review.md missing)',
        detail='STEP 7.2 dispatched the QC sub-agent but no review markdown landed at /tmp/qc_review.md. Either the sub-agent errored, returned empty content, or was never dispatched. The pulse markdown is already committed and the bridge will deliver it; the QC review is missing for this run.',
    )
    raise SystemExit(0)

content_str = open('/tmp/qc_review.md').read().strip()
if not content_str:
    print('QC review file is empty')
    _commit_failure(
        stage='STEP 7 (QC review)',
        reason='QC review markdown is empty (sub-agent returned blank content)',
        detail='/tmp/qc_review.md exists but contains no content after strip(). Sub-agent likely produced empty output or whitespace-only response.',
    )
    raise SystemExit(0)

ts = open('/tmp/pulse_ts.txt').read().strip() if os.path.exists('/tmp/pulse_ts.txt') \
     else json.load(open('/tmp/qc_inputs.json'))['timestamp']
qc_path = f'pulse-output/qc-reviews/{ts}.md'

try:
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
except Exception as e:
    print(f'ERROR committing QC review: {e}')
    _commit_failure(
        stage='STEP 7 (QC review commit)',
        reason=f'{type(e).__name__}: {e}',
        detail=f'Got QC review markdown from sub-agent ({len(content_str)} chars) but the GitHub PUT to {qc_path} failed.\n\n{traceback.format_exc()}',
    )
    raise SystemExit(1)
PYEOF
```

```bash
python3 /tmp/progress.py "STEP_7_QC_DONE"
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
