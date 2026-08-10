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


# Infra wrappers the bot ships INSTEAD of an answer. The literals live in
# discord_bot/bot.py (search "→ Gemini bounced this one"); they are
# duplicated here rather than imported because ask_qc is deliberately
# decoupled from the bot module. smoke_ask_qc_infra_bucket asserts the
# bot's live strings are still covered, so drift fails a test instead of
# silently sending canned strings back to the judge.
#
# `filter-retry: failed` in the route metadata is the PRIMARY signal —
# recorded system state beats matching prose. The prefixes below cover the
# two paths that ship a wrapper without touching filter_retry (token
# budget, MAX_TOKENS) and legacy entries with no Route line.
_INFRA_ANSWER_SIGNATURES = (
    ("Gemini bounced this one", "filter-block"),
    ("Daily token budget reached", "token-budget"),
    ("Thought myself in circles and ran out of room", "max-tokens"),
)


def infra_failure_reason(interaction: AskInteraction) -> Optional[str]:
    """Return a reason string when this interaction has no bot answer to
    grade, else None.

    An infra failure is not a bad answer — it is the absence of one. The
    judge cannot tell the difference, and over 2026-08-07..09 it graded
    the identical filter-block wrapper CLEAN twice and FAIL twice, once
    calling the accurate block report a fabrication. This decides it from
    system state instead."""
    route = (interaction.route_meta or "").lower()
    if "filter-retry: failed" in route:
        return "filter-block"
    answer = interaction.answer or ""
    for needle, reason in _INFRA_ANSWER_SIGNATURES:
        if needle in answer:
            return reason
    return None


class JudgeResponseBlocked(Exception):
    """Gemini returned no text for the judge call — safety filter block
    or empty candidate. Named so the QC report's `grader_error` reads
    as a diagnosis instead of the bare TypeError that `json.loads(None)`
    used to raise (2026-07-22 19:39 UNGRADED)."""


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
        # Short-circuit BEFORE the semaphore: an infra failure costs no
        # Gemini call and shouldn't occupy a concurrency slot.
        reason = infra_failure_reason(ix)
        if reason:
            log.info(
                f"ask-qc INFRA {ix.ts_utc}: {reason} — no bot answer to "
                f"grade, judge skipped"
            )
            return GradedInteraction(
                interaction_ts_utc=ix.ts_utc,
                dimensions={},
                notable_pattern=None,
                infra_reason=reason,
            )
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
    raw = response.text
    if raw is None:
        feedback = getattr(response, "prompt_feedback", None)
        raise JudgeResponseBlocked(
            f"judge response had no text (prompt_feedback={feedback!r})"
        )
    return _parse_judge_response(interaction.ts_utc, raw)


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
