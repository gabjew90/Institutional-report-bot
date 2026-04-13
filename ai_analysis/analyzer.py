"""Gemini API analysis orchestrator.

Implements the tiered analysis strategy:
  Tier 1: Triage (cheap text-only classification)
  Tier 2: Deep analysis (text-only; sends full document to Gemini)
  Tier 3: Synthesis (cross-PDF report generation, handled by report/synthesizer.py)

Uses Google Gemini for all tiers.
"""

import asyncio
import json
import logging
import time

from google import genai
from google.genai import types

from ai_analysis.models import (
    TriageResult, PdfAnalysis, MarketMover, SectorView,
    MacroIndicator, TradeIdea,
)
from ai_analysis.prompts import (
    TRIAGE_SYSTEM_PROMPT, TRIAGE_USER_PROMPT,
    ANALYSIS_SYSTEM_PROMPT, ANALYSIS_USER_PROMPT_MULTIMODAL,
    ANALYSIS_USER_PROMPT_TEXT_ONLY,
)
from ai_analysis.rate_limiter import RateLimiter
from pdf_processing.models import PdfExtraction
from config import settings

log = logging.getLogger(__name__)

_client: genai.Client | None = None
_rate_limiter: RateLimiter | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=settings.google_api_key)
    return _client


def _get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    if _rate_limiter is None:
        _rate_limiter = RateLimiter(
            max_concurrent=settings.gemini_max_concurrent,
            rpm_limit=50,
        )
    return _rate_limiter


def _parse_json_response(text: str) -> dict:
    """Extract JSON from model response, handling markdown code blocks and extra text."""
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to find JSON object in the text
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise json.JSONDecodeError(f"Could not extract valid JSON from response: {text[:500]}", text, 0)


def _safe_dataclass(cls, data: dict):
    """Build a dataclass from dict, tolerating extra/missing keys.

    Gemini occasionally returns unexpected keys (e.g., extra 'confidence' field)
    or omits optional ones. Spread-style `cls(**data)` blows up on either; this
    helper filters to known fields and lets defaults cover missing ones.
    """
    try:
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed}
        return cls(**filtered)
    except Exception:
        return None


def _is_tier1_source(source: str, folder_path: str) -> bool:
    """Check if PDF is from a tier 1 bank (GS, JPM, BofA, MS)."""
    tier1_keywords = ["goldman", "gs ", "jpmorgan", "jpm", "bank of america", "bofa",
                      "morgan stanley"]
    source_lower = source.lower()
    folder_lower = folder_path.lower()
    return any(kw in source_lower or kw in folder_lower for kw in tier1_keywords)


def _apply_priority_rules(gemini_priority: str, source: str, report_type: str, folder_path: str) -> str:
    """Override Gemini's priority based on source and topic rules.

    Tier 1 sources (GS, JPM, BofA, MS): floor is MEDIUM — never dropped.
    Gemini decides HIGH vs MEDIUM based on content (charts, macro, positioning).
    High-priority topics boost any source to HIGH.
    """
    tier1 = _is_tier1_source(source, folder_path)

    # Tier 1 sources: never LOW, floor is MEDIUM
    if tier1 and gemini_priority == "low":
        return "medium"

    # Non-tier-1 LOW: respect Gemini's call, skip it
    if gemini_priority == "low":
        return "low"

    # High-priority topics boost to HIGH regardless of source
    high_topics = {"macro", "crypto", "vol_commentary", "morning_briefing",
                   "sales_trading", "strategy", "derivatives"}
    if report_type in high_topics:
        return "high"

    # Otherwise trust Gemini's judgment
    return gemini_priority


