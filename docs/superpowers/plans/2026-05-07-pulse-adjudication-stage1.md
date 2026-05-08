# Pulse Adjudication Stage 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the bridge worker to receive, persist, and archive a per-pulse adjudication JSON file produced by the synthesis routine — so the audit/diff-framing artifact described in `docs/superpowers/specs/2026-05-07-pulse-adjudication-design.md` lands in `daily_reports.report_json` and on the bridge branch alongside each posted pulse.

**Architecture:** Single file change to `github_bridge/jobs.py`. The synthesis routine writes `pulse-output/pending-adjudications/<timestamp>.json` to the bridge branch alongside the existing `pulse-output/pending/<timestamp>.md`. When the worker processes a pending pulse, it now also looks up the matching adjudication file by base name (`<timestamp>.md` ↔ `<timestamp>.json`), embeds the parsed JSON into `DailyReport.raw_json` under key `"adjudication"`, and archives the adjudication file alongside the pulse markdown after successful posting. Failures to fetch / parse / archive the adjudication never block pulse delivery.

**Tech Stack:** Python 3.11, existing `github_bridge.client` HTTP wrappers (`get_file_text`, `put_file`, `delete_file`), existing `db.insert_daily_report` / `report.models.DailyReport`. No new dependencies. No DB schema change. No tests are pytest tests — the codebase has no pytest infra; verification uses a probe script matching the existing `test_pulse.py` / `sample_*.py` pattern.

**Scope note (Stage 1):** This plan covers only the bridge worker side of the design. The synthesis-routine prompt changes (parallel sub-agent dispatch, lint, writing the adjudication JSON) live on Claude.ai and are *not* in this repo, so they are out of scope here. Stage 1 makes the worker safe to run *before* the routine is updated — when the routine isn't yet writing adjudication files, the worker silently does nothing extra.

---

## File Structure

| File | Change |
|---|---|
| `github_bridge/jobs.py` | Modify: add 2 path constants, 1 helper function, edit `_process_one_pulse` in 3 places |
| `probe_bridge_adjudication.py` | Create: top-level probe script that verifies the full path with a fake bridge client (matches existing `test_pulse.py` / `sample_*.py` pattern; no pytest) |

No other files touched. The change is intentionally small because all the heavy lifting (sub-agent orchestration, lint) lives in the routine prompt on Claude.ai.

---

## Task 1: Add adjudication path constants

**Files:**
- Modify: `github_bridge/jobs.py:37-39`

- [ ] **Step 1: Add `PENDING_ADJUDICATIONS_DIR` and `ARCHIVE_ADJUDICATIONS_DIR` constants**

Find this block (currently lines 37-39):

```python
CONTEXT_PATH = "pulse-context/latest.json"
PENDING_DIR = "pulse-output/pending"
ARCHIVE_DIR = "pulse-output/archive"
```

Replace with:

```python
CONTEXT_PATH = "pulse-context/latest.json"
PENDING_DIR = "pulse-output/pending"
ARCHIVE_DIR = "pulse-output/archive"
PENDING_ADJUDICATIONS_DIR = "pulse-output/pending-adjudications"
ARCHIVE_ADJUDICATIONS_DIR = "pulse-output/archive-adjudications"
```

- [ ] **Step 2: Verify the file imports cleanly**

Run: `python -c "from github_bridge.jobs import PENDING_ADJUDICATIONS_DIR, ARCHIVE_ADJUDICATIONS_DIR; print(PENDING_ADJUDICATIONS_DIR, ARCHIVE_ADJUDICATIONS_DIR)"`
Expected: `pulse-output/pending-adjudications pulse-output/archive-adjudications`

- [ ] **Step 3: Commit**

```bash
git add github_bridge/jobs.py
git commit -m "Bridge: add pending/archive adjudications dir constants"
```

---

## Task 2: Add `_fetch_matching_adjudication` helper

**Files:**
- Modify: `github_bridge/jobs.py` — insert helper after `_parse_frontmatter` (currently ends at line 236), before `_process_one_pulse` (currently starts at line 239)

- [ ] **Step 1: Add the helper function**

Find this block (currently lines 236-239):

```python
            meta[k] = v
    return meta, body


async def _process_one_pulse(bot, item: dict[str, Any]) -> None:
```

Replace with:

```python
            meta[k] = v
    return meta, body


def _fetch_matching_adjudication(pulse_md_name: str) -> tuple[dict | None, str | None]:
    """For pulse markdown filename '<base>.md', look for the matching adjudication
    JSON at PENDING_ADJUDICATIONS_DIR/<base>.json.

    Returns (parsed_dict, raw_text):
      - (None, None)         file is absent (404 from the bridge)
      - (dict, raw_text)     file present and parses as JSON
      - (None, raw_text)     file present but JSON malformed — caller can still
                             archive the raw form for inspection; pulse posting
                             continues without an adjudication payload.

    Never raises. The bridge worker must not lose a pulse over an adjudication
    fetch issue.
    """
    if not pulse_md_name.endswith(".md"):
        return None, None
    base = pulse_md_name[:-3]
    adj_path = f"{PENDING_ADJUDICATIONS_DIR}/{base}.json"
    try:
        raw = gh.get_file_text(adj_path)
    except Exception as e:
        log.warning(f"Bridge: error fetching adjudication {adj_path}: {e}")
        return None, None
    if not raw:
        return None, None
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as e:
        log.warning(f"Bridge: adjudication file {adj_path} present but malformed JSON: {e}")
        return None, raw


async def _process_one_pulse(bot, item: dict[str, Any]) -> None:
```

- [ ] **Step 2: Verify the helper imports cleanly**

Run: `python -c "from github_bridge.jobs import _fetch_matching_adjudication; print(_fetch_matching_adjudication.__doc__.splitlines()[0])"`
Expected: `For pulse markdown filename '<base>.md', look for the matching adjudication`

- [ ] **Step 3: Commit**

```bash
git add github_bridge/jobs.py
git commit -m "Bridge: add _fetch_matching_adjudication helper"
```

---

## Task 3: Wire fetch + embed adjudication into `DailyReport.raw_json`

**Files:**
- Modify: `github_bridge/jobs.py` inside `_process_one_pulse` — add fetch call after `_parse_frontmatter`, change `raw_json` construction so `report.raw_json` carries the adjudication when present.

- [ ] **Step 1: Fetch adjudication after parsing frontmatter**

Find this block (currently around lines 250-257 inside `_process_one_pulse`):

```python
        # Parse optional frontmatter for accurate pdf_count + token usage
        meta, markdown = _parse_frontmatter(raw_markdown)

        today = date.today().isoformat()
        pdf_count = int(meta.get("pdf_count", 0))
        input_tokens = int(meta.get("input_tokens", 0))
        output_tokens = int(meta.get("output_tokens", 0))
```

Replace with:

```python
        # Parse optional frontmatter for accurate pdf_count + token usage
        meta, markdown = _parse_frontmatter(raw_markdown)

        # Fetch the matching adjudication JSON if the routine produced one.
        # Returns (None, None) cleanly when absent, so this is a no-op for
        # pulses produced before the adjudication step was wired into the
        # routine prompt. Captured here so we can both embed it into the
        # DailyReport and archive it alongside the pulse markdown later.
        parsed_adj, raw_adj = _fetch_matching_adjudication(name)
        if parsed_adj is not None:
            adj_themes = len(parsed_adj.get("themes", []) or [])
            adj_discarded = len(parsed_adj.get("discarded_themes", []) or [])
            log.info(
                f"Bridge: matched adjudication for {name} — "
                f"{adj_themes} themes, {adj_discarded} discarded"
            )

        today = date.today().isoformat()
        pdf_count = int(meta.get("pdf_count", 0))
        input_tokens = int(meta.get("input_tokens", 0))
        output_tokens = int(meta.get("output_tokens", 0))
```

- [ ] **Step 2: Embed adjudication into `DailyReport.raw_json`**

Find this block (currently around lines 268-277 inside `_process_one_pulse`):

```python
        report = DailyReport(
            report_date=today,
            report_type="daily",  # routine pulses replace the scheduled Gemini one
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json={"source": "github_bridge", "pending_file": name, **meta},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stats=stats,
        )
```

Replace with:

```python
        raw_json_payload: dict[str, Any] = {
            "source": "github_bridge",
            "pending_file": name,
            **meta,
        }
        if parsed_adj is not None:
            raw_json_payload["adjudication"] = parsed_adj

        report = DailyReport(
            report_date=today,
            report_type="daily",  # routine pulses replace the scheduled Gemini one
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json=raw_json_payload,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stats=stats,
        )
```

- [ ] **Step 3: Mirror adjudication into the persisted `report_json`**

Find this block (currently around lines 280-288 inside `_process_one_pulse`):

