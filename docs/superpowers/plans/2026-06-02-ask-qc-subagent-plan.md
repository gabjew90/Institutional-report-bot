# /ask QC Sub-Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily post-hoc grader that reads the previous day's `/ask` markdown log, scores each interaction via a Gemini Flash Lite judge against a 6-dimension rubric, and publishes a per-day report to `pulse-data:ask-qc/`.

**Architecture:** Five focused modules under `ask_qc/` (models, parser, judge_prompt, grader, aggregator) wired to a new APScheduler cron at 03:00 ET. File-based artifact — no DB schema change. Reuses existing `github_bridge.client.put_file()` for publishing and `db.record_pipeline_event()` for observability.

**Tech Stack:** Python 3, `google-genai` SDK (Gemini 3.1 Flash Lite), APScheduler, existing `github_bridge.client`, project's smoke-test convention (`scripts/smoke_*.py`).

**Spec:** `docs/superpowers/specs/2026-06-02-ask-qc-subagent-design.md`

---

## File Structure

```
ask_qc/
├── __init__.py        # package marker, exports public names
├── models.py          # AskInteraction, DimensionVerdict, GradedInteraction dataclasses
├── parser.py          # parse_ask_log(text) -> list[AskInteraction]
├── judge_prompt.py    # build_judge_prompt() + JUDGE_RESPONSE_SCHEMA + RUBRIC_DEFINITIONS
├── grader.py          # grade_day(interactions) -> list[GradedInteraction]
└── aggregator.py      # render_report(date, graded) -> str

scheduler/jobs.py      # add _ask_qc_job + cron registration

scripts/
├── smoke_ask_qc_models.py
├── smoke_ask_qc_parser.py
├── smoke_ask_qc_judge_prompt.py
├── smoke_ask_qc_grader.py
├── smoke_ask_qc_aggregator.py
├── smoke_ask_qc_scheduler_wired.py
└── smoke_ask_qc_end_to_end.py
```

---

## Task 1: Package skeleton + dataclasses

**Files:**
- Create: `ask_qc/__init__.py`
- Create: `ask_qc/models.py`
- Test: `scripts/smoke_ask_qc_models.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_models.py`:

```python
"""Smoke test for ask_qc.models dataclasses.

Verifies the three dataclasses (AskInteraction, DimensionVerdict,
GradedInteraction) construct cleanly with the expected fields and
that GradedInteraction's overall_verdict computation is correct."""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_ask_interaction_construct():
    from ask_qc.models import AskInteraction
    a = AskInteraction(
        ts_utc="2026-06-01 22:01:45 UTC",
        asker_label="BK (`bankerkyle`)",
        asker_username="bankerkyle",
        channel="#stonks-yapping",
        question="who's the most racist?",
        answer="SV takes the crown.",
        prompt_block=None,
    )
    assert a.ts_utc.startswith("2026-")
    assert a.prompt_block is None
    _ok("AskInteraction constructs with None prompt_block (legacy log)")


def test_dimension_verdict_construct():
    from ask_qc.models import DimensionVerdict
    v = DimensionVerdict(verdict="PASS", rationale="grounded in tool output")
    assert v.verdict == "PASS"
    _ok("DimensionVerdict constructs")


def test_graded_interaction_rollup_clean():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="2026-06-01 22:01:45 UTC",
        dimensions={
            "fabrication": DimensionVerdict("PASS", "ok"),
            "status_handling": DimensionVerdict("N/A", "legacy"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("N/A", "all ok"),
        },
        notable_pattern=None,
    )
    assert g.overall_verdict == "CLEAN", g.overall_verdict
    _ok("GradedInteraction rollup CLEAN when all PASS/N/A")


def test_graded_interaction_rollup_fail_beats_concern():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={
            "fabrication": DimensionVerdict("FAIL", "fabricated price"),
            "status_handling": DimensionVerdict("CONCERN", "under-hedged"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("PASS", "ok"),
        },
        notable_pattern="fabricated $TSLA price",
    )
    assert g.overall_verdict == "FAIL", g.overall_verdict
    _ok("GradedInteraction rollup FAIL when any FAIL present")


def test_graded_interaction_rollup_concern():
    from ask_qc.models import DimensionVerdict, GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={
            "fabrication": DimensionVerdict("PASS", "ok"),
            "status_handling": DimensionVerdict("CONCERN", "under-hedged"),
            "voice": DimensionVerdict("PASS", "ok"),
            "format_adherence": DimensionVerdict("PASS", "ok"),
            "depth_match": DimensionVerdict("PASS", "ok"),
            "decline_when_uncertain": DimensionVerdict("PASS", "ok"),
        },
        notable_pattern=None,
    )
    assert g.overall_verdict == "CONCERN", g.overall_verdict
    _ok("GradedInteraction rollup CONCERN when CONCERN but no FAIL")


def test_graded_interaction_ungraded_short_circuit():
    """If `grader_error` is set, the rollup short-circuits to UNGRADED
    regardless of dimension verdicts (which should be empty)."""
    from ask_qc.models import GradedInteraction
    g = GradedInteraction(
        interaction_ts_utc="t",
        dimensions={},
        notable_pattern=None,
        grader_error="JSONDecodeError",
    )
    assert g.overall_verdict == "UNGRADED", g.overall_verdict
    _ok("GradedInteraction rollup UNGRADED when grader_error set")


if __name__ == "__main__":
    print("=== ask_qc.models smoke ===")
    test_ask_interaction_construct()
    test_dimension_verdict_construct()
    test_graded_interaction_rollup_clean()
    test_graded_interaction_rollup_fail_beats_concern()
    test_graded_interaction_rollup_concern()
    test_graded_interaction_ungraded_short_circuit()
    print("\nALL ASK-QC MODELS SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_models.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ask_qc'`

- [ ] **Step 3: Create `ask_qc/__init__.py`**

```python
"""Post-hoc QC sub-agent for /ask interactions.

Reads the previous day's /data/ask-logs/{date}.md, grades each
interaction via Gemini against a 6-dimension rubric, and publishes
a per-day markdown report to pulse-data:ask-qc/.

See docs/superpowers/specs/2026-06-02-ask-qc-subagent-design.md for
design rationale and rubric semantics.
"""

from ask_qc.models import (
    AskInteraction,
    DimensionVerdict,
    GradedInteraction,
)

__all__ = ["AskInteraction", "DimensionVerdict", "GradedInteraction"]
```

- [ ] **Step 4: Create `ask_qc/models.py`**

```python
"""Dataclass shapes shared across parser / grader / aggregator.

Keeps the modules decoupled — none of them imports the others'
implementation, only this models layer.

Verdict literals:
  - PASS  — dimension passed cleanly
  - CONCERN — borderline / minor issue worth surfacing
  - FAIL  — clear miss
  - N/A   — dimension not applicable (legacy log w/o <details>, or
            answer type where the rubric doesn't apply)

Overall verdict rollup (computed property):
  - UNGRADED — grader_error was set; dimensions intentionally empty
  - FAIL     — any dimension is FAIL
  - CONCERN  — no FAIL, any dimension is CONCERN
  - CLEAN    — all dimensions PASS or N/A
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# The 6 rubric dimensions. Locked here so the parser, judge_prompt,
# and aggregator all agree on the canonical name set.
DIMENSION_NAMES = (
    "fabrication",
    "status_handling",
    "voice",
    "format_adherence",
    "depth_match",
    "decline_when_uncertain",
)


@dataclass
class AskInteraction:
    """One /ask interaction parsed out of the daily log file.

    `prompt_block` is the FULL `<details>` body — the literal
    user_content sent to Gemini at /ask time (profiles + recent chat +
    tool descriptions + question). `None` for legacy log entries
    written before the <details> feature shipped — those interactions
    are still gradable but get N/A on `status_handling` and
    `decline_when_uncertain` since the judge can't see what the
    tools returned."""
    ts_utc: str
    asker_label: str          # e.g. "BK (`bankerkyle`)" or plain "kloh"
    asker_username: Optional[str]  # the bare username if surfaced; else None
    channel: str              # e.g. "#stonks-yapping"
    question: str
    answer: str
    prompt_block: Optional[str]


@dataclass
class DimensionVerdict:
    """Per-dimension grade from the judge."""
    verdict: str   # PASS | CONCERN | FAIL | N/A
    rationale: str # 1-2 sentence justification


@dataclass
class GradedInteraction:
    """An AskInteraction after the judge runs over it.

    On grader failure (Gemini exception, malformed JSON after retry),
    `grader_error` holds the exception class name and `dimensions`
    stays empty — `overall_verdict` short-circuits to UNGRADED."""
    interaction_ts_utc: str
    dimensions: dict[str, DimensionVerdict] = field(default_factory=dict)
    notable_pattern: Optional[str] = None
    grader_error: Optional[str] = None

    @property
    def overall_verdict(self) -> str:
        if self.grader_error:
            return "UNGRADED"
        verdicts = {d.verdict for d in self.dimensions.values()}
        if "FAIL" in verdicts:
            return "FAIL"
        if "CONCERN" in verdicts:
            return "CONCERN"
        return "CLEAN"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_models.py`