async def triage_pdf(file_name: str, text_preview: str, folder_path: str = "") -> TriageResult:
    """Tier 1: Quick classification using Gemini (text-only).

    Returns priority (high/medium/low), report_type, key tickers, and summary.
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    preview = text_preview[:8000]

    user_prompt = TRIAGE_USER_PROMPT.format(
        file_name=file_name,
        folder_path=folder_path,
        text_preview=preview,
    )

    async with limiter:
        response = await client.aio.models.generate_content(
            model=settings.gemini_triage_model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=TRIAGE_SYSTEM_PROMPT,
                max_output_tokens=1024,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

    result_text = response.text
    data = _parse_json_response(result_text)

    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0

    gemini_priority = data.get("priority", "medium")
    report_type = data.get("report_type", "other")
    source = data.get("source", "")

    # Deterministic priority override based on source and topic
    final_priority = _apply_priority_rules(gemini_priority, source, report_type, folder_path)

    return TriageResult(
        priority=final_priority,
        report_type=report_type,
        key_tickers=data.get("key_tickers", []),
        summary=data.get("summary", ""),
        source=source,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def analyze_pdf_deep(
    pdf_file_id: int,
    file_name: str,
    extraction: PdfExtraction,
    priority: str,
) -> PdfAnalysis:
    """Tier 2: Deep analysis using Gemini — text-only.

    Sends the full document to Gemini. No truncation, no image rendering.
    Gemini's 1M-token context handles even 90-page UBS Contextual Diaries.
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    text_content = extraction.full_text

    user_text = ANALYSIS_USER_PROMPT_TEXT_ONLY.format(
        file_name=file_name,
        total_pages=extraction.total_pages,
        text_content=text_content,
    )
    content_parts = [types.Part.from_text(text=user_text)]

    start_time = time.time()

    async with limiter:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=content_parts,
            config=types.GenerateContentConfig(
                system_instruction=ANALYSIS_SYSTEM_PROMPT,
                max_output_tokens=settings.gemini_max_tokens,
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )

    duration = time.time() - start_time
    result_text = response.text

    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0

    try:
        data = _parse_json_response(result_text)
        # Gemini sometimes returns a list instead of a dict — grab first element
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, Exception) as e:
        log.error(f"Failed to parse analysis JSON for {file_name}: {e} — {result_text[:200]}")
        data = {}

    analysis = PdfAnalysis(
        pdf_file_id=pdf_file_id,
        file_name=file_name,
        source=data.get("source", "Unknown"),
        title=data.get("title", file_name),
        report_type=data.get("report_type", "other"),
        priority=priority,
        key_insights=data.get("key_insights", []),
        market_movers=[
            mover for mover in (
                _safe_dataclass(MarketMover, mm) for mm in data.get("market_movers", [])
                if isinstance(mm, dict)
            ) if mover is not None
        ],
        sector_views=[
            sv for sv in (
                _safe_dataclass(SectorView, sv) for sv in data.get("sector_views", [])
                if isinstance(sv, dict)
            ) if sv is not None
        ],
        earnings_insights=data.get("earnings_insights", []),
        macro_indicators=[
            mi for mi in (
                _safe_dataclass(MacroIndicator, mi) for mi in data.get("macro_indicators", [])
                if isinstance(mi, dict)
            ) if mi is not None
        ],
        crypto_views=data.get("crypto_views", []),
        trade_ideas=[
            ti for ti in (
                _safe_dataclass(TradeIdea, ti) for ti in data.get("trade_ideas", [])
                if isinstance(ti, dict)
            ) if ti is not None
        ],
        risk_factors=data.get("risk_factors", []),
        charts_described=data.get("charts_described", []),
        vol_and_positioning=data.get("vol_and_positioning", []),
        geopolitical=data.get("geopolitical", []),
        cross_bank_references=data.get("cross_bank_references", []),
        pages_analyzed=0,  # text-only; no image rendering
        total_pages=extraction.total_pages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    log.info(
        f"Analyzed {file_name}: priority={priority}, "
        f"text-only, "
        f"{input_tokens} in / {output_tokens} out, "
        f"{duration:.1f}s"
    )
    return analysis


async def analyze_batch(
    items: list[tuple[int, str, PdfExtraction, str]],
) -> list[PdfAnalysis]:
    """Process multiple PDFs concurrently.

    Items: list of (pdf_file_id, file_name, extraction, priority)
    Rate limiter handles concurrency internally.
    """
    tasks = [
        analyze_pdf_deep(pdf_id, fname, extraction, priority)
        for pdf_id, fname, extraction, priority in items
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    analyses = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            pdf_id, fname, _, _ = items[i]
            log.error(f"Analysis failed for {fname} (id={pdf_id}): {result}")
        else:
            analyses.append(result)

    return analyses
