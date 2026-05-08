# Routine Operations

How to fire test pulses, update synthesis logic, change routine config, and read the artifacts each fire produces. This is the operations-side reference; for the actual synthesis instructions see [`synthesis-routine.md`](synthesis-routine.md).

---

## Architecture in 30 seconds

The routine config on Claude.ai is a **10-line bootstrap**: it curls [`synthesis-routine.md`](synthesis-routine.md) from this branch and follows it verbatim. Everything substantive — the synthesis pipeline, voice rules, lint, prompts — lives in this repo. Updating logic = `git push`; the next routine fire fetches the fresh markdown automatically. The Claude.ai routine UI itself is set and almost never needs to be touched again.

**Three control planes:**
1. **Repo** (`git push`): synthesis-routine.md, voice_rules.py, prompts.py, scripts/pulse_*.py — for changing what the routine *does*.
2. **`RemoteTrigger` API**: for *firing* the routine on demand, or changing routine *config* fields (model, cron, allowed_tools, env vars).
3. **Claude.ai routine UI**: only for OAuth token rotation or adding new routines from scratch.

---

## Active triggers

| Trigger ID | Name | State | Purpose |
|---|---|---|---|
| `trig_01PyVG6upw8ddZoSZrQaKfP6` | Daily Market Pulse (Opus, GitHub bridge) | Enabled, cron `0 13 * * 1-5` (9 AM ET weekdays) | The production routine. Also used for one-off test fires via the 3-API-call dance below. |
| `trig_0143UjLciqrMGNEvJCFdSYm5` | (HTTP-bridge corpse) | Disabled | Old HTTP-bridge attempt, kept for historical reference. The Anthropic egress proxy blocks `*.up.railway.app`, which is why we shifted to the GitHub-as-bridge pattern. |
| `trig_01BgFLZwCarXGeKFkcLgLoPf` | (Egress probe) | Disabled, `run_once_at` already fired | One-shot probe of which domains the routine sandbox can reach. Kept as a record of the egress allowlist. |

The API doesn't expose a delete endpoint. To clean dead triggers, use the web UI at https://claude.ai/code/routines.

---

## The 3-API-call test fire (most common operation)

To fire a one-off pulse to `#test-channel` only without disturbing tomorrow's prod cron:

### Step 1 — Update routine body to set `TARGET_CHANNELS='test-channel'`

```python
from RemoteTrigger import update  # or whatever wrapper you have
import uuid

update(
    trigger_id="trig_01PyVG6upw8ddZoSZrQaKfP6",
    body={
        "job_config": {
            "ccr": {
                "environment_id": "env_01Rx3c1fmFswESDae915SEqh",
                "session_context": {
                    "model": "claude-opus-4-7",
                    "allowed_tools": ["Bash", "Read", "Glob", "Grep", "Agent"],
                    "sources": [{"git_repository": {"url": "https://github.com/gabjew90/Institutional-report-bot"}}],
                    "outcomes": [{"git_repository": {"git_info": {"branches": ["claude/amazing-ride"], "repo": "gabjew90/Institutional-report-bot"}}}],
                    "autofix_on_pr_create": False,
                },
                "events": [{"data": {
                    "uuid": str(uuid.uuid4()),
                    "session_id": "",
                    "type": "user",
                    "parent_tool_use_id": None,
                    "message": {"role": "user", "content": ROUTINE_BODY_WITH_TEST_CHANNEL},
                }}],
            },
        }
    },
)
```

`ROUTINE_BODY_WITH_TEST_CHANNEL` is the bootstrap with `export TARGET_CHANNELS='test-channel'` added between `export GH_TOKEN=...` and the curl. The routine markdown's STEP 6 reads `os.environ.get('TARGET_CHANNELS')` and only injects the `target_channels` frontmatter line when set; unset means "all configured channels."

### Step 2 — Fire it

```python
run(trigger_id="trig_01PyVG6upw8ddZoSZrQaKfP6")
```

### Step 3 — IMMEDIATELY revert the body to prod-safe

The routine session captured the env var at fire time, so reverting now doesn't disrupt the in-flight test. But it must happen before the next cron at 13:08 UTC, otherwise that prod fire would also go to test-channel only.