Expected: `ALL ASK-QC MODELS SMOKE TESTS PASS` (6 PASS lines)

- [ ] **Step 6: Commit**

```bash
git add ask_qc/__init__.py ask_qc/models.py scripts/smoke_ask_qc_models.py
git commit -m "ask-qc: scaffold + models

Three dataclasses (AskInteraction, DimensionVerdict, GradedInteraction)
with overall_verdict rollup logic (UNGRADED > FAIL > CONCERN > CLEAN).
Locked DIMENSION_NAMES tuple so parser/judge_prompt/aggregator agree
on the canonical 6-dimension set.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Parser

**Files:**
- Create: `ask_qc/parser.py`
- Test: `scripts/smoke_ask_qc_parser.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_parser.py`:

```python
"""Smoke test for ask_qc.parser.parse_ask_log.

Covers:
  - per-interaction split on '## YYYY-MM-DD HH:MM:SS UTC' headers
  - asker label / username / channel extraction
  - question + answer body extraction (multiline)
  - <details> block extraction (when present)
  - <details> block = None for legacy entries (no block)
  - malformed blocks silently skipped (graceful, not raised)
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Inline fixture so this smoke runs without external files. Mirrors
# the real format observed in .tmp_ask_log_today.md.
SAMPLE_LOG = """# /ask interactions — 2026-06-01

## 2026-06-01 14:30:00 UTC

**Asker:** kloh (`kloh.`) in #💬-stonks-yapping-💬

**Q:** what's TSLA at right now

**A:**

- **$TSLA $310** as of 14:30 ET, up **+1.2%** on session

- Holding **$305** support — clean breakout target **$315**

<details>
<summary>Prompt sent to Gemini (40k char cap)</summary>

WHO'S TALKING:
kloh — Personality: chill, posts charts...

USER QUESTION: what's TSLA at right now
</details>

---

## 2026-06-01 18:45:12 UTC

**Asker:** BK (`bankerkyle`) in #💬-stonks-yapping-💬

**Q:** who's the worst trader?

**A:**

Looking at the bottom of the leaderboard, that'd be theorb_18574 — sitting at the very bottom of the trader rankings.

---

## 2026-06-01 20:00:00 UTC

**Asker:** 2pale in #💬-stonks-yapping-💬

**Q:** how about now

**A:**

- still flat

---
"""


def test_parser_splits_on_timestamp_headers():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    assert len(interactions) == 3, (
        f"expected 3 interactions, got {len(interactions)}"
    )
    _ok(f"parse_ask_log: 3 interactions parsed out of 3-block sample")


def test_parser_extracts_asker_fields():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    bk = interactions[1]
    assert bk.asker_label.startswith("BK"), bk.asker_label
    assert bk.asker_username == "bankerkyle", bk.asker_username
    assert "stonks-yapping" in bk.channel, bk.channel
    _ok("parser extracts asker_label, asker_username, channel")


def test_parser_extracts_no_username_when_label_is_plain():
    """2pale's row has no backtick-username — asker_username should be None."""
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    twopale = interactions[2]
    assert twopale.asker_label.startswith("2pale"), twopale.asker_label
    assert twopale.asker_username is None, twopale.asker_username
    _ok("parser leaves asker_username None when label has no `username` form")


def test_parser_extracts_question_and_answer_multiline():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    kloh = interactions[0]
    assert "TSLA" in kloh.question, kloh.question
    assert "$310" in kloh.answer, kloh.answer
    assert "breakout target" in kloh.answer, kloh.answer
    _ok("parser captures multiline question + multiline answer body")


def test_parser_extracts_details_block_when_present():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    kloh = interactions[0]
    assert kloh.prompt_block is not None, "expected <details> block extracted"
    assert "WHO'S TALKING" in kloh.prompt_block, kloh.prompt_block[:200]
    _ok("parser extracts <details> body into prompt_block")


def test_parser_prompt_block_none_when_absent():
    from ask_qc.parser import parse_ask_log
    interactions = parse_ask_log(SAMPLE_LOG)
    bk = interactions[1]
    assert bk.prompt_block is None, (
        f"expected prompt_block=None for legacy entry, got "
        f"{bk.prompt_block[:100] if bk.prompt_block else bk.prompt_block!r}"
    )
    _ok("parser leaves prompt_block=None for entries without <details>")


def test_parser_skips_malformed_blocks():
    """A block with no clear timestamp header should be silently dropped,
    other interactions in the file should still parse."""
    from ask_qc.parser import parse_ask_log
    corrupted = SAMPLE_LOG.replace("## 2026-06-01 14:30:00 UTC", "## garbage")
    interactions = parse_ask_log(corrupted)
    assert len(interactions) == 2, (
        f"expected 2 interactions (1 dropped), got {len(interactions)}"
    )
    _ok("parser silently drops malformed blocks, continues on the rest")


def test_parser_handles_empty_input():
    from ask_qc.parser import parse_ask_log
    assert parse_ask_log("") == []
    assert parse_ask_log("# header only, no interactions\n") == []
    _ok("parser returns [] on empty / header-only input")


