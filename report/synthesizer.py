"""Cross-PDF synthesis using Gemini to generate Market Pulse reports."""

import json
import logging
from dataclasses import asdict
from datetime import date, datetime, timedelta

from google import genai
from google.genai import types

from ai_analysis.models import PdfAnalysis
from ai_analysis.prompts import (
    DAILY_SYNTHESIS_SYSTEM, DAILY_SYNTHESIS_USER,
)
from report.market_data import fetch_market_snapshot
from report.news_data import (
    fetch_news_snapshot, fetch_earnings_calendar, fetch_economic_calendar,
)
from report.models import DailyReport
from config import settings
import db

log = logging.getLogger(__name__)


def _get_client() -> genai.Client:
    return genai.Client(api_key=settings.google_api_key)


def _build_ticker_map(analyses: list[PdfAnalysis]) -> dict[str, dict]:
    """Aggregate entities_mentioned across all PDFs into a dedup ticker map.

    Returns {TICKER: {"name": str, "asset_class": str, "mentions": int}}.
    Only entities with a non-empty ticker are included.
    Cashtags are only valid for stock/etf/crypto/index — other asset classes
    are kept in the map for synthesis context but flagged as no_cashtag=True.
    """
    CASHTAG_CLASSES = {"stock", "etf", "crypto", "index"}
    out: dict[str, dict] = {}
    for a in analyses:
        for e in a.entities_mentioned:
            if not e.ticker:
                continue
            ticker = e.ticker.strip().upper()
            if not ticker:
                continue
            if ticker not in out:
                out[ticker] = {
                    "name": e.name,
                    "asset_class": (e.asset_class or "").lower().strip(),
                    "mentions": 1,
                    "no_cashtag": (e.asset_class or "").lower().strip() not in CASHTAG_CLASSES,
                }
            else:
                out[ticker]["mentions"] += 1
    return out


def _compute_stats(analyses: list[PdfAnalysis]) -> dict:
    """Summary stats for the footer: top sources, priority mix, date range."""
    from collections import Counter

    source_counts = Counter(a.source or "Unknown" for a in analyses)
    priority_counts = Counter(a.priority or "unknown" for a in analyses)

    published_dates = [a.published_at[:10] for a in analyses if a.published_at]
    earliest = min(published_dates) if published_dates else None
    latest = max(published_dates) if published_dates else None

    return {
        "pdf_count": len(analyses),
        "top_sources": source_counts.most_common(5),
        "priority_mix": dict(priority_counts),
        "earliest_upload": earliest,
        "latest_upload": latest,
    }


