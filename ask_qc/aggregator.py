"""Render a list[GradedInteraction] into a daily markdown QC report.

Layout:
  # /ask QC - YYYY-MM-DD

  **Total:** N interactions - X CLEAN | Y CONCERN | Z FAIL | W UNGRADED

  ### Recurring patterns
  - "pattern text" (count)
  - ...

  ## HH:MM:SS UTC - VERDICT
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
        return f"# /ask QC - {date}\n\n*No /ask interactions on {date}.*\n"

    lines: list[str] = []
    lines.append(f"# /ask QC - {date}")
    lines.append("")

    # Header summary
    counts: Counter[str] = Counter(g.overall_verdict for g in graded)
    total = len(graded)
    # INFRA is reported against the total but kept out of the graded
    # tally: those interactions never produced a bot answer, so folding
    # them into CLEAN or FAIL makes both numbers mean less.
    n_infra = counts.get("INFRA", 0)
    graded_total = total - n_infra
    lines.append(
        f"**Total:** {total} interactions - "
        f"{counts.get('CLEAN', 0)} CLEAN | "
        f"{counts.get('CONCERN', 0)} CONCERN | "
        f"{counts.get('FAIL', 0)} FAIL | "
        f"{counts.get('UNGRADED', 0)} UNGRADED"
    )
    if n_infra:
        pct = round(100 * n_infra / total)
        lines.append("")
        lines.append(
            f"**Not gradable:** {n_infra} of {total} ({pct}%) shipped a "
            f"system wrapper instead of an answer - "
            + ", ".join(
                f"{r} x{c}" for r, c in Counter(
                    g.infra_reason for g in graded if g.infra_reason
                ).most_common()
            )
            + f". The {graded_total} graded above exclude them."
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
            lines.append(f"- _{p}_ ({c}x)")
        lines.append("")

    # Per-interaction blocks
    for g in graded:
        lines.append(f"## {g.interaction_ts_utc} - {g.overall_verdict}")
        lines.append("")
        if g.infra_reason:
            lines.append(
                f"_No bot answer to grade - `{g.infra_reason}`. The user "
                f"got a system wrapper. Not a voice, format or fabrication "
                f"result; fix the pipeline, not the prompt._"
            )
            lines.append("")
        elif g.grader_error:
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
                    lines.append(f"| {name} | - | - |")
                else:
                    # Pipe in rationale would break the table - escape it.
                    rationale = d.rationale.replace("|", "\\|").replace("\n", " ")
                    lines.append(f"| {name} | {d.verdict} | {rationale} |")
            lines.append("")
            if g.notable_pattern:
                lines.append(f"_Notable: {g.notable_pattern}_")
                lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)
