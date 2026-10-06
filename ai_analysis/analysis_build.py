"""Turn a deep-analysis JSON response into a PdfAnalysis (2026-10-06).

Moved verbatim out of ai_analysis/analyzer.analyze_pdf_deep so the same
field mapping, anchor check and conference-session resolution run for
Gemini's answer and for the Claude analysis lane (.github/workflows/
pilot-analysis.yml, spec 2026-10-06-retire-gemini-pdf-pipeline). Standard
library only, so it runs on a GitHub runner without the bot's
dependencies.
"""
from __future__ import annotations

import json
import logging
import re

from ai_analysis.models import (
    EntityMention, KeyDataPoint, MacroIndicator, MarketMover, PdfAnalysis,
    SectorView, TensionPoint, ThemeStance, TradeIdea,
)

log = logging.getLogger(__name__)


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


# One name per house, spelled the way Gemini's 30-day rows most often had
# it (2026-10-06). Both lanes varied ("BofA Securities", "BofA Global
# Research", "Bank of America"), and the Claude lane added provenance
# notes ("Citi (via ZeroHedge / Market Ear)"). /ask groups desk notes by
# this field and credits the desk by it.
_SOURCE_ALIASES = {
    "bofa": "Bank of America", "bofa securities": "Bank of America",
    "bofa global research": "Bank of America", "bank of america securities": "Bank of America",
    "bank of america global research": "Bank of America", "merrill lynch": "Bank of America",
    "gs": "Goldman Sachs", "goldman": "Goldman Sachs",
    "goldman sachs global investment research": "Goldman Sachs",
    "jpm": "JPMorgan", "j.p. morgan": "JPMorgan", "jp morgan": "JPMorgan",
    "jpmorgan chase": "JPMorgan", "j.p. morgan chase": "JPMorgan",
    "citigroup": "Citi", "citi research": "Citi", "citibank": "Citi",
    "ms": "Morgan Stanley", "db": "Deutsche Bank",
    "société générale": "Societe Generale", "socgen": "Societe Generale",
    "tme": "The Market Ear", "market ear": "The Market Ear",
    "zero hedge": "ZeroHedge", "zerohedge": "ZeroHedge",
}


def canonical_source(source: str) -> str:
    """'Citi (via ZeroHedge / Market Ear)' -> 'Citi'; 'BofA Global Research'
    -> 'Bank of America'. Anything unknown passes through trimmed."""
    s = re.sub(r"\([^)]*\)", " ", source or "")
    s = re.split(r"\s+via\s+|\s*,?\s+citing\s+", s, maxsplit=1, flags=re.I)[0]
    s = re.sub(r"\s+", " ", s).strip(" ,;-")
    if not s:
        return (source or "").strip() or "Unknown"
    return _SOURCE_ALIASES.get(s.lower(), s)


def build_analysis(data: dict, *, pdf_file_id: int, file_name: str, priority: str,
                   text_content: str, total_pages: int, pages_analyzed: int = 0,
                   input_tokens: int = 0, output_tokens: int = 0) -> PdfAnalysis:
    """The PdfAnalysis for one document from the model's parsed JSON and
    the document text the anchors are checked against."""
    analysis = PdfAnalysis(
        pdf_file_id=pdf_file_id,
        file_name=file_name,
        source=canonical_source(data.get("source") or "Unknown"),
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
        pages_analyzed=pages_analyzed,  # >0 when multimodal pass ran
        total_pages=total_pages,
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

    # Conference sessions (spec 2026-09-09): ENFORCING, unlike the
    # warn-only check above. A slot whose schedule line is not in the
    # text, or whose date is not printed in the text, is dropped here,
    # while the source is still in memory. Stored as plain dicts so the
    # JSON round-trip and the DB helper read one shape.
    try:
        from dataclasses import asdict as _asdict
        from ai_analysis.conference_sessions import resolve_sessions
        _sessions, _cs_stats = resolve_sessions(
            data.get("conference_sessions") or [], text_content,
            file_name=file_name)
        analysis.conference_sessions = [_asdict(s) for s in _sessions]
    except Exception as e:
        log.warning(f"conference sessions skipped for {file_name}: {e}")
    return analysis
