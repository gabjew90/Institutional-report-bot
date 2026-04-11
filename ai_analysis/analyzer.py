"""Gemini API multimodal analysis orchestrator.

Implements the tiered analysis strategy:
  Tier 1: Triage (text-only, cheap classification)
  Tier 2: Deep analysis (multimodal for HIGH, text-only for MEDIUM)
  Tier 3: Synthesis (cross-PDF report generation, handled by report/synthesizer.py)

Uses Google Gemini 3.1 Lite for all tiers.
"""

import asyncio
import base64
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
    """Extract JSON from model response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


async def triage_pdf(file_name: str, text_preview: str) -> TriageResult:
    """Tier 1: Quick classification using Gemini (text-only).

    Returns priority (high/medium/low), report_type, key tickers, and summary.
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    preview = text_preview[:8000]

    user_prompt = TRIAGE_USER_PROMPT.format(
        file_name=file_name,
        text_preview=preview,
    )

    async with limiter:
        response = await client.aio.models.generate_content(
            model=settings.gemini_triage_model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=TRIAGE_SYSTEM_PROMPT,
                max_output_tokens=500,
                temperature=0.1,
            ),
        )

    result_text = response.text
    data = _parse_json_response(result_text)

    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0

    return TriageResult(
        priority=data.get("priority", "medium"),
        report_type=data.get("report_type", "other"),
        key_tickers=data.get("key_tickers", []),
        summary=data.get("summary", ""),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


async def analyze_pdf_deep(
    pdf_file_id: int,
    file_name: str,
    extraction: PdfExtraction,
    priority: str,
) -> PdfAnalysis:
    """Tier 2: Deep analysis using Gemini.

    HIGH priority: multimodal (text + page images)
    MEDIUM priority: text-only
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    use_images = priority == "high" and extraction.selected_page_images

    # Build text content
    if use_images:
        image_page_nums = {img.page_number for img in extraction.selected_page_images}
        text_parts = []
        for page in extraction.pages:
            if page.page_number in image_page_nums:
                text_parts.append(f"[Page {page.page_number + 1}]\n{page.text}")
            else:
                preview = page.text[:300].strip()
                if preview:
                    text_parts.append(f"[Page {page.page_number + 1} preview]\n{preview}...")
        text_content = "\n\n".join(text_parts)
    else:
        text_content = extraction.full_text[:30000]

    # Build content parts for Gemini
    content_parts: list = []

    if use_images:
        user_text = ANALYSIS_USER_PROMPT_MULTIMODAL.format(
            file_name=file_name,
            total_pages=extraction.total_pages,
            image_pages=", ".join(
                str(img.page_number + 1) for img in extraction.selected_page_images
            ),
            text_content=text_content,
        )
        content_parts.append(types.Part.from_text(text=user_text))

        # Add page images as inline data
        for img in extraction.selected_page_images:
            image_bytes = base64.standard_b64decode(img.image_base64)
            content_parts.append(types.Part.from_bytes(
                data=image_bytes,
                mime_type=img.media_type,
            ))
    else:
        user_text = ANALYSIS_USER_PROMPT_TEXT_ONLY.format(
            file_name=file_name,
            total_pages=extraction.total_pages,
            text_content=text_content,
        )
        content_parts.append(types.Part.from_text(text=user_text))

    start_time = time.time()

    async with limiter:
        response = await client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=content_parts,
            config=types.GenerateContentConfig(
                system_instruction=ANALYSIS_SYSTEM_PROMPT,
                max_output_tokens=settings.gemini_max_tokens,
                temperature=0.2,
            ),
        )

    duration = time.time() - start_time
    result_text = response.text

    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0

    try:
        data = _parse_json_response(result_text)
    except json.JSONDecodeError:
        log.error(f"Failed to parse analysis JSON for {file_name}: {result_text[:200]}")
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
            MarketMover(**mm) for mm in data.get("market_movers", [])
            if isinstance(mm, dict)
        ],
        sector_views=[
            SectorView(**sv) for sv in data.get("sector_views", [])
            if isinstance(sv, dict)
        ],
        earnings_insights=data.get("earnings_insights", []),
        macro_indicators=[
            MacroIndicator(**mi) for mi in data.get("macro_indicators", [])
            if isinstance(mi, dict)
        ],
        crypto_views=data.get("crypto_views", []),
        trade_ideas=[
            TradeIdea(**ti) for ti in data.get("trade_ideas", [])
            if isinstance(ti, dict)
        ],
        risk_factors=data.get("risk_factors", []),
        charts_described=data.get("charts_described", []),
        pages_analyzed=len(extraction.selected_page_images) if use_images else 0,
        total_pages=extraction.total_pages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    log.info(
        f"Analyzed {file_name}: priority={priority}, "
        f"{'multimodal' if use_images else 'text-only'}, "
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