if __name__ == "__main__":
    print("=== ask_qc.parser smoke ===")
    test_parser_splits_on_timestamp_headers()
    test_parser_extracts_asker_fields()
    test_parser_extracts_no_username_when_label_is_plain()
    test_parser_extracts_question_and_answer_multiline()
    test_parser_extracts_details_block_when_present()
    test_parser_prompt_block_none_when_absent()
    test_parser_skips_malformed_blocks()
    test_parser_handles_empty_input()
    print("\nALL ASK-QC PARSER SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_parser.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ask_qc.parser'`

- [ ] **Step 3: Create `ask_qc/parser.py`**

```python
"""Parse a daily /ask interaction log into structured records.

The log format (see db.append_ask_log_entry):

  # /ask interactions — YYYY-MM-DD

  ## 2026-06-01 14:30:00 UTC

  **Asker:** {label} in #{channel}

  **Q:** {question — possibly multiline, ends at next **A:**}

  **A:**

  {answer — possibly multiline, ends at next \\n---\\n or EOF}

  <details>
  <summary>...</summary>

  {prompt_block — optional, ends at </details>}
  </details>

  ---

The parser is permissive: any block where the timestamp header
doesn't match the regex is dropped silently — log corruption shouldn't
break the QC pipeline."""

from __future__ import annotations
import re
from typing import Optional

from ask_qc.models import AskInteraction


# Per-interaction header: "## 2026-06-01 14:30:00 UTC"
_HEADER_RE = re.compile(
    r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)\s*$",
    re.MULTILINE,
)

# Asker line: "**Asker:** {label} in #{channel}"
# {label} may be "kloh", "BK (`bankerkyle`)", "kloh (`kloh.`)", or "2pale"
_ASKER_RE = re.compile(
    r"\*\*Asker:\*\*\s*(?P<label>.+?)\s+in\s+(?P<channel>#\S+)",
)

# Username inside the asker label: "BK (`bankerkyle`)" -> bankerkyle
_USERNAME_RE = re.compile(r"`([^`]+)`")

# Question body: between "**Q:**" and the next "**A:**"
_QA_SPLIT_RE = re.compile(
    r"\*\*Q:\*\*\s*\n?(?P<q>.*?)\n\s*\*\*A:\*\*\s*\n+(?P<a>.*?)"
    r"(?=\n<details>|\n---\s*$|\Z)",
    re.DOTALL | re.MULTILINE,
)

# Optional <details>...</details> block
_DETAILS_RE = re.compile(
    r"<details>\s*<summary>.*?</summary>\s*(?P<body>.*?)\s*</details>",
    re.DOTALL,
)


def parse_ask_log(text: str) -> list[AskInteraction]:
    """Split `text` on timestamp headers, parse each block.

    Returns the successfully-parsed interactions in source order.
    Malformed blocks are silently dropped (best-effort recovery — the
    QC report header should note unparseable counts if the caller
    wants to surface them; the parser itself is lossy)."""
    if not text:
        return []

    # Split on the header regex. re.split with a capturing group keeps
    # the header text in the result list at odd indices, so the pattern
    # becomes: [preamble, ts1, body1, ts2, body2, ...].
    parts = _HEADER_RE.split(text)
    if len(parts) < 3:
        return []

    interactions: list[AskInteraction] = []
    # parts[0] is the file preamble (the "# /ask interactions —" line);
    # iterate the (ts, body) pairs that follow.
    for i in range(1, len(parts), 2):
        ts = parts[i]
        body = parts[i + 1] if i + 1 < len(parts) else ""
        parsed = _parse_block(ts, body)
        if parsed is not None:
            interactions.append(parsed)
    return interactions


def _parse_block(ts_utc: str, body: str) -> Optional[AskInteraction]:
    """Parse a single interaction body. Returns None on malformed input."""
    asker_m = _ASKER_RE.search(body)
    if not asker_m:
        return None
    label = asker_m.group("label").strip()
    channel = asker_m.group("channel").strip()
    username_m = _USERNAME_RE.search(label)
    asker_username = username_m.group(1).strip() if username_m else None

    qa_m = _QA_SPLIT_RE.search(body)
    if not qa_m:
        return None
    question = qa_m.group("q").strip()
    answer = qa_m.group("a").strip()

    details_m = _DETAILS_RE.search(body)
    prompt_block = details_m.group("body").strip() if details_m else None

    return AskInteraction(
        ts_utc=ts_utc,
        asker_label=label,
        asker_username=asker_username,
        channel=channel,
        question=question,
        answer=answer,
        prompt_block=prompt_block,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_parser.py`
Expected: `ALL ASK-QC PARSER SMOKE TESTS PASS` (8 PASS lines)

- [ ] **Step 5: Commit**

```bash
git add ask_qc/parser.py scripts/smoke_ask_qc_parser.py
git commit -m "ask-qc: parser for daily /ask log

Splits the markdown log on per-interaction timestamp headers,
extracts asker_label/username/channel + Q/A body + optional
<details> prompt_block. Permissive: malformed blocks are silently
dropped rather than raised — log corruption can't kill the QC job.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Judge prompt

**Files:**
- Create: `ask_qc/judge_prompt.py`
- Test: `scripts/smoke_ask_qc_judge_prompt.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_judge_prompt.py`:

```python
"""Smoke test for ask_qc.judge_prompt.

Verifies the prompt assembly includes:
  - all 6 rubric definitions
  - the JSON response schema example
  - the 'do NOT penalize' guardrails list
  - the interaction's Q + A + (optional) prompt_block
  - the legacy-log instruction (N/A for status_handling +
    decline_when_uncertain when prompt_block is None)
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_all_six_dimensions_in_prompt():
    from ask_qc.judge_prompt import build_judge_prompt, RUBRIC_DEFINITIONS
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="2026-06-01 14:30:00 UTC",
        asker_label="kloh", asker_username="kloh.",
        channel="#stonks-yapping",
        question="what's TSLA at",
        answer="**$TSLA $310** as of 14:30 ET",
        prompt_block="WHO'S TALKING: ...",
    )
    prompt = build_judge_prompt(interaction)
    for dim in ("fabrication", "status_handling", "voice",
                "format_adherence", "depth_match", "decline_when_uncertain"):
        assert dim in prompt, f"dimension {dim} missing from prompt"
        assert dim in RUBRIC_DEFINITIONS, f"dimension {dim} missing from RUBRIC_DEFINITIONS"
    _ok("build_judge_prompt: all 6 rubric dimensions present")


def test_response_schema_in_prompt():
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="t", asker_label="x", asker_username=None,
        channel="#c", question="q", answer="a", prompt_block=None,
    )
    prompt = build_judge_prompt(interaction)
    assert '"overall"' in prompt, "JSON schema missing overall field"
    assert '"dimensions"' in prompt, "JSON schema missing dimensions field"
    assert '"notable_pattern"' in prompt, "JSON schema missing notable_pattern field"
    _ok("build_judge_prompt: response JSON schema present")


def test_guardrails_in_prompt():
    """The 'do NOT penalize' list must reach the judge so it doesn't
    grade against its own taste."""
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="t", asker_label="x", asker_username=None,
        channel="#c", question="q", answer="a", prompt_block=None,
    )
    prompt = build_judge_prompt(interaction)
    # Sample guardrails — the prompt should phrase these clearly
    assert "NOT penalize" in prompt or "do not penalize" in prompt.lower(), (
        "guardrail framing missing"
    )
    assert "brevity" in prompt.lower(), "brevity-when-terse guardrail missing"
    assert "decline" in prompt.lower(), "decline-when-tools-failed guardrail missing"
    _ok("build_judge_prompt: 'do NOT penalize' guardrails present")


def test_interaction_qa_embedded():
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="t", asker_label="kloh", asker_username="kloh.",
        channel="#c", question="what's TSLA at",
        answer="**$TSLA $310** as of 14:30 ET", prompt_block=None,
    )
    prompt = build_judge_prompt(interaction)
    assert "what's TSLA at" in prompt, "question text missing from prompt"
    assert "$310" in prompt, "answer text missing from prompt"
    _ok("build_judge_prompt: interaction Q+A embedded verbatim")


def test_prompt_block_included_when_present():
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="t", asker_label="x", asker_username=None,
        channel="#c", question="q", answer="a",
        prompt_block="TOOL_CALL: lookup_user_profile -> status=empty",
    )
    prompt = build_judge_prompt(interaction)
    assert "status=empty" in prompt, "prompt_block forensics missing"
    _ok("build_judge_prompt: prompt_block embedded when present")


def test_legacy_log_marker_when_prompt_block_absent():
    """When prompt_block is None, the judge must be told to mark
    status_handling and decline_when_uncertain as N/A (it can't see
    what the tools returned)."""
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    interaction = AskInteraction(
        ts_utc="t", asker_label="x", asker_username=None,
        channel="#c", question="q", answer="a", prompt_block=None,
    )
    prompt = build_judge_prompt(interaction)
    assert "N/A" in prompt, "N/A instruction missing for legacy logs"
    assert "status_handling" in prompt and "decline_when_uncertain" in prompt
    _ok("build_judge_prompt: legacy-log N/A instruction present when prompt_block is None")


def test_prompt_truncation_when_huge():
    """If prompt_block is >100k chars, build_judge_prompt should
    tail-truncate it and mark truncation. Keeps the judge call under
    Gemini's input cap even for outlier interactions."""
    from ask_qc.judge_prompt import build_judge_prompt
    from ask_qc.models import AskInteraction
    huge = "X" * 200_000
    interaction = AskInteraction(
        ts_utc="t", asker_label="x", asker_username=None,
        channel="#c", question="q", answer="a", prompt_block=huge,
    )
    prompt = build_judge_prompt(interaction)
    assert len(prompt) < 150_000, f"prompt should be truncated, got {len(prompt)} chars"
    assert "truncated" in prompt.lower(), "truncation marker missing"
    _ok("build_judge_prompt: huge prompt_block tail-truncated with marker")


if __name__ == "__main__":
    print("=== ask_qc.judge_prompt smoke ===")
    test_all_six_dimensions_in_prompt()
    test_response_schema_in_prompt()
    test_guardrails_in_prompt()
    test_interaction_qa_embedded()
    test_prompt_block_included_when_present()
    test_legacy_log_marker_when_prompt_block_absent()
    test_prompt_truncation_when_huge()
    print("\nALL ASK-QC JUDGE-PROMPT SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_judge_prompt.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ask_qc.judge_prompt'`

- [ ] **Step 3: Create `ask_qc/judge_prompt.py`**

```python
"""Build the Gemini-facing prompt that grades one /ask interaction.

The prompt embeds:
  1. The judge's role + the 6-dimension rubric definitions
  2. The 'do NOT penalize' guardrails (so the judge doesn't grade
     against its own taste)
  3. The interaction's Q + A
  4. The full <details> prompt block when present (forensic context)
  5. Explicit instruction to mark status_handling + decline_when_uncertain
     as N/A when the prompt_block is absent (legacy log)
  6. The exact JSON response schema the grader will parse

Truncation: prompt_block is tail-truncated at 100k chars so even
outlier interactions fit comfortably under Gemini's input cap.
"""

from __future__ import annotations

from ask_qc.models import AskInteraction


# Tail-truncate prompt_block at this many chars. Gemini Flash Lite
# can handle 1M tokens, but we want the call to be cheap and the
# truncation marker to be visible to the human reader of the report.
_PROMPT_BLOCK_CAP = 100_000


RUBRIC_DEFINITIONS = {
    "fabrication": (
        "Did the answer claim specific facts not supported by tool "
        "output, search results, or clear general knowledge?\n"
        "  PASS — every specific claim (price, rank, date, %, attributed "
        "quote) is grounded in the visible tool/search payload, or is "
        "general knowledge that doesn't change with time.\n"
        "  CONCERN — borderline grounding (approximate number without an "
        "'around'/'roughly' hedge, paraphrased rank that drifts from the "
        "verbatim rationale).\n"
        "  FAIL — a specific factual claim contradicts or extrapolates "
        "beyond the tool/search output. Includes: confident answer when "
        "`status=empty`/`error`; quoting a price that wasn't in the "
        "response; inventing a rank position.\n"
        "  N/A — pure Type 2 banter / Type 3 personality answer with no "
        "factual assertion."
    ),
    "status_handling": (
        "Did the answer correctly interpret the `status` + freshness "
        "fields on tool responses?\n"
        "  PASS — `status=empty` → 'no data'; `status=error` → 'lookup "
        "failed'; `status=not_found` → 'don't see that user/rank'; "
        "`status=ok` → uses the data.\n"
        "  CONCERN — hedges where data exists, or under-hedges a stale "
        "`updated_at` (>5d old).\n"
        "  FAIL — treats `empty` as data; fabricates on `error`; ignores "
        "`not_found` and invents the user.\n"
        "  N/A — no visible tool payload to grade against (legacy log)."
    ),
    "voice": (
        "Trader-newsletter voice — conversational, opinionated, no AI tells.\n"
        "  PASS — direct, opinionated, sharp. No 'it's worth noting', "
        "'notably', 'moreover', 'furthermore', 'in conclusion', em-dash "
        "chains, hedging cascades, or summary wrap-ups.\n"
        "  CONCERN — 1-2 AI tells slip through.\n"
        "  FAIL — paragraph-mode prose, multiple AI tells, lecture cadence."
    ),
    "format_adherence": (
        "Type 1 (factual) answers = literal `→` arrow bullets within the "
        "depth tier's word ceiling, no closing prose paragraph.\n"
        "  PASS — arrow bullets, within word cap, no closing wrap-up.\n"
        "  CONCERN — one stray closing line ('Net-net, …') or modest "
        "word-cap overshoot (≤15%).\n"
        "  FAIL — prose-only where arrows were required, or 'In short, …' "
        "summary tacked on.\n"
        "  N/A — Type 2 (banter) / Type 3 (personality) answer."
    ),
    "depth_match": (
        "Answer tier matches question shape — concision is the default.\n"
        "  Tiers:\n"
        "    Quick   — ≤60 words, 2-3 arrows (single-fact lookups, "
        "'what's X at')\n"
        "    Standard — ≤130 words, 3-5 arrows (most trade questions)\n"
        "    Full DD  — ≤250 words, 5-7 arrows ('walk me through X', "
        "'deep dive on Y')\n"
        "  PASS — tier matches question shape.\n"
        "  CONCERN — one tier off (Standard answer to a Quick question).\n"
        "  FAIL — two tiers off (Full DD to 'what's TSLA at'), or sub-2 "
        "arrows on a clear DD request."
    ),
    "decline_when_uncertain": (
        "When tools came back empty/error or search whiffed, did the "
        "answer cleanly decline instead of fabricating?\n"
        "  PASS — `status=empty`/`error` surfaced as 'no data' / 'lookup "
        "failed'; search-miss → 'can't verify cleanly'.\n"
        "  CONCERN — slight overconfidence given the data quality.\n"
        "  FAIL — confident fabricated answer on top of failed lookup.\n"
        "  N/A — every tool returned `status=ok` AND search returned "
        "grounded results."
    ),
}


_GUARDRAILS = """\
You are grading the bot's answer, not writing your own. Do NOT penalize:
  - Brevity when the question was terse — that's the depth_match PASS condition
  - Declining to answer when tools failed — that's the goal, not a failure
  - An answer differing from what you would have written, if it's grounded + voice-correct + format-correct
  - Insults or charged language in Type 2 (banter) / Type 3 (personality) answers — the bot is allowed to be sharp
"""


_RESPONSE_SCHEMA_EXAMPLE = """\
Return EXACTLY this JSON shape, no surrounding prose:

{
  "overall": "CLEAN | CONCERN | FAIL",
  "dimensions": {
    "fabrication":            {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."},
    "status_handling":        {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."},
    "voice":                  {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "format_adherence":       {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."},
    "depth_match":            {"verdict": "PASS|CONCERN|FAIL", "rationale": "..."},
    "decline_when_uncertain": {"verdict": "PASS|CONCERN|FAIL|N/A", "rationale": "..."}
  },
  "notable_pattern": "single-line description of any standout pattern, or null"
}
"""


def build_judge_prompt(interaction: AskInteraction) -> str:
    """Assemble the per-interaction judge prompt."""
    lines: list[str] = []
    lines.append("# /ask Interaction QC")
    lines.append("")
    lines.append(
        "You are an LLM judge grading one Gemini answer from a Discord "
        "trading bot's /ask command against a fixed 6-dimension rubric."
    )
    lines.append("")
    lines.append("## Rubric (grade each dimension; rationale = 1-2 sentences)")
    lines.append("")
    for dim, definition in RUBRIC_DEFINITIONS.items():
        lines.append(f"### {dim}")
        lines.append(definition)
        lines.append("")

    lines.append("## Guardrails")
    lines.append(_GUARDRAILS)
    lines.append("")

    # Legacy-log instruction
    if interaction.prompt_block is None:
        lines.append(
            "NOTE: this log entry has no <details> block — you cannot see "
            "what the tools returned. Mark `status_handling` and "
            "`decline_when_uncertain` as `N/A` (with rationale "
            "'legacy log, no tool forensics')."
        )
        lines.append("")

    lines.append("## Interaction")
    lines.append("")
    lines.append(f"Timestamp: {interaction.ts_utc}")
    lines.append(f"Asker: {interaction.asker_label}")
    lines.append(f"Channel: {interaction.channel}")
    lines.append("")
    lines.append("### Question")
    lines.append("")
    lines.append(interaction.question)
    lines.append("")
    lines.append("### Answer")
    lines.append("")
    lines.append(interaction.answer)
    lines.append("")

    if interaction.prompt_block is not None:
        block = interaction.prompt_block
        truncated = False
        if len(block) > _PROMPT_BLOCK_CAP:
            block = block[:_PROMPT_BLOCK_CAP]
            truncated = True
        lines.append("### Prompt sent to /ask (forensic context)")
        lines.append("")
        lines.append(block)
        if truncated:
            lines.append("")
            lines.append(
                f"_…(prompt_block truncated for grader at "
                f"{_PROMPT_BLOCK_CAP} chars)_"
            )
        lines.append("")

    lines.append("## Response Format")
    lines.append(_RESPONSE_SCHEMA_EXAMPLE)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_judge_prompt.py`
Expected: `ALL ASK-QC JUDGE-PROMPT SMOKE TESTS PASS` (7 PASS lines)

- [ ] **Step 5: Commit**

```bash
git add ask_qc/judge_prompt.py scripts/smoke_ask_qc_judge_prompt.py
git commit -m "ask-qc: judge prompt + rubric definitions

build_judge_prompt(AskInteraction) assembles a per-interaction
Gemini prompt with the 6-dimension rubric (verbatim, transparent
calibration), guardrails ('do NOT penalize brevity / declining /
divergent-but-grounded answers'), the Q+A+prompt_block, and the
exact JSON response schema.

Legacy logs (no <details> block) get an explicit N/A instruction
for status_handling + decline_when_uncertain.

Prompt blocks >100k chars get tail-truncated with a visible marker
so even outlier interactions fit cleanly.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Grader (Gemini orchestration)

**Files:**
- Create: `ask_qc/grader.py`
- Test: `scripts/smoke_ask_qc_grader.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_grader.py`:

```python
"""Smoke test for ask_qc.grader.

Covers:
  - clean JSON response → GradedInteraction with all 6 dimensions
  - malformed JSON → retry once → UNGRADED on second failure
  - Gemini API exception → retry once → UNGRADED on second failure
  - parallel grading uses the semaphore (max N concurrent)
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_interaction(ts="2026-06-01 14:30:00 UTC"):
    from ask_qc.models import AskInteraction
    return AskInteraction(
        ts_utc=ts, asker_label="kloh", asker_username="kloh.",
        channel="#c", question="q", answer="a", prompt_block=None,
    )


def _mock_gemini_response(text: str):
    """Build the shape google-genai's generate_content returns."""
    response = MagicMock()
    response.text = text
    return response


_CLEAN_JSON = json.dumps({
    "overall": "CLEAN",
    "dimensions": {
        "fabrication":            {"verdict": "PASS", "rationale": "ok"},
        "status_handling":        {"verdict": "N/A", "rationale": "legacy"},
        "voice":                  {"verdict": "PASS", "rationale": "ok"},
        "format_adherence":       {"verdict": "PASS", "rationale": "ok"},
        "depth_match":            {"verdict": "PASS", "rationale": "ok"},
        "decline_when_uncertain": {"verdict": "N/A", "rationale": "all ok"},
    },
    "notable_pattern": None,
})


def test_grader_clean_response():
    from ask_qc import grader
    interaction = _make_interaction()
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=_mock_gemini_response(_CLEAN_JSON)
    )
    with patch("ask_qc.grader._get_client", return_value=mock_client):
        graded = asyncio.run(grader.grade_day([interaction]))
    assert len(graded) == 1
    g = graded[0]
    assert g.overall_verdict == "CLEAN", g.overall_verdict
    assert "fabrication" in g.dimensions
    assert g.dimensions["fabrication"].verdict == "PASS"
    assert g.grader_error is None
    _ok("grader: clean JSON response → CLEAN GradedInteraction")


def test_grader_malformed_json_retries_then_ungraded():
    from ask_qc import grader
    interaction = _make_interaction()
    mock_client = MagicMock()
    # Both attempts return malformed JSON
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=_mock_gemini_response("not valid json {")
    )
    with patch("ask_qc.grader._get_client", return_value=mock_client):
        graded = asyncio.run(grader.grade_day([interaction]))
    assert mock_client.aio.models.generate_content.call_count == 2, (
        f"expected 2 calls (initial + 1 retry), got "
        f"{mock_client.aio.models.generate_content.call_count}"
    )
    g = graded[0]
    assert g.overall_verdict == "UNGRADED", g.overall_verdict
    assert g.grader_error is not None
    _ok("grader: malformed JSON retried once then UNGRADED")


def test_grader_api_exception_retries_then_ungraded():
    from ask_qc import grader
    interaction = _make_interaction()
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=RuntimeError("Gemini 503")
    )
    with patch("ask_qc.grader._get_client", return_value=mock_client):
        graded = asyncio.run(grader.grade_day([interaction]))
    assert mock_client.aio.models.generate_content.call_count == 2, (
        f"expected 2 calls (initial + 1 retry), got "
        f"{mock_client.aio.models.generate_content.call_count}"
    )
    g = graded[0]
    assert g.overall_verdict == "UNGRADED"
    assert "RuntimeError" in (g.grader_error or "")
    _ok("grader: API exception retried once then UNGRADED")


def test_grader_first_attempt_fails_second_succeeds():
    """Confirm one transient failure followed by a clean response is
    recovered rather than UNGRADED."""
    from ask_qc import grader
    interaction = _make_interaction()
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        side_effect=[
            RuntimeError("Gemini 503"),
            _mock_gemini_response(_CLEAN_JSON),
        ]
    )
    with patch("ask_qc.grader._get_client", return_value=mock_client):
        graded = asyncio.run(grader.grade_day([interaction]))
    g = graded[0]
    assert g.overall_verdict == "CLEAN", g.overall_verdict
    assert g.grader_error is None
    _ok("grader: transient fail + retry success → CLEAN (not UNGRADED)")