```python
update(
    trigger_id="trig_01PyVG6upw8ddZoSZrQaKfP6",
    body={
        # Same shape as Step 1, but events[].data.message.content is the
        # bootstrap WITHOUT export TARGET_CHANNELS=...
        ...
    },
)
```

**Why this works without touching anything else:** the toggle lives entirely in the routine sandbox's environment. Every other piece (synthesis-routine.md, voice_rules.py, the bridge worker) has zero awareness of test vs prod. The routine emits a `target_channels: test-channel` frontmatter line; the bridge worker on Railway reads that line and filters Discord delivery accordingly. No git push, no MD edit, no risk of the toggle being left ON across day boundaries because the body is reverted in the same minute the run was issued.

---

## Routine session lifecycle: what happens when you fire it

```
RemoteTrigger run
  ↓ Anthropic spins up a sandbox session, clones the repo
  ↓ session executes the bootstrap (curl synthesis-routine.md)
  ↓ session reads /tmp/routine.md and walks each STEP

  STEP 1   — read prompts.py for ADJUDICATION/DRAFT/AUDIT system + user
  STEP 2   — curl pulse-context/latest.json from this branch
  STEP 3   — inspect theme_coverage block
  STEP 3.5 — adjudicate (parallel sub-agents per theme, with lint)
  STEP 4   — DRAFT (orchestrator's own reasoning, with adjudication block injected)
  STEP 5a  — STITCH (scripts/pulse_stitch.py — mechanical fixes)
  STEP 5b  — EDIT (sub-agent dispatch, fresh context)
  STEP 5.5 — LINT (scripts/pulse_lint.py against final.md)
  STEP 6   — commit pulse + adjudication + draft + stitched + lint to bridge
  STEP 7   — report

  ↓ bridge worker on Railway polls every 60s
  ↓ matches pulse markdown to its sibling adjudication
  ↓ filters channels via target_channels frontmatter (if set)
  ↓ posts to Discord, archives all five files
```

Total wall time: 5-12 min depending on Opus latency and theme count.

---

## Common operations

### "I want to change a synthesis rule"

Edit `synthesis-routine.md` (the STEP-level instructions) or `ai_analysis/prompts.py` (the DRAFT/AUDIT/ADJUDICATION prompt strings) or `ai_analysis/voice_rules.py` (banned phrases, jargon, source-prefix patterns). `git push`. Next routine fire (manual or cron) picks it up via the bootstrap fetch. **No RemoteTrigger update needed.**

### "I want to add a banned phrase to the linter"

Edit `ai_analysis/voice_rules.py` — append to `BANNED_FILLER_PHRASES`, `BANNED_AI_TELLS`, `JARGON_WITH_TRANSLATIONS`, etc. The constant is the single source of truth: both the AUDIT prompt (via `compose_audit_voice_block()` interpolation in `prompts.py`) and the linter (`scripts/pulse_lint.py` via `compose_lint_patterns()`) read from it. No drift possible. `git push`.

### "I want to change the routine config (model, cron, allowed_tools, mcp connectors)"

Use `RemoteTrigger update` with a body containing only `job_config.ccr.session_context.<field>` overrides plus the existing events (with a fresh UUID). The routine config — distinct from the routine *body* — only changes when you actually want different infrastructure (e.g., bumping to a new model). Most operations don't need this.

### "I want to add a new routine"

Use the [`schedule` skill](../../../.claude/...) (or call `RemoteTrigger create` directly). The routine body should be the bootstrap pattern from `routine-bootstrap.md` — paste once, then iterate via repo edits. Don't paste a full inline synthesis prompt; you'll regret the drift.

### "I want to fire prod manually outside the cron schedule"

`RemoteTrigger run` against `trig_01PyVG6upw8ddZoSZrQaKfP6`. The current body (whatever it last got `update`d to) determines behavior. By default that's prod-safe (no `TARGET_CHANNELS`), so a bare `run` posts to all configured channels.

### "I want to verify what state the routine body is currently in"

`RemoteTrigger get` against the trigger ID. The response contains `job_config.ccr.events[0].data.message.content` — that's the actual prompt body the routine will execute on its next fire. Useful sanity check after a test-fire dance to confirm the revert step landed.

---

## Artifacts produced per fire

The routine commits 5 files per fire to the `pulse-data` branch. The bridge worker on Railway archives them after Discord delivery.

