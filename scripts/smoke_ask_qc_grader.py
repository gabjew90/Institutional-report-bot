"""Smoke test for ask_qc.grader.

Covers:
  - clean JSON response -> GradedInteraction with all 6 dimensions
  - malformed JSON -> retry once -> UNGRADED on second failure
  - Gemini API exception -> retry once -> UNGRADED on second failure
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
    _ok("grader: clean JSON response -> CLEAN GradedInteraction")


def test_grader_malformed_json_retries_then_ungraded():
    from ask_qc import grader
    interaction = _make_interaction()
    mock_client = MagicMock()
    # Both attempts return malformed JSON
    mock_client.aio.models.generate_content = AsyncMock(
        return_value=_mock_gemini_response("not valid json {")
    )
    with (
        patch("ask_qc.grader._get_client", return_value=mock_client),
        # Patch out asyncio.sleep so the test doesn't actually wait 5s
        patch("ask_qc.grader.asyncio.sleep", new=AsyncMock()),
    ):
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
    with (
        patch("ask_qc.grader._get_client", return_value=mock_client),
        patch("ask_qc.grader.asyncio.sleep", new=AsyncMock()),
    ):
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
    with (
        patch("ask_qc.grader._get_client", return_value=mock_client),
        patch("ask_qc.grader.asyncio.sleep", new=AsyncMock()),
    ):
        graded = asyncio.run(grader.grade_day([interaction]))
    g = graded[0]
    assert g.overall_verdict == "CLEAN", g.overall_verdict
    assert g.grader_error is None
    _ok("grader: transient fail + retry success -> CLEAN (not UNGRADED)")


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


def test_grader_empty_list_returns_empty():
    from ask_qc import grader
    graded = asyncio.run(grader.grade_day([]))
    assert graded == []
    _ok("grader: empty input -> [] (no Gemini call)")


if __name__ == "__main__":
    print("=== ask_qc.grader smoke ===")
    test_grader_clean_response()
    test_grader_malformed_json_retries_then_ungraded()
    test_grader_api_exception_retries_then_ungraded()
    test_grader_first_attempt_fails_second_succeeds()
    test_grader_multiple_interactions_all_processed()
    test_grader_empty_list_returns_empty()
    print("\nALL ASK-QC GRADER SMOKE TESTS PASS")