```python
        # Persist before posting so we don't lose track if Discord errors
        report_id = db.insert_daily_report(
            report_date=today,
            report_type="daily",
            report_json=json.dumps(report.raw_json),
            report_markdown=markdown,
            pdf_count=pdf_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
```

This block does NOT need to change — `report.raw_json` is now the dict that includes the adjudication, and `json.dumps` will serialize it. Confirm this by reading and proceed.

- [ ] **Step 4: Verify the file imports cleanly**

Run: `python -c "from github_bridge.jobs import _process_one_pulse; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add github_bridge/jobs.py
git commit -m "Bridge: embed matching adjudication JSON into DailyReport.raw_json"
```

---

## Task 4: Archive adjudication alongside the pulse + clean up pending

**Files:**
- Modify: `github_bridge/jobs.py` inside `_process_one_pulse` — add adjudication archive + delete after the existing pulse archive + delete block.

- [ ] **Step 1: Add adjudication archive/cleanup after the pulse archive block**

Find this block (currently around lines 329-343 inside `_process_one_pulse`):

```python
        # Archive the pending file regardless of channel success — the pulse is
        # in daily_reports table; we don't want to repost it on next poll.
        # Archive the raw form (with frontmatter) so we keep the metadata.
        await asyncio.to_thread(
            gh.put_file,
            archive_path,
            raw_markdown,
            f"bridge: archive posted pulse {name} ({channels_sent} ch)",
        )
        await asyncio.to_thread(
            gh.delete_file,
            pending_path,
            f"bridge: remove pending {name} after posting",
        )
        log.info(f"Bridge: archived {name} (posted to {channels_sent} channels)")
```

Replace with:

```python
        # Archive the pending file regardless of channel success — the pulse is
        # in daily_reports table; we don't want to repost it on next poll.
        # Archive the raw form (with frontmatter) so we keep the metadata.
        await asyncio.to_thread(
            gh.put_file,
            archive_path,
            raw_markdown,
            f"bridge: archive posted pulse {name} ({channels_sent} ch)",
        )
        await asyncio.to_thread(
            gh.delete_file,
            pending_path,
            f"bridge: remove pending {name} after posting",
        )
        log.info(f"Bridge: archived {name} (posted to {channels_sent} channels)")

        # Archive the matching adjudication file if one was retrieved. Errors
        # here must NOT cascade — the pulse is already posted and persisted.
        # Worst case: the adjudication file stays in pending-adjudications/
        # and gets cleaned up next cycle (the worker only matches by pulse
        # markdown, so an orphaned adjudication does no harm).
        if raw_adj is not None and name.endswith(".md"):
            base = name[:-3]
            adj_pending = f"{PENDING_ADJUDICATIONS_DIR}/{base}.json"
            adj_archive = f"{ARCHIVE_ADJUDICATIONS_DIR}/{base}.json"
            try:
                await asyncio.to_thread(
                    gh.put_file,
                    adj_archive,
                    raw_adj,
                    f"bridge: archive adjudication for {name}",
                )
                await asyncio.to_thread(
                    gh.delete_file,
                    adj_pending,
                    f"bridge: remove pending adjudication for {name}",
                )
                log.info(f"Bridge: archived adjudication {base}.json")
            except Exception as e:
                log.warning(
                    f"Bridge: failed to archive adjudication for {name}: {e} "
                    f"(pulse already posted; adjudication will retry next cycle)"
                )
```

- [ ] **Step 2: Verify the file imports cleanly**

Run: `python -c "from github_bridge.jobs import _process_one_pulse; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add github_bridge/jobs.py
git commit -m "Bridge: archive adjudication alongside pulse, with isolated failure handling"
```

---

## Task 5: Add probe script and verify end-to-end

**Files:**
- Create: `probe_bridge_adjudication.py`

The codebase has no pytest infra; instead we follow the existing `test_pulse.py` / `sample_*.py` convention — a runnable script with inline assertions that prints `PASS` / `FAIL` to stdout. This probe stubs the `gh` HTTP client and the `db` / Discord layers in-memory and exercises `_process_one_pulse` against three scenarios.

- [ ] **Step 1: Create `probe_bridge_adjudication.py`**

Write the following file at the project root (`c:/Users/gabje/Institutional-report-bot/probe_bridge_adjudication.py`):

