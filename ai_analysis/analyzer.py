"""Gemini API analysis orchestrator.

Implements the tiered analysis strategy:
  Tier 1: Triage (cheap text-only classification)
  Tier 2: Deep analysis (text-only; sends full document to Gemini)
  Tier 3: Synthesis (cross-PDF report generation, handled by report/synthesizer.py)

Uses Google Gemini for all tiers.
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
    MacroIndicator, TradeIdea, EntityMention,
    KeyDataPoint, TensionPoint, ThemeStance,
)
from ai_analysis.prompts import (
    TRIAGE_SYSTEM_PROMPT, TRIAGE_USER_PROMPT,
    ANALYSIS_SYSTEM_PROMPT, ANALYSIS_USER_PROMPT_TEXT_ONLY,
    ANALYSIS_USER_PROMPT_MULTIMODAL,
)
from ai_analysis.rate_limiter import RateLimiter
from pdf_processing.models import PdfExtraction
from pdf_processing.page_selector import select_pages
from pdf_processing.extractor import render_pages_to_images
from config import settings


# Multimodal trigger — selectively render the densest pages of equity-research
# / vol / derivatives pieces from top investment banks. The vast majority of
# HIGH-priority research is well-summarized in text; multimodal pays off
# only on dense exhibit tables (Goldman/MS earnings models) and cross-asset
# visualizations (DB dislocations, BofA Hartnett heatmaps). See assessment
# in conversation history for the empirical 20% lift figure.
_MULTIMODAL_SOURCES = {
    "Goldman Sachs", "Morgan Stanley", "JPMorgan", "JP Morgan",
    "Citi", "Citigroup", "Deutsche Bank", "Bank of America", "BofA",
}
_MULTIMODAL_REPORT_TYPES = {
    "equity_research", "derivatives", "vol_commentary",
}
_MULTIMODAL_TITLE_HINTS = (
    "preview", "model", "dispersion", "dislocation",
    "exhibit", "deep dive", "deep-dive",
)


def _should_run_multimodal(
    priority: str,
    source: str | None,
    report_type: str | None,
    file_name: str,
    total_pages: int,
) -> bool:
    """Selective multimodal trigger.

    HIGH-priority pieces from top banks where the report type or filename
    suggests dense exhibits / cross-asset visualizations. Skip very short
    notes (≤4 pages) — those are typically TME-style chart-and-paragraph
    pieces where text already captures the takeaway.
    """
    if (priority or "").lower() != "high":
        return False
    if total_pages < 5:
        return False
    src = (source or "").strip()
    if src not in _MULTIMODAL_SOURCES:
        return False
    rt = (report_type or "").strip().lower()
    fname_lower = (file_name or "").lower()
    if rt in _MULTIMODAL_REPORT_TYPES:
        return True
    if any(hint in fname_lower for hint in _MULTIMODAL_TITLE_HINTS):
        return True
    return False

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
    """Return Gemini's priority as-is. No source- or topic-based overrides.

    Previously we floored tier-1 banks (GS/JPM/BofA/MS) at MEDIUM and boosted
    HIGH topics to HIGH regardless of source. Removed — Gemini is the sole
    priority decider based on content.
    """
    return gemini_priority if gemini_priority in ("high", "medium", "low") else "medium"


async def triage_pdf(file_name: str, text_preview: str, folder_path: str = "") -> TriageResult:
    """Tier 1: Quick classification using Gemini (text-only).

    Returns priority (high/medium/low), report_type, key tickers, and summary.
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    # Cap triage input at ~8K tokens (32K chars). Older 8K-char cap was
    # missing the body of longer docs (calendars, multi-section morning
    # briefings) — the first 8K chars often = cover + 1 page of exec
    # summary, hiding the actual content from the priority decision.
    preview = text_preview[:32000]

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
    source: str | None = None,
    report_type: str | None = None,
) -> PdfAnalysis:
    """Tier 2: Deep analysis using Gemini.

    Default path: text-only — sends the full document to Gemini. Gemini's
    1M-token context handles even 90-page UBS Contextual Diaries.

    Selective multimodal path: when source/report_type/filename suggest
    dense exhibit tables or cross-asset visualizations (top-bank equity
    research / vol / derivatives), the densest 3-5 pages are rendered to
    images and attached. Triggered by _should_run_multimodal().
    """
    client = _get_client()
    limiter = _get_rate_limiter()

    text_content = extraction.full_text
    image_parts: list[types.Part] = []
    pages_analyzed_count = 0

    use_multimodal = _should_run_multimodal(
        priority=priority,
        source=source,
        report_type=report_type,
        file_name=file_name,
        total_pages=extraction.total_pages,
    )

    if use_multimodal:
        try:
            # Cap at 30 pages. Gemini Flash Lite image tokens are cheap
            # (~$0.001/page) and many top-bank reports have 10-20+ exhibits
            # that would be cut by a tighter cap. The page_selector already
            # filters disclaimers and sub-threshold pages, so 30 is a soft
            # upper bound — typical reports render fewer.
            mm_max_pages = 30
            selected = await asyncio.to_thread(
                select_pages, extraction.pages, mm_max_pages
            )
            if selected:
                images = await asyncio.to_thread(
                    render_pages_to_images, extraction.file_path, selected
                )
                for img in images:
                    img_bytes = base64.b64decode(img.image_base64)
                    image_parts.append(
                        types.Part.from_bytes(data=img_bytes, mime_type=img.media_type)
                    )
                pages_analyzed_count = len(images)
                log.info(
                    f"Multimodal pass ENABLED for {file_name}: "
                    f"{pages_analyzed_count} pages rendered (source={source}, type={report_type})"
                )
            else:
                log.info(f"Multimodal eligible but page_selector returned 0 pages for {file_name}")
                use_multimodal = False
        except Exception as e:
            log.warning(f"Multimodal rendering failed for {file_name}: {e} — falling back to text-only")
            use_multimodal = False
            image_parts = []
            pages_analyzed_count = 0

    if use_multimodal and image_parts:
        user_text = ANALYSIS_USER_PROMPT_MULTIMODAL.format(
            file_name=file_name,
            total_pages=extraction.total_pages,
            image_pages=pages_analyzed_count,
            text_content=text_content,
        )
    else:
        user_text = ANALYSIS_USER_PROMPT_TEXT_ONLY.format(
            file_name=file_name,
            total_pages=extraction.total_pages,
            text_content=text_content,
        )
    content_parts: list[types.Part] = [types.Part.from_text(text=user_text)]
    content_parts.extend(image_parts)

    start_time = time.time()

    # Token-budget reservation BEFORE firing the call. Deep analysis on
    # a 400-page table-heavy PDF can consume 80K+ tokens; without this
    # guard one bad PDF can blow the daily budget (CLAUDE.md notes the
    # $10 cap was hit once). The estimate is conservative — we charge
    # the full text content size plus a buffer for response. The post-
    # call record_actual corrects with the real usage_metadata so
    # mid-sized PDFs don't over-debit the budget.
    from ai_analysis.token_budget import get_budget, BudgetExceeded
    text_chars = len(text_content or "")
    # Heuristic: 4 chars/token for English text + 2000-token response
    # cap (max_output_tokens) + 500-token system instruction + image
    # parts each ~258 tokens for Flash inputs.
    estimated_tokens = (
        text_chars // 4
        + (settings.gemini_max_tokens or 2000)
        + 500
        + 258 * len(image_parts)
    )
    try:
        get_budget().reserve_or_raise(
            estimated_tokens=estimated_tokens,
            caller=f"pdf_deep_analysis:{file_name[:40]}",
        )
    except BudgetExceeded as e:
        log.warning(
            f"Skipping deep analysis for {file_name} — token budget: {e}"
        )
        raise

    # TRUNCATION GUARD (2026-07-15 review). A dense morning briefing can
    # exceed max_output_tokens; the response then ends mid-JSON and the
    # parser either fails (analysis silently becomes {}) or — worse —
    # rfind("}") closes on an earlier brace and a PARTIAL object ships as
    # if complete, silently dropping the tail fields (theme_stances,
    # key_data_points live at the end of the schema). Truncation is
    # detectable from finish_reason: retry once with a raised cap, and if
    # it STILL truncates, raise — a FAILED row + auto-retry is honest,
    # a half-analysis is not.
    max_out = settings.gemini_max_tokens
    truncated = False
    for attempt in (1, 2):
        async with limiter:
            response = await client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=content_parts,
                config=types.GenerateContentConfig(
                    system_instruction=ANALYSIS_SYSTEM_PROMPT,
                    max_output_tokens=max_out,
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
        finish = ""
        try:
            finish = str(response.candidates[0].finish_reason or "")
        except (AttributeError, IndexError, TypeError):
            finish = ""
        truncated = "MAX_TOKENS" in finish.upper()
        if not truncated:
            break
        if attempt == 1:
            new_cap = max(8192, (max_out or 4096) * 2)
            # Reserve the retry's extra output before refiring (2026-08-20
            # review: the second call was unbudgeted). Input was already
            # covered by attempt 1's reservation; the delta is output cap.
            try:
                get_budget().reserve_or_raise(
                    estimated_tokens=new_cap,
                    caller=f"pdf_deep_retry:{file_name[:40]}",
                )
            except BudgetExceeded as e:
                log.warning(
                    f"Truncation retry for {file_name} skipped — "
                    f"token budget: {e}"
                )
                break
            log.warning(
                f"Deep analysis for {file_name} truncated at "
                f"max_output_tokens={max_out} — retrying with {new_cap}"
            )
            max_out = new_cap
    if truncated:
        raise RuntimeError(
            f"deep analysis output truncated at max_output_tokens={max_out} "
            f"even after retry — refusing to parse a partial JSON"
        )

    duration = time.time() - start_time
    result_text = response.text

    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0
    # Reconcile actual usage against reservation; frees up budget if
    # the response came in under the conservative estimate.
    try:
        get_budget().record_actual(
            estimated=estimated_tokens,
            actual=input_tokens + output_tokens,
            caller=f"pdf_deep_analysis:{file_name[:40]}",
        )
    except Exception as e:
        log.debug(f"token_budget record_actual non-fatal failure: {e}")

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
        entities_mentioned=[
            em for em in (
                _safe_dataclass(EntityMention, e) for e in data.get("entities_mentioned", [])
                if isinstance(e, dict)
            ) if em is not None
        ],
        key_data_points=[
            kdp for kdp in (
                _safe_dataclass(KeyDataPoint, kdp) for kdp in data.get("key_data_points", [])
                if isinstance(kdp, dict)
            ) if kdp is not None
        ],
        tension_points=[
            tp for tp in (
                _safe_dataclass(TensionPoint, tp) for tp in data.get("tension_points", [])
                if isinstance(tp, dict)
            ) if tp is not None
        ],
        theme_stances=[
            ts for ts in (
                _safe_dataclass(ThemeStance, ts) for ts in data.get("theme_stances", [])
                if isinstance(ts, dict)
            ) if ts is not None
        ],
        contextual_mentions=[
            m.strip() for m in data.get("contextual_mentions", [])
            if isinstance(m, str) and m.strip()
        ],
        pages_analyzed=pages_analyzed_count,  # >0 when multimodal pass ran
        total_pages=extraction.total_pages,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    # Anchor verification — redesign step 2, WARN-ONLY. The source text
    # is in memory right here and only here; verifying later would mean
    # re-extracting the PDF. Stats ride into analysis_json for QC and
    # the pilot. Nothing is dropped or retried at this step.
    try:
        from ai_analysis.anchor_check import check_anchors
        analysis.anchor_check = check_anchors(
            analysis.key_data_points, text_content)
        ac = analysis.anchor_check
        if ac.get("missed"):
            log.warning(
                f"Anchor check {file_name}: {ac['missed']}/{ac['matched'] + ac['missed']} "
                f"verifiable anchors NOT found in source "
                f"(rate={ac.get('match_rate')}); first miss: "
                f"{(ac.get('misses') or [{}])[0]}"
            )
        elif ac.get("total"):
            log.info(
                f"Anchor check {file_name}: {ac['matched']}/{ac['total']} "
                f"matched (empty={ac.get('empty', 0)}, "
                f"too_short={ac.get('too_short', 0)})"
            )
    except Exception as e:
        log.warning(f"Anchor check skipped for {file_name}: {e}")

    log.info(
        f"Analyzed {file_name}: priority={priority}, "
        f"{'multimodal' if pages_analyzed_count else 'text-only'}"
        f"{f' ({pages_analyzed_count}p)' if pages_analyzed_count else ''}, "
        f"{input_tokens} in / {output_tokens} out, "
        f"{duration:.1f}s"
    )
    return analysis