| Path | Contents |
|---|---|
| `pulse-output/pending/<ts>.md` | Final pulse markdown (post-EDIT, post-LINT). Bridge worker posts this to Discord then archives to `pulse-output/archive/<ts>.md`. |
| `pulse-output/pending-adjudications/<ts>.json` | Per-theme adjudication: stance counts, consensus_view, facts_agreed, facts_contested, falsifiable_predictions. Archived to `pulse-output/archive-adjudications/<ts>.json`. |
| `pulse-output/drafts/<ts>.md` | Pre-STITCH DRAFT (post-DRAFT, pre-mechanical-fixes). Forensics — diff vs `stitched/<ts>.md` to see what STITCH did. |
| `pulse-output/stitched/<ts>.md` | Post-STITCH, pre-EDIT (mechanical fixes applied: foreign cashtag scrub, ETF normalization). Diff vs `archive/<ts>.md` to isolate EDIT's editorial changes from STITCH's mechanical ones. |
| `pulse-output/lint/<ts>.json` | Lint report: list of `{line, kind, snippet}` issues found in the final markdown. Hard issues (em-dash, banned vocab, source-prefix) should be 0; soft issues (jargon-bare, top-3-theme-missing) are advisory. |

The pulse markdown (and its `daily_reports.report_json` row in the production DB) also embeds the full adjudication via `raw_json["adjudication"]`. So a single SQL query can pull pulse + adjudication together for any past fire.

---

## What you basically never touch

- **Claude.ai routine UI** — the bootstrap is set; future iteration is git + API.
- **GH_TOKEN** — embedded in the routine body. Rotate when convenient (whoever has had access to this conversation has seen the token).
- **`api/server.py`** HTTP endpoints — alternative to the GitHub bridge, currently inactive. The bridge pattern won.
- **`synthesize_daily_pulse`** in `report/synthesizer.py` — the legacy in-process Gemini synthesis path used only by manual `/pulse` Discord command. Not used by the scheduled routine.

---

## Egress allowlist (routine sandbox)

The routine session can reach:
- `github.com` and `raw.githubusercontent.com` (for fetching the routine markdown, the prompts, and the context JSON; for committing pulse/adjudication/draft/stitched/lint files)
- `api.github.com` (for the contents API used by `urllib.request` PUT)

It cannot reach:
- `*.up.railway.app` (no direct route to your Railway worker)
- Discord or any other end-user surface
- Any internal Anthropic services beyond the routine harness

This is why the GitHub-as-bridge pattern is the only viable channel: GitHub is the message bus, Railway pulls from GitHub, Railway pushes to Discord. The routine never talks to Railway or Discord directly.

---

## Quick troubleshooting

**Routine fired but no pulse in Discord:** Check Railway logs (`railway logs --deployment | grep Bridge:`) for the bridge worker's poll cycle. The pulse markdown might be in `pulse-output/pending/` waiting for the next 60s tick, or might have been filtered to zero channels (e.g., `TARGET_CHANNELS` set to a substring that doesn't match any configured channel name).

**Adjudication produced but empty:** Check the routine session output for `WARNING: theme_map missing` or `Dropping N theme(s) before dispatch — no usable inputs` messages. Most often a theme normalization mismatch (synthesizer.py vs routine norm()) or the corpus genuinely had no cross-bank themes that day.

**Lint reports many issues:** Look at `pulse-output/lint/<ts>.json`. Hard issue kinds (em-dash, semicolon, source-prefix, AI-cliche-verb, etc.) mean the EDIT sub-agent's voice scrub didn't fully apply. Soft issue kinds (jargon-bare, top-3-theme-missing) are advisory — verify the surrounding paragraph has a translation or reasoning, but they don't block commits.

**Lint reports zero issues but the pulse still reads wrong:** the patterns in `voice_rules.py` are not exhaustive. If you spot a new pattern that should have been caught, add it as a constant entry — both the AUDIT prompt and the linter pick it up automatically on next module load.

**Routine running on stale code:** `git push` happens but the routine fetches old files. Two possible causes: (a) GitHub raw.github.com CDN cache (~5 min TTL — wait or bust), (b) Railway hasn't redeployed yet (railway redeploys on push but takes ~2-3 min). Verify with `curl https://raw.githubusercontent.com/.../synthesis-routine.md | head -10` to confirm the file you expect is actually being served.