```python
"""Probe: verify github_bridge.jobs._process_one_pulse correctly handles the
matching adjudication JSON in three scenarios:

  1. Pending pulse + matching valid adjudication        → archive both, embed in raw_json
  2. Pending pulse + no adjudication                    → archive pulse only, no adjudication key
  3. Pending pulse + malformed adjudication             → archive pulse, archive raw adjudication, log warning, raw_json has no adjudication key

Run:  python probe_bridge_adjudication.py
Expected output ends with: 'PROBE PASSED'
Any AssertionError or unexpected exception = failure.

Stubs the github_bridge.client functions and the discord/db layers in-memory.
"""

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# --- in-memory fakes ---------------------------------------------------------

class FakeBridge:
    """In-memory stand-in for github_bridge.client functions."""
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.deleted: list[str] = []
        self.put_calls: list[tuple[str, str]] = []

    def get_file_text(self, path: str, ref: str | None = None) -> str | None:
        return self.files.get(path)

    def put_file(self, path: str, content: str, message: str, ref: str | None = None) -> dict:
        self.files[path] = content
        self.put_calls.append((path, content))
        return {"commit": {"sha": "deadbeef"}}

    def delete_file(self, path: str, message: str, ref: str | None = None) -> dict | None:
        if path in self.files:
            del self.files[path]
            self.deleted.append(path)
            return {"commit": {"sha": "deadbeef"}}
        return None


class FakeChannel:
    name = "test-channel"

    async def send(self, *args, **kwargs):
        return None


class FakeBot:
    def get_channel(self, cid):
        return FakeChannel()


# --- patch hooks -------------------------------------------------------------

import github_bridge.jobs as jobs_mod
import github_bridge.client as client_mod
import db as db_mod
from config import settings

# Track DB inserts
inserted_reports: list[dict] = []

def fake_insert_daily_report(**kwargs):
    inserted_reports.append(kwargs)
    return 999

def fake_mark_report_sent(report_id: int):
    pass

# Stub the synthesizer footer-stats path (it queries the real DB)
def fake_compute_footer_stats():
    return {"pdf_count": 0, "top_sources": [], "priority_mix": {}}

# Stub send_embeds (avoid Discord)
async def fake_send_embeds(channel, embeds):
    return True

# Stub format_report_embeds (avoid building real embeds — just return a list)
def fake_format_report_embeds(report):
    return ["embed-stub"]


def install_stubs(fake: FakeBridge):
    # gh IS github_bridge.client (alias inside jobs_mod). Patch its functions
    # via the alias — same effect as patching client_mod directly.
    jobs_mod.gh.get_file_text = fake.get_file_text
    jobs_mod.gh.put_file = fake.put_file
    jobs_mod.gh.delete_file = fake.delete_file
    # format_report_embeds and send_embeds are imported as bare names into
    # jobs_mod. Python resolves these against jobs_mod's symbol table at
    # call time, so patching the source module would NOT take effect — we
    # must patch the bound names on jobs_mod itself.
    jobs_mod.format_report_embeds = fake_format_report_embeds
    jobs_mod.send_embeds = fake_send_embeds
    # db is `import db` (module-level), so attribute lookup goes through
    # the module — patching db_mod is fine.
    db_mod.insert_daily_report = fake_insert_daily_report
    db_mod.mark_report_sent = fake_mark_report_sent
    jobs_mod._compute_footer_stats = fake_compute_footer_stats
    # Force a single-channel config so the loop runs once.
    settings.discord_channel_ids = [12345]


# --- scenario runners --------------------------------------------------------

PULSE_NAME = "2026-05-07T13-00.md"
PULSE_MD = """---
pdf_count: 187
input_tokens: 350000
output_tokens: 5200
---

# Pulse content placeholder
"""

VALID_ADJ = {
    "pulse_date": "2026-05-07",
    "window_label": "since-last-daily (2026-05-06 13:00)",
    "themes": [
        {
            "theme": "hormuz oil shock",
            "selected": True,
            "stance_counts": {"supportive": 5, "skeptical": 1, "neutral": 3},
            "consensus_view": "Supply scarcity priced; cuts coming Q2.",
            "facts_agreed": [],
            "facts_contested": [],
            "falsifiable_predictions": [],
        }
    ],
    "discarded_themes": [],
}


async def run_scenario(label: str, include_adj: bool, malformed: bool) -> None:
    print(f"\n--- scenario: {label} ---")
    inserted_reports.clear()

    fake = FakeBridge()
    fake.files[f"{jobs_mod.PENDING_DIR}/{PULSE_NAME}"] = PULSE_MD
    if include_adj:
        adj_payload = "{this is not json" if malformed else json.dumps(VALID_ADJ)
        fake.files[f"{jobs_mod.PENDING_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"] = adj_payload

    install_stubs(fake)

    item = {"name": PULSE_NAME, "type": "file"}
    await jobs_mod._process_one_pulse(FakeBot(), item)

    # Assertions ---------------------------------------------------------------
    archive_path = f"{jobs_mod.ARCHIVE_DIR}/{PULSE_NAME}"
    pending_path = f"{jobs_mod.PENDING_DIR}/{PULSE_NAME}"
    adj_archive_path = f"{jobs_mod.ARCHIVE_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"
    adj_pending_path = f"{jobs_mod.PENDING_ADJUDICATIONS_DIR}/2026-05-07T13-00.json"

    assert archive_path in fake.files, f"pulse markdown not archived for scenario {label}"
    assert pending_path in fake.deleted, f"pending pulse not deleted for scenario {label}"
    assert len(inserted_reports) == 1, f"expected exactly 1 db insert, got {len(inserted_reports)}"
    raw_json = json.loads(inserted_reports[0]["report_json"])
    print(f"  raw_json keys: {sorted(raw_json.keys())}")

    if include_adj and not malformed:
        assert "adjudication" in raw_json, f"valid adj missing from raw_json in scenario {label}"
        assert raw_json["adjudication"]["pulse_date"] == "2026-05-07"
        assert adj_archive_path in fake.files, f"adjudication not archived for scenario {label}"
        assert adj_pending_path in fake.deleted, f"pending adjudication not deleted for scenario {label}"
        print("  [OK] adjudication archived + embedded in raw_json")
    elif include_adj and malformed:
        assert "adjudication" not in raw_json, f"malformed adj should not be in raw_json"
        # We still archive the raw form for inspection
        assert adj_archive_path in fake.files, f"raw malformed adj not archived for scenario {label}"
        print("  [OK] malformed adjudication: raw archived, raw_json clean")
    else:
        assert "adjudication" not in raw_json, f"raw_json should not have adjudication key"
        assert adj_archive_path not in fake.files, f"no adjudication should have been archived"
        print("  [OK] no adjudication: pulse unaffected")


async def main() -> None:
    await run_scenario("valid adjudication present", include_adj=True, malformed=False)
    await run_scenario("no adjudication file", include_adj=False, malformed=False)
    await run_scenario("malformed adjudication file", include_adj=True, malformed=True)
    print("\nPROBE PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print(f"\nPROBE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\nPROBE FAILED (unexpected): {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(2)
```

