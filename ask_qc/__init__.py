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
