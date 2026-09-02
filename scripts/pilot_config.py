#!/usr/bin/env python3
"""Single source of truth for pilot wiring — destination, model
pinning, and reader tiers.

WHY THIS FILE
=============
The owner's destination decision for the pilot tree is OUTSTANDING
(plan §0). Every piece reads its root from here, so that decision
changes ONE constant and nothing else. Same for the model strings:
the plan's freeze rule makes a mid-pilot model change a clock restart,
which is only enforceable if there is exactly one place the strings
live.

Override PILOT_ROOT with the PILOT_ROOT env var (the workflows pass
it) so a relocation needs no code edit at all.
"""
from __future__ import annotations

import hashlib
import os

# Where the pilot tree lives. Owner decision 2026-09-01 (option B):
# a dedicated ORPHAN branch, not the production pulse-data branch, so
# the pilot cannot touch production artifacts even by accident, the
# pulse archive's history stays readable, and cleanup after the
# verdict is deleting one branch.
PILOT_BRANCH = os.environ.get("PILOT_BRANCH", "pilot-data")
# Root WITHIN that branch's checkout. Env-overridable so a relocation
# still needs no code edit.
PILOT_ROOT = os.environ.get("PILOT_ROOT", "pilot")

# Subdirectories, derived. Nothing else in the codebase hardcodes
# these paths.
SOURCE_TEXT_DIR = f"{PILOT_ROOT}/source-text"
# MEDIUM documents, for the graders' source set only (2026-09-02); the
# readers never read this tree.
SOURCE_TEXT_ALL_DIR = f"{PILOT_ROOT}/source-text-all"
CARDS_DIR = f"{PILOT_ROOT}/cards"
SHADOW_DIR = f"{PILOT_ROOT}/shadow"
GRADES_DIR = f"{PILOT_ROOT}/grades"
GRADER_FIXTURES_DIR = f"{PILOT_ROOT}/grader-fixtures"
SCOREBOARD_PATH = f"{PILOT_ROOT}/scoreboard.md"

# PINNED model strings (plan §3.7). Bare "opus"/"sonnet" are aliases
# that can move server-side mid-pilot — the shape behind two retracted
# findings in this repo. A change to any of these restarts the pilot
# clock; that is the whole reason they live in one place.
MODEL_READER_TOP = "claude-opus-5"      # GS/MS/JPM/Citi/DB/BofA
MODEL_READER_REST = "claude-sonnet-5"   # everything else
MODEL_EDITOR = "claude-opus-5"
MODEL_GRADER = "claude-sonnet-5"

# Reader tiering (plan §3.2 — this plan's deviation, recorded).
# Matches the multimodal trigger's top-bank line so the two tier
# definitions in this repo cannot drift apart.
TOP_BANK_SOURCES = (
    "goldman", "gs", "morgan stanley", "ms", "jpm", "jpmorgan",
    "j.p. morgan", "citi", "citigroup", "deutsche", "db",
    "bofa", "bank of america", "merrill",
)


def reader_tier(source: str) -> tuple[str, str]:
    """(tier_name, pinned_model_string) for a document's source."""
    s = (source or "").strip().lower()
    if any(b in s for b in TOP_BANK_SOURCES):
        return "top", MODEL_READER_TOP
    return "rest", MODEL_READER_REST


def prompt_sha(text: str) -> str:
    """Short stable hash of a prompt's text, recorded in provenance.

    A pinned model string plus a prompt hash is what makes the freeze
    rule auditable after the fact rather than a promise: any day whose
    artifacts carry a different hash was graded under different
    instructions.
    """
    return hashlib.sha256(
        (text or "").encode("utf-8")).hexdigest()[:12]


def provenance(model_requested: str, model_version_returned: str | None,
               prompt_text: str) -> dict:
    """The provenance block every cards/grade file carries (§3.7).

    `model_version_returned` is recorded separately from the request
    because a pinned request string is a REQUEST, not a guarantee —
    the fixture harness learned this when a "-preview" alias moved
    under two baselines.
    """
    return {
        "model_requested": model_requested,
        "model_version_returned": model_version_returned or "unreported",
        "prompt_sha": prompt_sha(prompt_text),
    }
