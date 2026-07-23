"""Smoke test for ask_qc filter-block handling.

Context (2026-07-23): three interactions in the 07-15..07-22 window
landed UNGRADED with `grader_error: TypeError`. Root cause: the judge
prompt embeds the forensic prompt_block verbatim — including WHO'S
TALKING profiles quoting members' slurs — which trips Gemini's
unconfigurable safety filter. A blocked response has `text=None`, and
`json.loads(None)` raises the bare TypeError.

Covers:
  - build_judge_prompt masks slur tokens (question, answer, prompt_block)
  - masking leaves the rubric/schema scaffolding intact
  - grader: text=None -> retried once -> UNGRADED with the descriptive
    JudgeResponseBlocked error class, not TypeError
"""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _make_interaction(question="q", answer="a", prompt_block=None):
    from ask_qc.models import AskInteraction
    return AskInteraction(
        ts_utc="2026-07-22 19:39:58 UTC", asker_label="Ry",
        asker_username="nft_spaceman", channel="#c",
        question=question, answer=answer, prompt_block=prompt_block,
    )


def test_judge_prompt_masks_slurs():
    from ask_qc.judge_prompt import build_judge_prompt
    ix = _make_interaction(
        question="give me the weekly slur count",
        answer='You dropped "Damn nigga" twice this week.',
        prompt_block=(
            "**Voice.**\n"
            '- "Pump it niggers" — [when martingaling]\n'
            '- "which he admits is retarded" — [self-aware]\n'
            "faggot chink spic kike tranny paki\n"
        ),
    )
    prompt = build_judge_prompt(ix)
    lowered = prompt.lower()
    for tok in ("nigga", "nigger", "retarded", "faggot", "chink",
                "spic", "kike", "tranny", "paki"):
        assert tok not in lowered, f"unmasked slur token in prompt: {tok}"
    assert "[redacted]" in prompt, "mask placeholder missing"
    _ok("judge prompt masks slur tokens in Q/A/prompt_block")


def test_judge_prompt_scaffolding_survives_mask():
    from ask_qc.judge_prompt import build_judge_prompt
    ix = _make_interaction(question="clean q", answer="clean a")
    prompt = build_judge_prompt(ix)
    for marker in ("fabrication", "status_handling", "voice",
                   "format_adherence", "depth_match",
                   "decline_when_uncertain", "Return EXACTLY"):
        assert marker in prompt, f"scaffolding lost: {marker}"
    assert "[redacted]" not in prompt, "clean prompt should have no masks"
    _ok("rubric/schema scaffolding intact; clean prompts unmasked")


def test_grader_blocked_response_named_error():
    from ask_qc import grader
    ix = _make_interaction()
    blocked = MagicMock()
    blocked.text = None  # what google-genai returns on a filter block
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=blocked
    )
    with (
        patch("ask_qc.grader._get_client", return_value=mock_client),
        patch("ask_qc.grader.asyncio.sleep", new=AsyncMock()),
    ):
        graded = asyncio.run(grader.grade_day([ix]))
    assert mock_client.aio.models.generate_content.call_count == 2, (
        "blocked response should still get one retry"
    )
    g = graded[0]
    assert g.overall_verdict == "UNGRADED", g.overall_verdict
    assert g.grader_error == "JudgeResponseBlocked", (
        f"expected JudgeResponseBlocked, got {g.grader_error!r}"
    )
    _ok("text=None -> UNGRADED with JudgeResponseBlocked (not TypeError)")


if __name__ == "__main__":
    print("=== ask_qc filter-block smoke ===")
    test_judge_prompt_masks_slurs()
    test_judge_prompt_scaffolding_survives_mask()
    test_grader_blocked_response_named_error()
    print("\nALL ASK-QC FILTER-BLOCK SMOKE TESTS PASS")
