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
    # Sample guardrails - the prompt should phrase these clearly
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