- [ ] **Step 2: Run the probe**

Run: `python probe_bridge_adjudication.py`
Expected: ends with `PROBE PASSED`. Any `PROBE FAILED` line, AssertionError, or non-zero exit code = failure.

- [ ] **Step 3: Commit**

```bash
git add probe_bridge_adjudication.py
git commit -m "Bridge: probe script for adjudication wiring (3 scenarios)"
```

---

## Verification Checklist (run after all tasks complete)

- [ ] `python -c "from github_bridge.jobs import _process_one_pulse, _fetch_matching_adjudication, PENDING_ADJUDICATIONS_DIR, ARCHIVE_ADJUDICATIONS_DIR; print('ok')"` prints `ok`
- [ ] `python probe_bridge_adjudication.py` prints `PROBE PASSED`
- [ ] `git log --oneline -6` shows 5 new commits (one per task)
- [ ] Running `git diff <pre-task1-sha> -- github_bridge/jobs.py` shows: 2 new constants, 1 new helper, 3 modifications inside `_process_one_pulse` (fetch + raw_json + archive), no other changes.

## Out of Scope (explicit)

- The synthesis-routine prompt change on Claude.ai (theme selection, parallel sub-agent dispatch, lint, writing the adjudication JSON to `pulse-output/pending-adjudications/`). That work is what *produces* the file the worker now consumes; it's a separate piece tracked outside this repo.
- Stage 2 prose-prompt rewrite (consume adjudications as primary DRAFT input).
- Diff-framing changes in `report/synthesizer.build_pulse_context` to fetch yesterday's archived adjudication file. (The bridge layout this plan creates is what enables that work later.)
- `inspect_db.py` updates to surface adjudication blocks. The data is already queryable via the `report_json` column; adding a pretty-printed view is a follow-up if it turns out to be needed.