def _analyses_to_json(analyses: list[PdfAnalysis]) -> str:
    """Convert analyses to a compact JSON string for the synthesis prompt."""
    compact = []
    for a in analyses:
        entry = {
            "source": a.source,
            "title": a.title,
            "type": a.report_type,
            "priority": a.priority,
            "published": (a.published_at or "unknown")[:10],  # YYYY-MM-DD only
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
        if a.vol_and_positioning:
            entry["vol_positioning"] = a.vol_and_positioning
        if a.geopolitical:
            entry["geopolitical"] = a.geopolitical
        if a.cross_bank_references:
            entry["cross_bank_refs"] = a.cross_bank_references
        if a.entities_mentioned:
            entry["entities"] = [
                {"name": e.name, "ticker": e.ticker, "class": e.asset_class}
                for e in a.entities_mentioned
            ]
        compact.append(entry)
    return json.dumps(compact, indent=1)


async def synthesize_daily_pulse(
    analyses: list[PdfAnalysis],
    use_prev_context: bool = True,
) -> DailyReport:
    """Generate the Daily Market Pulse from all today's analyses.

    Args:
        analyses: per-PDF analyses to synthesize.
        use_prev_context: if True (default), include the last scheduled pulse's
            markdown as context for diff-vs-yesterday framing. Set False for
            standalone manual pulses that should not be biased by prior pulse
            structure.
    """
    import pytz
    from config import settings as _settings

    client = _get_client()
    today = date.today().isoformat()
    # Build day-of-week + current time context so Gemini can distinguish
    # "Tuesday BMO" = today vs "Tuesday BMO" = future this week, and can move
    # already-released events from WHAT TO WATCH to RECAP.
    try:
        tz = pytz.timezone(_settings.timezone)
        now_local = datetime.now(tz)
        today_label = f"{today} ({now_local.strftime('%A')})"
        now_label = now_local.strftime("%H:%M %Z")
    except Exception:
        today_label = today
        now_label = datetime.utcnow().strftime("%H:%M UTC")

    analyses_json = _analyses_to_json(analyses)
    market_snapshot = fetch_market_snapshot()
    news_snapshot = fetch_news_snapshot(since_hours=48, limit=15)
    earnings_calendar = fetch_earnings_calendar(days_ahead=7)
    economic_calendar = fetch_economic_calendar(days_ahead=7)

    # Build the ticker lookup and render as a prompt section
    ticker_map = _build_ticker_map(analyses)
    if ticker_map:
        # Sort by mentions desc so the most-referenced names are at the top
        sorted_tickers = sorted(ticker_map.items(), key=lambda kv: -kv[1]["mentions"])
        cashtag_lines = []
        no_cashtag_lines = []
        for ticker, info in sorted_tickers:
            line = f"  {ticker} — {info['name']} ({info['asset_class']}, {info['mentions']} mentions)"
            if info["no_cashtag"]:
                no_cashtag_lines.append(line)
            else:
                cashtag_lines.append(line)
        ticker_block_parts = ["TICKER LOOKUP — use $TICKER (cashtag format) when referring to these:"]
        if cashtag_lines:
            ticker_block_parts.append("\n".join(cashtag_lines))
        if no_cashtag_lines:
            ticker_block_parts.append("\nDo NOT prefix $ for these (FX / commodity / other — reference by name):")
            ticker_block_parts.append("\n".join(no_cashtag_lines))
        ticker_block = "\n".join(ticker_block_parts)
    else:
        ticker_block = "TICKER LOOKUP: (none extracted — use only tickers that clearly appear in the research text)"

    # Previous scheduled pulse for cross-day continuity — only when the caller
    # wants it. Scheduled pulses use this to diff vs yesterday; manual /pulse
    # skips it so each ad-hoc run is fully standalone.
    if not use_prev_context:
        prev_context = (
            "PREVIOUS PULSE: (this is a standalone manual pulse — no prior-pulse "
            "comparison requested. Treat this as a fresh snapshot of the current "
            "research window. Do NOT anchor on any specific previous structure.)"
        )
    else:
        prev = db.get_last_daily_pulse()
        prev_context = "PREVIOUS PULSE: (none — this is the first scheduled pulse)"
        if prev and prev.get("created_at"):
            try:
                prev_ts = datetime.fromisoformat(prev["created_at"][:19])
                age = datetime.utcnow() - prev_ts
                if age <= timedelta(hours=48):
                    # Extract the Insights theme headers from yesterday's pulse
                    # so the model knows what to AVOID repeating. Passing the full
                    # markdown caused Gemini to copy it verbatim.
                    import re
                    md = prev["report_markdown"] or ""
                    # Match bolded theme headers (e.g. "**The Systematic Squeeze**")
                    theme_headers = re.findall(r"\*\*([^*\n]{5,80})\*\*", md)
                    # Also grab section headers
                    section_heads = re.findall(r"^##+\s*([^\n]+)", md, re.MULTILINE)
                    themes_list = [t.strip() for t in theme_headers if t.strip()][:12]
                    prev_context = (
                        f"PREVIOUS PULSE SUMMARY (from {prev['created_at'][:16].replace('T', ' ')} UTC, "
                        f"~{int(age.total_seconds() / 3600)}h ago, {prev['pdf_count']} reports):\n\n"
                        f"Themes already covered in yesterday's pulse (DO NOT REPEAT VERBATIM — these are the exact headlines the reader saw yesterday):\n"
                        + "\n".join(f"  - {t}" for t in themes_list)
                        + "\n\nYour job today:\n"
                        + "1. For each theme above, ask: has the research today materially advanced it? If no → SKIP. If yes → lead with 'Since yesterday: [what's new/changed]'.\n"
                        + "2. Actively hunt for themes that are NOT in the list above — new catalysts, fresh desk calls, new positioning data.\n"
                        + "3. Your pulse should be notably different from yesterday's. If today's pulse would look 80%+ the same as yesterday's, you've failed.\n"
                        + "4. Do NOT rewrite yesterday's themes with synonyms and new numbers. That's the same pulse in a trench coat."
                    )
                else:
                    prev_context = (
                        f"PREVIOUS PULSE: last scheduled pulse was "
                        f"~{int(age.total_seconds() / 3600)}h ago — too stale to use for comparison. "
                        f"Treat this as a fresh pulse with no prior context."
                    )
            except (ValueError, TypeError):
                pass

    user_prompt = DAILY_SYNTHESIS_USER.format(
        pdf_count=len(analyses),
        today=today_label,
        now=now_label,
        market_snapshot=market_snapshot,
        news_snapshot=news_snapshot,
        earnings_calendar=earnings_calendar,
        economic_calendar=economic_calendar,
        ticker_block=ticker_block,
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
        stats=_compute_stats(analyses),
    )
