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
        "  PASS - every specific claim (price, rank, date, %, attributed "
        "quote) is grounded in the visible tool/search payload, or is "
        "general knowledge that doesn't change with time.\n"
        "  CONCERN - borderline grounding (approximate number without an "
        "'around'/'roughly' hedge, paraphrased rank that drifts from the "
        "verbatim rationale).\n"
        "  FAIL - a specific factual claim contradicts or extrapolates "
        "beyond the tool/search output. Includes: confident answer when "
        "`status=empty`/`error`; quoting a price that wasn't in the "
        "response; inventing a rank position.\n"
        "  N/A - pure Type 2 banter / Type 3 personality answer with no "
        "factual assertion."
    ),
    "status_handling": (
        "Did the answer correctly interpret the `status` + freshness "
        "fields on tool responses?\n"
        "  PASS - `status=empty` -> 'no data'; `status=error` -> 'lookup "
        "failed'; `status=not_found` -> 'don't see that user/rank'; "
        "`status=ok` -> uses the data.\n"
        "  CONCERN - hedges where data exists, or under-hedges a stale "
        "`updated_at` (>5d old).\n"
        "  FAIL - treats `empty` as data; fabricates on `error`; ignores "
        "`not_found` and invents the user.\n"
        "  N/A - no visible tool payload to grade against (legacy log)."
    ),
    "voice": (
        "Trader-newsletter voice - conversational, opinionated, no AI tells.\n"
        "  PASS - direct, opinionated, sharp. No 'it's worth noting', "
        "'notably', 'moreover', 'furthermore', 'in conclusion', em-dash "
        "chains, hedging cascades, or summary wrap-ups.\n"
        "  CONCERN - 1-2 AI tells slip through.\n"
        "  FAIL - paragraph-mode prose, multiple AI tells, lecture cadence."
    ),
    "format_adherence": (
        "Type 1 (factual) answers = literal arrow bullets within the "
        "depth tier's word ceiling, no closing prose paragraph.\n"
        "  PASS - arrow bullets, within word cap, no closing wrap-up.\n"
        "  CONCERN - one stray closing line ('Net-net, ...') or modest "
        "word-cap overshoot (<=15%).\n"
        "  FAIL - prose-only where arrows were required, or 'In short, ...' "
        "summary tacked on.\n"
        "  N/A - Type 2 (banter) / Type 3 (personality) answer."
    ),
    "depth_match": (
        "Answer tier matches question shape - concision is the default.\n"
        "  Tiers:\n"
        "    Quick    - <=60 words, 2-3 arrows (single-fact lookups, "
        "'what's X at')\n"
        "    Standard - <=130 words, 3-5 arrows (most trade questions)\n"
        "    Full DD  - <=250 words, 5-7 arrows ('walk me through X', "
        "'deep dive on Y')\n"
        "  PASS - tier matches question shape.\n"
        "  CONCERN - one tier off (Standard answer to a Quick question).\n"
        "  FAIL - two tiers off (Full DD to 'what's TSLA at'), or sub-2 "
        "arrows on a clear DD request."
    ),
    "decline_when_uncertain": (
        "When tools came back empty/error or search whiffed, did the "
        "answer cleanly decline instead of fabricating?\n"
        "  PASS - `status=empty`/`error` surfaced as 'no data' / 'lookup "
        "failed'; search-miss -> 'can't verify cleanly'.\n"
        "  CONCERN - slight overconfidence given the data quality.\n"
        "  FAIL - confident fabricated answer on top of failed lookup.\n"
        "  N/A - every tool returned `status=ok` AND search returned "
        "grounded results."
    ),
}


_GUARDRAILS = """\
You are grading the bot's answer, not writing your own. Do NOT penalize:
  - Brevity when the question was terse - that's the depth_match PASS condition
  - Declining to answer when tools failed - that's the goal, not a failure
  - An answer differing from what you would have written, if it's grounded + voice-correct + format-correct
  - Insults or charged language in Type 2 (banter) / Type 3 (personality) answers - the bot is allowed to be sharp
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
            "NOTE: this log entry has no <details> block - you cannot see "
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
                f"_...(prompt_block truncated for grader at "
                f"{_PROMPT_BLOCK_CAP} chars)_"
            )
        lines.append("")

    lines.append("## Response Format")
    lines.append(_RESPONSE_SCHEMA_EXAMPLE)
    return "\n".join(lines)
