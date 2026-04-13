"""Cross-PDF synthesis using Gemini to generate Market Pulse reports."""

import json
import logging
from dataclasses import asdict
from datetime import date

from google import genai
from google.genai import types

from ai_analysis.models import PdfAnalysis
from ai_analysis.prompts import (
    DAILY_SYNTHESIS_SYSTEM, DAILY_SYNTHESIS_USER,
)
from report.market_data import fetch_market_snapshot
from report.models import DailyReport
from config import settings
import db

log = logging.getLogger(__name__)


def _get_client() -> genai.Client:
    return genai.Client(api_key=settings.google_api_key)


def _analyses_to_json(analyses: list[PdfAnalysis]) -> str:
    """Convert analyses to a compact JSON string for the synthesis prompt."""
    compact = []
    for a in analyses:
        entry = {
            "source": a.source,
            "title": a.title,
            "type": a.report_type,
            "priority": a.priority,
            "insights": a.key_insights,
        }
        if a.market_movers:
            entry["market_movers"] = [asdict(mm) for mm in a.market_movers]
        if a.sector_views:
            entry["sector_views"] = [asdict(sv) for sv in a.sector_views]
        if a.earnings_insights:
            entry["earnings"] = a.earnings_insights
        if a.macro_indicators:
            entry["macro"] = [asdict(mi) for mi in a.macro_indicators]
        if a.crypto_views:
            entry["crypto"] = a.crypto_views
        if a.trade_ideas:
            entry["trades"] = [asdict(ti) for ti in a.trade_ideas]
        if a.risk_factors:
            entry["risks"] = a.risk_factors
        if a.charts_described:
            entry["charts"] = a.charts_described
        compact.append(entry)
    return json.dumps(compact, indent=1)


async def synthesize_daily_pulse(analyses: list[PdfAnalysis]) -> DailyReport:
    """Generate the Daily Market Pulse from all today's analyses."""
    client = _get_client()
    today = date.today().isoformat()

    analyses_json = _analyses_to_json(analyses)
    market_snapshot = fetch_market_snapshot()

    # Pull the previous scheduled pulse to give the model cross-day continuity.
    prev = db.get_last_daily_pulse()
    if prev:
        prev_context = (
            f"PREVIOUS PULSE (from {prev['created_at'][:16].replace('T', ' ')} UTC, "
            f"{prev['pdf_count']} reports):\n\n{prev['report_markdown']}"
        )
    else:
        prev_context = "PREVIOUS PULSE: (none — this is the first scheduled pulse)"

    user_prompt = DAILY_SYNTHESIS_USER.format(
        pdf_count=len(analyses),
        today=today,
        market_snapshot=market_snapshot,
        prev_pulse=prev_context,
        analyses_json=analyses_json,
    )

    response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=DAILY_SYNTHESIS_SYSTEM,
            max_output_tokens=8192,
            temperature=0.3,
        ),
    )

    markdown = response.text
    input_tokens = response.usage_metadata.prompt_token_count or 0
    output_tokens = response.usage_metadata.candidates_token_count or 0

    log.info(
        f"Daily pulse synthesized: {len(analyses)} PDFs, "
        f"{input_tokens} in / {output_tokens} out"
    )

    return DailyReport(
        report_date=today,
        report_type="daily",
        pdf_count=len(analyses),
        markdown_content=markdown,
        raw_json={"analyses_count": len(analyses)},
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
