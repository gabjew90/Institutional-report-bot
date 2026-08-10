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
  - INFRA    — the bot never produced an answer; a system wrapper shipped
               instead (filter block, token budget, MAX_TOKENS). Not
               graded, because there is no bot answer to grade.
  - UNGRADED — grader_error was set; dimensions intentionally empty
  - FAIL     — any dimension is FAIL
  - CONCERN  — no FAIL, any dimension is CONCERN
  - CLEAN    — all dimensions PASS or N/A

Why INFRA exists (2026-08-10). The filter-block wrapper is one fixed
string, and over 2026-08-07..09 the judge graded that identical string
CLEAN twice and FAIL twice. On format_adherence alone it produced both
"used the required arrow bullet format even for the error message" and
"failed to use the required arrow bullet format". One FAIL went further
and called the accurate "hard filter" report a fabrication, when the ask
log's own route metadata records the block as real.

Asking an LLM to grade voice, depth and fabrication on a canned system
string is a category error: there is no authored answer under it. The
verdict is now decided deterministically from the recorded system state,
so CLEAN/FAIL counts mean "the bot answered well/badly" and infra
failures are counted separately instead of polluting both buckets.
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
    # The `**Route:**` line verbatim, e.g.
    # "`LOCAL/BANTER` · ungrounded · filter-retry: failed · guards: —".
    # This is recorded system state, not prose, so the INFRA short-circuit
    # reads it in preference to pattern-matching the answer text. `None`
    # for legacy entries written before the Route line shipped.
    route_meta: Optional[str] = None


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
    # Set when the interaction never produced a bot answer. Short-circuits
    # the judge entirely — no Gemini call is made — and holds the reason
    # ("filter-block", "token-budget", "max-tokens") for the report.
    infra_reason: Optional[str] = None

    @property
    def overall_verdict(self) -> str:
        if self.infra_reason:
            return "INFRA"
        if self.grader_error:
            return "UNGRADED"
        verdicts = {d.verdict for d in self.dimensions.values()}
        if "FAIL" in verdicts:
            return "FAIL"
        if "CONCERN" in verdicts:
            return "CONCERN"
        return "CLEAN"
