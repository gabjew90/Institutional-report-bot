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
    rather than raising - partial-day reports beat no report."""
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
    """Single Gemini call -> parsed GradedInteraction. Raises on
    Gemini error or malformed JSON - the retry wrapper handles it."""
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