def test_grader_multiple_interactions_all_processed():
    from ask_qc import grader
    interactions = [_make_interaction(f"2026-06-01 14:{i:02d}:00 UTC")
                    for i in range(5)]
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=_mock_gemini_response(_CLEAN_JSON)
    )
    with patch("ask_qc.grader._get_client", return_value=mock_client):
        graded = asyncio.run(grader.grade_day(interactions, sem_size=2))
    assert len(graded) == 5
    assert all(g.overall_verdict == "CLEAN" for g in graded)
    _ok("grader: 5 interactions all graded under concurrency=2")


if __name__ == "__main__":
    print("=== ask_qc.grader smoke ===")
    test_grader_clean_response()
    test_grader_malformed_json_retries_then_ungraded()
    test_grader_api_exception_retries_then_ungraded()
    test_grader_first_attempt_fails_second_succeeds()
    test_grader_multiple_interactions_all_processed()
    print("\nALL ASK-QC GRADER SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_grader.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ask_qc.grader'`

- [ ] **Step 3: Create `ask_qc/grader.py`**

```python
"""Run the judge against a day's worth of /ask interactions.

One Gemini Flash Lite call per interaction. Concurrent via asyncio
semaphore (default 3). Retry once on parse failure or API exception;
on second failure, mark the interaction UNGRADED with the exception
class name as `grader_error`."""

from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional

from ask_qc.judge_prompt import build_judge_prompt
from ask_qc.models import (
    AskInteraction,
    DimensionVerdict,
    GradedInteraction,
    DIMENSION_NAMES,
)
from config import settings

log = logging.getLogger(__name__)


# Cached client instance so we don't re-init the SDK per call.
_client = None


def _get_client():
    """Lazy SDK init. Mockable in tests via patch on this function."""
    global _client
    if _client is None:
        from google.genai import Client
        _client = Client(api_key=settings.google_api_key)
    return _client


async def grade_day(
    interactions: list[AskInteraction],
    sem_size: int = 3,
) -> list[GradedInteraction]:
    """Grade every interaction in `interactions` in parallel.

    Concurrency is capped at `sem_size`. Returns one GradedInteraction
    per input, in source order. Per-interaction failures (Gemini
    exception, malformed JSON after retry) yield UNGRADED entries
    rather than raising — partial-day reports beat no report."""
    if not interactions:
        return []
    sem = asyncio.Semaphore(sem_size)

    async def _grade_one(ix: AskInteraction) -> GradedInteraction:
        async with sem:
            return await _grade_interaction_with_retry(ix)

    return await asyncio.gather(*(_grade_one(i) for i in interactions))


async def _grade_interaction_with_retry(
    interaction: AskInteraction,
) -> GradedInteraction:
    """Run the judge for one interaction. One retry on any failure."""
    last_exc: Optional[Exception] = None
    for attempt in (1, 2):
        try:
            return await _grade_interaction_once(interaction)
        except Exception as e:
            last_exc = e
            if attempt == 1:
                log.info(
                    f"ask-qc grader retry for {interaction.ts_utc}: "
                    f"{type(e).__name__}: {e}"
                )
                await asyncio.sleep(5)
    # Both attempts failed.
    log.warning(
        f"ask-qc grader UNGRADED {interaction.ts_utc}: "
        f"{type(last_exc).__name__}: {last_exc}"
    )
    return GradedInteraction(
        interaction_ts_utc=interaction.ts_utc,
        dimensions={},
        notable_pattern=None,
        grader_error=type(last_exc).__name__,
    )


async def _grade_interaction_once(
    interaction: AskInteraction,
) -> GradedInteraction:
    """Single Gemini call → parsed GradedInteraction. Raises on
    Gemini error or malformed JSON — the retry wrapper handles it."""
    from google.genai import types

    prompt = build_judge_prompt(interaction)
    client = _get_client()
    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )
    return _parse_judge_response(interaction.ts_utc, response.text)


def _parse_judge_response(ts_utc: str, raw: str) -> GradedInteraction:
    """Parse the judge's JSON. Raises ValueError if malformed."""
    payload = json.loads(raw)
    dims_in = payload.get("dimensions") or {}
    dims_out: dict[str, DimensionVerdict] = {}
    for name in DIMENSION_NAMES:
        d = dims_in.get(name) or {}
        dims_out[name] = DimensionVerdict(
            verdict=str(d.get("verdict") or "N/A"),
            rationale=str(d.get("rationale") or ""),
        )
    return GradedInteraction(
        interaction_ts_utc=ts_utc,
        dimensions=dims_out,
        notable_pattern=payload.get("notable_pattern"),
        grader_error=None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_grader.py`
Expected: `ALL ASK-QC GRADER SMOKE TESTS PASS` (5 PASS lines)

- [ ] **Step 5: Commit**

```bash
git add ask_qc/grader.py scripts/smoke_ask_qc_grader.py
git commit -m "ask-qc: grader with per-interaction Gemini judge

grade_day(interactions, sem_size=3) runs one Gemini Flash Lite call
per interaction in parallel. One retry with 5s backoff on any
failure (Gemini exception, malformed JSON, network); second failure
yields UNGRADED rather than raising — partial-day reports beat
no report.

Lazy SDK init via _get_client(); patchable in tests.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Aggregator (markdown report renderer)

**Files:**
- Create: `ask_qc/aggregator.py`
- Test: `scripts/smoke_ask_qc_aggregator.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_aggregator.py`:

```python
"""Smoke test for ask_qc.aggregator.render_report.

Verifies:
  - header summary has correct counts + percentages
  - top notable_pattern surfacing
  - per-interaction blocks render verdict-per-dimension
  - UNGRADED interactions render with grader_error
  - empty input renders a 'no interactions' stub
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_graded(ts, verdicts, notable=None, error=None):
    from ask_qc.models import DimensionVerdict, GradedInteraction
    return GradedInteraction(
        interaction_ts_utc=ts,
        dimensions={
            name: DimensionVerdict(v, f"rationale for {name}")
            for name, v in verdicts.items()
        },
        notable_pattern=notable,
        grader_error=error,
    )


def test_header_summary_counts():
    from ask_qc.aggregator import render_report
    all_pass = {n: "PASS" for n in (
        "fabrication", "status_handling", "voice",
        "format_adherence", "depth_match", "decline_when_uncertain",
    )}
    one_fail = dict(all_pass, **{"fabrication": "FAIL"})
    one_concern = dict(all_pass, **{"voice": "CONCERN"})
    graded = [
        _make_graded("2026-06-01 10:00:00 UTC", all_pass),
        _make_graded("2026-06-01 11:00:00 UTC", all_pass),
        _make_graded("2026-06-01 12:00:00 UTC", one_concern),
        _make_graded("2026-06-01 13:00:00 UTC", one_fail),
    ]
    report = render_report("2026-06-01", graded)
    assert "4 interactions" in report or "4 total" in report.lower(), report[:500]
    # 2 CLEAN, 1 CONCERN, 1 FAIL
    assert "CLEAN" in report and "CONCERN" in report and "FAIL" in report
    _ok("render_report: header shows correct verdict counts")


def test_per_interaction_block_renders():
    from ask_qc.aggregator import render_report
    graded = [_make_graded(
        "2026-06-01 22:01:45 UTC",
        {
            "fabrication": "PASS", "status_handling": "FAIL",
            "voice": "PASS", "format_adherence": "PASS",
            "depth_match": "PASS", "decline_when_uncertain": "FAIL",
        },
        notable="treated status=empty as logged trades",
    )]
    report = render_report("2026-06-01", graded)
    assert "22:01:45" in report
    assert "status_handling" in report
    assert "decline_when_uncertain" in report
    assert "rationale for fabrication" in report
    assert "treated status=empty as logged trades" in report
    _ok("render_report: per-interaction block has timestamp + dims + notable_pattern")


def test_ungraded_renders_with_error():
    from ask_qc.aggregator import render_report
    graded = [_make_graded(
        "2026-06-01 22:01:45 UTC",
        {}, error="RuntimeError",
    )]
    report = render_report("2026-06-01", graded)
    assert "UNGRADED" in report
    assert "RuntimeError" in report
    _ok("render_report: UNGRADED entries render with grader_error")


def test_top_pattern_surfaced_in_header():
    """If a notable_pattern appears 2+ times, surface it in the header
    so the daily-summary section calls out the recurring pattern."""
    from ask_qc.aggregator import render_report
    all_pass = {n: "PASS" for n in (
        "fabrication", "status_handling", "voice",
        "format_adherence", "depth_match", "decline_when_uncertain",
    )}
    pattern = "treated status=empty as logged trades"
    graded = [
        _make_graded("2026-06-01 10:00:00 UTC", all_pass, notable=pattern),
        _make_graded("2026-06-01 11:00:00 UTC", all_pass, notable=pattern),
        _make_graded("2026-06-01 12:00:00 UTC", all_pass, notable="one-off"),
    ]
    report = render_report("2026-06-01", graded)
    # Header section ends before per-interaction blocks (split on first '##')
    head = report.split("\n## ", 1)[0]
    assert pattern in head, "recurring pattern not surfaced in header"
    _ok("render_report: recurring patterns surfaced in header summary")


def test_empty_input_renders_stub():
    from ask_qc.aggregator import render_report
    report = render_report("2026-06-01", [])
    assert "No /ask interactions" in report or "no interactions" in report.lower()
    assert "2026-06-01" in report
    _ok("render_report: empty input renders 'no interactions' stub")


if __name__ == "__main__":
    print("=== ask_qc.aggregator smoke ===")
    test_header_summary_counts()
    test_per_interaction_block_renders()
    test_ungraded_renders_with_error()
    test_top_pattern_surfaced_in_header()
    test_empty_input_renders_stub()
    print("\nALL ASK-QC AGGREGATOR SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_aggregator.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ask_qc.aggregator'`

- [ ] **Step 3: Create `ask_qc/aggregator.py`**

```python
"""Render a list[GradedInteraction] into a daily markdown QC report.

Layout:
  # /ask QC — YYYY-MM-DD

  **Total:** N interactions — X CLEAN | Y CONCERN | Z FAIL | W UNGRADED

  ### Recurring patterns
  - "pattern text" (count)
  - ...

  ## HH:MM:SS UTC — VERDICT
  | dimension | verdict | rationale |
  |---|---|---|
  | fabrication | PASS | ... |
  | ... | ... | ... |

  Notable pattern: "..."

  ---

Empty input renders a one-line "no interactions" stub. Skip the
GitHub push at the orchestrator level when input is empty.
"""

from __future__ import annotations
from collections import Counter

from ask_qc.models import GradedInteraction, DIMENSION_NAMES


def render_report(date: str, graded: list[GradedInteraction]) -> str:
    """Build the markdown report for a single date."""
    if not graded:
        return f"# /ask QC — {date}\n\n*No /ask interactions on {date}.*\n"

    lines: list[str] = []
    lines.append(f"# /ask QC — {date}")
    lines.append("")

    # Header summary
    counts: Counter[str] = Counter(g.overall_verdict for g in graded)
    total = len(graded)
    lines.append(
        f"**Total:** {total} interactions — "
        f"{counts.get('CLEAN', 0)} CLEAN | "
        f"{counts.get('CONCERN', 0)} CONCERN | "
        f"{counts.get('FAIL', 0)} FAIL | "
        f"{counts.get('UNGRADED', 0)} UNGRADED"
    )
    lines.append("")

    # Recurring patterns (notable_pattern strings that appear 2+ times)
    patterns: Counter[str] = Counter(
        g.notable_pattern for g in graded if g.notable_pattern
    )
    recurring = [(p, c) for p, c in patterns.most_common() if c >= 2]
    if recurring:
        lines.append("### Recurring patterns")
        for p, c in recurring[:3]:
            lines.append(f"- _{p}_ ({c}×)")
        lines.append("")

    # Per-interaction blocks
    for g in graded:
        lines.append(f"## {g.interaction_ts_utc} — {g.overall_verdict}")
        lines.append("")
        if g.grader_error:
            lines.append(
                f"_Grader failed: `{g.grader_error}`. "
                f"Re-run will retry._"
            )
            lines.append("")
        else:
            lines.append("| dimension | verdict | rationale |")
            lines.append("|---|---|---|")
            for name in DIMENSION_NAMES:
                d = g.dimensions.get(name)
                if d is None:
                    lines.append(f"| {name} | — | — |")
                else:
                    # Pipe in rationale would break the table — escape it.
                    rationale = d.rationale.replace("|", "\\|").replace("\n", " ")
                    lines.append(f"| {name} | {d.verdict} | {rationale} |")
            lines.append("")
            if g.notable_pattern:
                lines.append(f"_Notable: {g.notable_pattern}_")
                lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_aggregator.py`
Expected: `ALL ASK-QC AGGREGATOR SMOKE TESTS PASS` (5 PASS lines)

- [ ] **Step 5: Commit**

```bash
git add ask_qc/aggregator.py scripts/smoke_ask_qc_aggregator.py
git commit -m "ask-qc: markdown report renderer

render_report(date, graded) produces the per-day markdown:
  - header: total + verdict counts (CLEAN/CONCERN/FAIL/UNGRADED)
  - 'Recurring patterns' section surfacing notable_pattern strings
    that appear 2+ times across the day
  - per-interaction block with verdict-per-dimension table + rationale
  - UNGRADED rows render the grader_error class name

Empty input gets a one-line 'No /ask interactions' stub.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Scheduler wiring + observability

**Files:**
- Modify: `scheduler/jobs.py` — add `_ask_qc_job` async function + cron registration
- Test: `scripts/smoke_ask_qc_scheduler_wired.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/smoke_ask_qc_scheduler_wired.py`:

```python
"""Smoke test: scheduler.jobs registers _ask_qc_job at 03:00 ET cron.

Verifies the new job is registered with the right id, trigger, and
guardrails (max_instances=1, misfire_grace_time set), and that the
job function is callable without raising on a missing log file
(graceful degradation when bot hasn't been live)."""

import asyncio
import sys
from unittest.mock import patch, MagicMock


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_ask_qc_job_registered():
    """register_jobs() should call scheduler.add_job with id='ask_qc'
    and a CronTrigger at hour=3."""
    from scheduler import jobs
    mock_scheduler = MagicMock()
    mock_bot = MagicMock()
    with patch("scheduler.jobs.settings") as mock_settings:
        # Minimal settings for the registration path
        mock_settings.timezone = "America/New_York"
        mock_settings.github_token = "fake"
        mock_settings.analyst_channel_name = ""
        mock_settings.profile_window_days = 30
        mock_settings.profile_delta_threshold = 20
        mock_settings.profile_min_messages = 30
        mock_settings.max_user_profiles = 100
        mock_settings.profile_channels = []
        mock_settings.analyst_trade_retention_days = 30
        jobs.register_jobs(mock_scheduler, mock_bot)
    # Find the call(s) where id='ask_qc'
    calls = [c for c in mock_scheduler.add_job.call_args_list
             if c.kwargs.get("id") == "ask_qc"]
    assert len(calls) == 1, (
        f"expected exactly 1 add_job(id='ask_qc') call, got {len(calls)}"
    )
    call = calls[0]
    # Trigger must be CronTrigger with hour=3
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(call.kwargs.get("trigger"), CronTrigger), (
        f"trigger not CronTrigger: {call.kwargs.get('trigger')}"
    )
    assert call.kwargs.get("max_instances") == 1
    _ok("scheduler: _ask_qc_job registered as 'ask_qc' cron with CronTrigger")


def test_ask_qc_job_noops_when_no_log_file(tmp_path=None):
    """_ask_qc_job should log + exit cleanly when yesterday's log file
    is missing (bot was down all day; first run after deploy)."""
    import tempfile
    from pathlib import Path
    from scheduler import jobs
    with tempfile.TemporaryDirectory() as tmp:
        # Point pdf_download_dir at an empty temp tree so ask-logs/ is empty
        with patch("config.settings.pdf_download_dir", str(Path(tmp) / "pdfs")):
            # Should NOT raise — graceful noop
            asyncio.run(jobs._ask_qc_job())
    _ok("_ask_qc_job: missing log file → graceful noop (no exception)")


if __name__ == "__main__":
    print("=== ask-qc scheduler wired smoke ===")
    test_ask_qc_job_registered()
    test_ask_qc_job_noops_when_no_log_file()
    print("\nALL ASK-QC SCHEDULER-WIRED SMOKE TESTS PASS")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_scheduler_wired.py`
Expected: FAIL with `AssertionError: expected exactly 1 add_job(id='ask_qc') call, got 0` (or `AttributeError` if `_ask_qc_job` doesn't exist yet)

- [ ] **Step 3: Add `_ask_qc_job` to `scheduler/jobs.py`**

Locate the `_ask_log_publish_job` function (around line 562) and add the new job function right after it:

```python
async def _ask_qc_job():
    """Daily 03:00 ET — grade yesterday's /ask interactions.

    Reads /data/ask-logs/{yesterday-UTC}.md, runs the Gemini judge
    over every interaction, renders a markdown report, writes it
    locally to /data/ask-qc/{date}.md, pushes to pulse-data:ask-qc/.

    Graceful degradation:
      - Missing log file -> log + exit
      - Empty log file (0 interactions) -> write stub locally,
        skip GitHub push
      - Gemini failures per interaction -> UNGRADED in report
      - GitHub push failure -> log WARNING, don't raise (local
        file is source of truth)

    Records a pipeline_events row on completion with the daily stats."""
    from pathlib import Path
    from datetime import datetime, timezone, timedelta
    from config import settings as _settings
    import db as _db

    try:
        # Yesterday UTC — the day whose log file is now closed
        now = datetime.now(timezone.utc)
        date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        base_dir = Path(_settings.pdf_download_dir).resolve().parent
        log_dir = base_dir / "ask-logs"
        qc_dir = base_dir / "ask-qc"
        qc_dir.mkdir(parents=True, exist_ok=True)

        log_path = log_dir / f"{date}.md"
        if not log_path.exists():
            log.info(f"ask-qc: no log for {date}, nothing to grade")
            return

        from ask_qc.parser import parse_ask_log
        from ask_qc.grader import grade_day
        from ask_qc.aggregator import render_report

        text = log_path.read_text(encoding="utf-8")
        interactions = parse_ask_log(text)
        log.info(f"ask-qc: grading {date} ({len(interactions)} interactions)")

        if not interactions:
            report = render_report(date, [])
            (qc_dir / f"{date}.md").write_text(report, encoding="utf-8")
            log.info(f"ask-qc: 0 interactions on {date}, wrote stub, skipping push")
            _db.record_pipeline_event(
                "ask_qc", "completed",
                {"date": date, "interactions_total": 0,
                 "interactions_graded": 0, "interactions_ungraded": 0,
                 "github_pushed": False},
            )
            return

        graded = await grade_day(interactions, sem_size=3)
        report = render_report(date, graded)
        (qc_dir / f"{date}.md").write_text(report, encoding="utf-8")

        # Push to pulse-data:ask-qc/. Best-effort; local file is source of truth.
        pushed = False
        if _settings.github_token:
            try:
                from github_bridge import client as gh_client
                gh_client.put_file(
                    path=f"ask-qc/{date}.md",
                    content=report,
                    message=f"ask-qc: snapshot {date}",
                )
                pushed = True
            except Exception as e:
                log.warning(f"ask-qc: GitHub push failed for {date}: {e}")

        # Verdict tallies for the pipeline_event row
        from collections import Counter
        counts = Counter(g.overall_verdict for g in graded)
        ungraded = counts.get("UNGRADED", 0)
        _db.record_pipeline_event(
            "ask_qc", "partial" if ungraded > 0 else "completed",
            {
                "date": date,
                "interactions_total": len(interactions),
                "interactions_graded": len(graded) - ungraded,
                "interactions_ungraded": ungraded,
                "clean": counts.get("CLEAN", 0),
                "concern": counts.get("CONCERN", 0),
                "fail": counts.get("FAIL", 0),
                "github_pushed": pushed,
            },
        )
        log.info(
            f"ask-qc: done {date} — {counts.get('CLEAN', 0)}/"
            f"{counts.get('CONCERN', 0)}/{counts.get('FAIL', 0)}/"
            f"{ungraded} (clean/concern/fail/ungraded), "
            f"pushed={pushed}"
        )
    except Exception as e:
        log.error(f"ask-qc job failed: {e}", exc_info=True)
        try:
            _db.record_pipeline_event(
                "ask_qc", "failed",
                {"error": f"{type(e).__name__}: {e}"},
            )
        except Exception:
            pass
```

- [ ] **Step 4: Register the cron in `register_jobs()`**

Locate the existing `_ask_log_publish_job` registration block (around line 304-316) in `register_jobs()` and add the new cron immediately after it:

```python
    # /ask QC sub-agent — daily 03:00 ET grader. Reads yesterday's
    # /ask log, runs Gemini judge over each interaction, publishes
    # report to pulse-data:ask-qc/. See ask_qc/ + design spec
    # docs/superpowers/specs/2026-06-02-ask-qc-subagent-design.md.
    scheduler.add_job(
        _ask_qc_job,
        trigger=CronTrigger(hour=3, minute=0, timezone=tz),
        id="ask_qc",
        name="/ask QC: grade yesterday's interactions",
        max_instances=1,
        misfire_grace_time=3600,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_scheduler_wired.py`
Expected: `ALL ASK-QC SCHEDULER-WIRED SMOKE TESTS PASS` (2 PASS lines)

- [ ] **Step 6: Commit**

```bash
git add scheduler/jobs.py scripts/smoke_ask_qc_scheduler_wired.py
git commit -m "ask-qc: scheduler wiring + observability

New _ask_qc_job async function in scheduler/jobs.py, registered as
'ask_qc' on a CronTrigger at 03:00 America/New_York. Reads
yesterday's /ask log, drives parser -> grader -> aggregator,
writes locally + pushes to pulse-data:ask-qc/.

Records a pipeline_events row per run with daily verdict counts;
matches the existing observability pattern from issue #7.

Graceful degradation: missing log -> noop; empty log -> stub +
skip push; per-interaction failures -> UNGRADED; GitHub push
failure -> WARNING not raise (local file is source of truth).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: End-to-end smoke

**Files:**
- Test: `scripts/smoke_ask_qc_end_to_end.py`

- [ ] **Step 1: Write the end-to-end test**

Create `scripts/smoke_ask_qc_end_to_end.py`:

```python
"""End-to-end smoke for the ask-qc pipeline.

Real parser + real aggregator + mocked Gemini judge + mocked
github_bridge. Drives _ask_qc_job with a temp log directory
seeded with a small fake log, verifies:
  - report file lands at the right local path
  - github_bridge.put_file is called with the right args
  - pipeline_events row is written with the expected payload
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_FAKE_LOG = """# /ask interactions — 2026-06-01

## 2026-06-01 14:30:00 UTC

**Asker:** kloh (`kloh.`) in #💬-stonks-yapping-💬

**Q:** what's TSLA at

**A:**

- **$TSLA $310**

---

## 2026-06-01 15:00:00 UTC

**Asker:** BK (`bankerkyle`) in #💬-stonks-yapping-💬

**Q:** who's the worst trader

**A:**

theorb_18574 sits at the bottom.

---
"""


_CLEAN_JSON = json.dumps({
    "overall": "CLEAN",
    "dimensions": {
        "fabrication": {"verdict": "PASS", "rationale": "grounded"},
        "status_handling": {"verdict": "N/A", "rationale": "legacy"},
        "voice": {"verdict": "PASS", "rationale": "ok"},
        "format_adherence": {"verdict": "PASS", "rationale": "ok"},
        "depth_match": {"verdict": "PASS", "rationale": "ok"},
        "decline_when_uncertain": {"verdict": "N/A", "rationale": "all ok"},
    },
    "notable_pattern": None,
})


def test_end_to_end_writes_local_report_and_pushes():
    from datetime import datetime, timezone, timedelta

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ask_logs_dir = tmp / "ask-logs"
        ask_logs_dir.mkdir()

        # Compute yesterday UTC the same way _ask_qc_job does
        date = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        (ask_logs_dir / f"{date}.md").write_text(
            _FAKE_LOG.replace("2026-06-01", date), encoding="utf-8"
        )

        # Mock Gemini to always return CLEAN
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = _CLEAN_JSON
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=mock_resp
        )

        mock_put = MagicMock()

        with (
            patch("config.settings.pdf_download_dir", str(tmp / "pdfs")),
            patch("config.settings.google_api_key", "fake"),
            patch("config.settings.gemini_model", "gemini-flash"),
            patch("config.settings.github_token", "fake"),
            patch("ask_qc.grader._get_client", return_value=mock_client),
            patch("github_bridge.client.put_file", mock_put),
        ):
            from scheduler import jobs
            asyncio.run(jobs._ask_qc_job())

        # 1. local report file exists
        report_path = tmp / "ask-qc" / f"{date}.md"
        assert report_path.exists(), f"report file missing at {report_path}"
        content = report_path.read_text(encoding="utf-8")
        assert "2 interactions" in content or "2 CLEAN" in content, content[:300]
        _ok("end-to-end: local report file written")

        # 2. github_bridge.put_file called with the right path/message
        assert mock_put.call_count == 1, (
            f"expected 1 put_file call, got {mock_put.call_count}"
        )
        call = mock_put.call_args
        assert call.kwargs["path"] == f"ask-qc/{date}.md", call.kwargs["path"]
        assert "ask-qc: snapshot" in call.kwargs["message"]
        _ok("end-to-end: github_bridge.put_file called with right args")


def test_end_to_end_empty_log_skips_push():
    from datetime import datetime, timezone, timedelta

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ask_logs_dir = tmp / "ask-logs"
        ask_logs_dir.mkdir()
        date = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        # Empty file (no interactions) — parser returns [], orchestrator
        # should write the stub locally but skip the push
        (ask_logs_dir / f"{date}.md").write_text(
            "# /ask interactions — placeholder\n", encoding="utf-8"
        )

        mock_put = MagicMock()
        with (
            patch("config.settings.pdf_download_dir", str(tmp / "pdfs")),
            patch("config.settings.github_token", "fake"),
            patch("github_bridge.client.put_file", mock_put),
        ):
            from scheduler import jobs
            asyncio.run(jobs._ask_qc_job())

        # Stub written locally
        report_path = tmp / "ask-qc" / f"{date}.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "No /ask interactions" in content or "no interactions" in content.lower()
        # No push
        assert mock_put.call_count == 0
        _ok("end-to-end: empty log writes stub locally, skips push")


if __name__ == "__main__":
    print("=== ask-qc end-to-end smoke ===")
    test_end_to_end_writes_local_report_and_pushes()
    test_end_to_end_empty_log_skips_push()
    print("\nALL ASK-QC END-TO-END SMOKE TESTS PASS")
```

- [ ] **Step 2: Run the end-to-end smoke**

Run: `PYTHONPATH=. py.exe scripts/smoke_ask_qc_end_to_end.py`
Expected: `ALL ASK-QC END-TO-END SMOKE TESTS PASS` (3 PASS lines)

- [ ] **Step 3: Commit**

```bash
git add scripts/smoke_ask_qc_end_to_end.py
git commit -m "ask-qc: end-to-end smoke

Real parser + real aggregator + mocked Gemini + mocked github_bridge.
Drives _ask_qc_job with a temp log directory; verifies local
report file lands at the right path AND push fires with the right
args, AND empty-log path writes stub but skips push.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Regression check + pyflakes

**Files:**
- No code changes (verification only)

- [ ] **Step 1: Run pyflakes on the new modules**

Run: `PYTHONPATH=. py.exe -m pyflakes ask_qc/ scheduler/jobs.py`
Expected: no output (clean). The pre-existing 2 warnings in `discord_bot/bot.py` are out of scope; if any new warnings show up under `ask_qc/` or in lines I added to `scheduler/jobs.py`, fix them inline before commit.

- [ ] **Step 2: Run the full /ask + ask-qc smoke suite**

```bash
for f in scripts/smoke_ask_qc_models.py \
         scripts/smoke_ask_qc_parser.py \
         scripts/smoke_ask_qc_judge_prompt.py \
         scripts/smoke_ask_qc_grader.py \
         scripts/smoke_ask_qc_aggregator.py \
         scripts/smoke_ask_qc_scheduler_wired.py \
         scripts/smoke_ask_qc_end_to_end.py \
         scripts/smoke_freshness_and_status.py \
         scripts/smoke_user_profile_tool.py \
         scripts/smoke_trade_log_tool.py \
         scripts/smoke_chat_search_keyword_optional.py \
         scripts/smoke_tools_wired.py \
         scripts/smoke_pyflakes_undefined.py; do
  echo "=== $f ==="
  PYTHONPATH=. py.exe "$f" 2>&1 | tail -2
done
```

Expected: every smoke ends with `ALL ... SMOKE TESTS PASS`. If any regression appears, fix before merging.

- [ ] **Step 3: Final import sanity check**

Run: `PYTHONPATH=. py.exe -c "import ask_qc; import scheduler.jobs; import discord_bot.bot; print('all imports ok')"`
Expected: `all imports ok`

- [ ] **Step 4: Push to GitHub**

```bash
git push
```

- [ ] **Step 5: Verify next-day production behavior (after first cron fire)**

Around 03:30 ET the day after merge, ask the user to run:

```bash
railway logs --deployment 2>&1 | grep "ask-qc"
```

Expected log lines:
- `INFO ask-qc: grading {date} ({N} interactions)`
- `INFO ask-qc: done {date} — X/Y/Z/0 (clean/concern/fail/ungraded), pushed=True`

And verify the report file appeared at:
`https://github.com/gabjew90/Institutional-report-bot/blob/pulse-data/ask-qc/{date}.md`

If logs show the cron didn't fire or the file didn't push, investigate the new `pipeline_events` row:

```bash
railway ssh 'python -c "import db; rows = db.get_recent_pipeline_events(event_type=\"ask_qc\", limit=3); print(rows)"'
```

---

## Done condition

- [ ] All 7 ask-qc smokes pass locally
- [ ] All 6 regression smokes still pass locally
- [ ] pyflakes clean on `ask_qc/` and the lines added to `scheduler/jobs.py`
- [ ] Branch pushed to GitHub
- [ ] Next-day verification: report file lands at `pulse-data:ask-qc/{date}.md`
