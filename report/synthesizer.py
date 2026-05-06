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
    DRAFT_SYSTEM, DRAFT_USER,
    AUDIT_SYSTEM, AUDIT_USER,
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


def _fmt_et(utc_iso: str) -> str:
    """Convert a UTC ISO timestamp to ET display like '2026-04-18 09:00 EDT'."""
    if not utc_iso:
        return ""
    try:
        import pytz
        clean = utc_iso.replace("T", " ")[:19]
        dt = datetime.fromisoformat(clean).replace(tzinfo=pytz.UTC)
        return dt.astimezone(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return utc_iso[:16].replace("T", " ")


# Theme buckets — keyword patterns scanned across each PDF's title +
# key_insights + structured fields. Counts are by DISTINCT bank source so
# a single bank uploading 12 notes on AI capex still counts as 1.
# Patterns are lowercase substring matches; tune over time.
_THEME_PATTERNS: dict[str, list[str]] = {
    "AI capex / hyperscaler earnings": [
        "ai capex", "ai infra", "hyperscaler", "data center capex",
        "wafer fab", "wfe ", "$751b", "$750", "$751", "ai chip",
        "ai monetiz", "ai super-cycle", "ai super cycle",
        "mag7", "magnificent 7", "magnificent seven", "mag 7",
        "ai infrastructure", "gpu demand", "neocloud",
    ],
    "Rate-cut repricing / yields breakout": [
        "rate cut", "rate hike", "no cuts", "no rate cut", "fomc dissent",
        "10y broke", "10-year yield", "10-yr yield", "10y above",
        "30y above 5", "30-year yield", "30-yr above",
        "bear-flatten", "bear flatten", "bear-steepen", "yields breaking",
        "fed transition", "warsh", "hawkish hold", "hawkish pivot",
        "dot plot", "cut probability", "priced out", "priced-out",
        "dissent", "easing bias", "move index",
    ],
    "Hormuz / Iran / oil shock": [
        "hormuz", "strait of hormuz", "iran", "uae attack", "fujairah",
        "brent", "wti ", "$usd_oil", "blockade", "shipping disruption",
        "oil shock", "energy shock", "oil capex", "$150/bbl", "$150 oil",
        "opec+", "opecxit", "energy security",
    ],
    "Crypto institutional view": [
        "btc ", "$btc", "bitcoin", "ethereum", "eth ", "solana", "sol ",
        "crypto etf", "spot etf", "on-chain", "stablecoin", "spot bitcoin",
        "crypto inflows", "btc etf", "eth etf",
    ],
    "Fed transition / dovish-hawkish surprise": [
        "warsh confirmation", "fed transition", "fed chair transition",
        "powell legacy", "powell exit", "warsh dovish", "warsh hawkish",
        "warsh balance sheet",
    ],
    "Tech dispersion / K-shaped": [
        "dispersion", "neocloud", "cpu-levered", "memory-levered",
        "k-shaped", "intra-tech rotation", "ai winners", "software lagg",
        "semis vs software", "semis-vs-software",
    ],
    "Positioning / late-stage / squeeze": [
        "cta ", "ctas ", "systematic", "hedge fund net", "leverage",
        "market concentration", "short squeeze", "melt-up", "melt up",
        "shooting star", "exhaustion", "crowded positioning",
        "prime brokerage", "retail inflows", "401(k)",
    ],
    "Major M&A / deal flow": [
        "acquisition", "all-stock deal", "unsolicited proposal",
        "buyout", "merger", "strategic stake", "spin-off", "spinoff",
        "going private", "13d filing", "acq.", "to acquire",
    ],
    "Earnings reactions (single-name catalysts)": [
        "1q26 beat", "1q26 miss", "q1 beat", "q1 miss", "q1'26",
        "first take", "results released", "earnings reaction",
        "guide-down", "guide down", "guide-up", "raise full year",
        "ramp 2h", "second half guide",
    ],
}


def _classify_themes(analyses: list[PdfAnalysis]) -> dict[str, dict]:
    """Scan analyses for theme keyword hits, count distinct banks per theme.

    Returns {theme_name: {"banks": int, "pdfs": int, "sources": list[str]}}.
    DRAFT prompt uses this to ground theme ordering — Gemini is told actual
    bank counts so it can't accidentally bury cross-bank consensus.
    """
    from dataclasses import asdict

    theme_sources: dict[str, set[str]] = {t: set() for t in _THEME_PATTERNS}
    theme_pdf_counts: dict[str, int] = {t: 0 for t in _THEME_PATTERNS}

    for a in analyses:
        # Build a single lowercase blob from the structured analysis fields
        parts: list[str] = []
        parts.append((a.title or "").lower())
        parts.extend((ins or "").lower() for ins in a.key_insights or [])
        for mm in a.market_movers or []:
            d = asdict(mm)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        for sv in a.sector_views or []:
            d = asdict(sv)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        for mi in a.macro_indicators or []:
            d = asdict(mi)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        for ti in a.trade_ideas or []:
            d = asdict(ti)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        parts.extend((rf or "").lower() for rf in a.risk_factors or [])
        parts.extend((cv or "").lower() for cv in a.crypto_views or [])
        parts.extend((vp or "").lower() for vp in a.vol_and_positioning or [])
        parts.extend((g or "").lower() for g in a.geopolitical or [])
        for kdp in a.key_data_points or []:
            d = asdict(kdp)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        for tp in a.tension_points or []:
            d = asdict(tp)
            parts.append(" ".join(str(v).lower() for v in d.values() if v))
        blob = " ".join(parts)
        if not blob.strip():
            continue

        source = (a.source or "Unknown").strip()
        for theme, patterns in _THEME_PATTERNS.items():
            if any(p in blob for p in patterns):
                theme_sources[theme].add(source)
                theme_pdf_counts[theme] += 1

    return {
        theme: {
            "banks": len(theme_sources[theme]),
            "pdfs": theme_pdf_counts[theme],
            "sources": sorted(theme_sources[theme]),
        }
        for theme in _THEME_PATTERNS
    }


def _format_theme_coverage(theme_map: dict[str, dict]) -> str:
    """Render theme counts as a forcing-function block for the DRAFT prompt."""
    lines = [
        "THEME COVERAGE — distinct bank counts across the corpus (use this to anchor INSIGHTS ordering; the highest-count themes MUST appear unless conviction-disqualified):",
    ]
    # Sort by bank count desc, then pdf count desc, drop themes with 0 banks
    ranked = sorted(
        theme_map.items(),
        key=lambda kv: (-kv[1]["banks"], -kv[1]["pdfs"], kv[0]),
    )
    for theme, info in ranked:
        if info["banks"] == 0:
            continue
        srcs = info["sources"][:6]
        more = info["banks"] - len(srcs)
        srcs_str = ", ".join(srcs)
        if more > 0:
            srcs_str += f", +{more} more"
        lines.append(
            f"  - {theme}: {info['banks']} banks / {info['pdfs']} PDFs "
            f"({srcs_str})"
        )
    if len(lines) == 1:
        lines.append("  (no themes matched any pattern — corpus may be unusually narrow today)")
    return "\n".join(lines)


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
        if a.key_data_points:
            entry["data_points"] = [asdict(kdp) for kdp in a.key_data_points]
        if a.tension_points:
            entry["tensions"] = [asdict(tp) for tp in a.tension_points]
        compact.append(entry)
    return json.dumps(compact, indent=1)


async def synthesize_daily_pulse(
    analyses: list[PdfAnalysis],
    use_prev_context: bool = True,
) -> DailyReport:
    """Generate the Daily Market Pulse via a two-stage pipeline.

    Stage 1 (DRAFT): synthesize narrative from research PDFs only — no live
    data. Focuses on INSIGHTS & ALPHA depth and WHAT TO WATCH research-backed
    events. RECAP left as `[LIVE PRICE RECAP]` placeholder.

    Stage 2 (AUDIT): review the draft against live market snapshot, news,
    economic calendar (RELEASED events), earnings calendar, and current time.
    Rewrite RECAP with live prices + released data + news. Fix tickers, timing,
    session framing. Preserve INSIGHTS & ALPHA and WHAT TO WATCH analytical
    content.

    Args:
        analyses: per-PDF analyses to synthesize.
        use_prev_context: if True, include the last scheduled pulse's theme
            headers as a "don't repeat" directive in Stage 1. Currently False
            for both scheduled and manual pulses (independence preferred).
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
        day_of_week = now_local.strftime("%A")
        today_label = f"{today} ({day_of_week})"
        now_label = now_local.strftime("%H:%M %Z")
        # Weekend flag: US equity, bond, and futures markets are closed Sat + Sun.
        # Crypto trades 24/7 but also quiet on weekends.
        is_weekend = day_of_week in ("Saturday", "Sunday")
    except Exception:
        today_label = today
        now_label = datetime.utcnow().strftime("%H:%M UTC")
        is_weekend = False

    market_status_note = ""
    if is_weekend:
        market_status_note = (
            "\n\n**MARKET STATUS: US markets are CLOSED TODAY (weekend).** "
            "The live price snapshot below shows LAST CLOSE — Friday's closing levels, "
            "not 'today's move.' Do NOT write sentences like 'SPX is up 2% today' — "
            "it's a weekend, nothing has traded. Phrase price references as "
            "'as of Friday's close' or 'heading into Monday.' RECAP should focus on "
            "what weekend news has done to sentiment and what's set up for Monday's open, "
            "not intraday action. Crypto trades 24/7 so BTC/ETH price commentary is fine, "
            "but weekend crypto volumes are thin — don't over-read short-term moves."
        )

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

    # Always compute the previous scheduled pulse's theme list — used as a
    # dedup reference even when DRAFT stage is standalone. Scheduled pulses
    # additionally get the full diff-framing directive in DRAFT.
    prev_themes_list: list[str] = []
    prev_age_hours: int | None = None
    prev = db.get_last_daily_pulse()
    if prev and prev.get("created_at"):
        try:
            prev_ts = datetime.fromisoformat(prev["created_at"][:19])
            age = datetime.utcnow() - prev_ts
            if age <= timedelta(hours=48):
                import re
                md = prev["report_markdown"] or ""
                theme_headers = re.findall(r"\*\*([^*\n]{5,80})\*\*", md)
                prev_themes_list = [t.strip() for t in theme_headers if t.strip()][:12]
                prev_age_hours = int(age.total_seconds() / 3600)
        except (ValueError, TypeError):
            pass

    # DRAFT-stage prev-pulse directive — only when caller opts in (scheduled).
    # Manual /pulse gets the "standalone" message so it doesn't anchor on
    # yesterday's structure.
    if not use_prev_context:
        prev_context = (
            "PREVIOUS PULSE: (this is a standalone manual pulse — no prior-pulse "
            "comparison requested. Treat this as a fresh snapshot of the current "
            "research window. Do NOT anchor on any specific previous structure.)"
        )
    elif not prev_themes_list:
        prev_context = (
            "PREVIOUS PULSE: (none available — this is the first scheduled pulse "
            "or the last one is too stale to compare against.)"
        )
    else:
        prev_context = (
            f"PREVIOUS PULSE SUMMARY (~{prev_age_hours}h ago, {prev['pdf_count']} reports):\n\n"
            f"Themes already covered in yesterday's pulse (DO NOT REPEAT VERBATIM — these are the exact headlines the reader saw yesterday):\n"
            + "\n".join(f"  - {t}" for t in prev_themes_list)
            + "\n\nYour job today:\n"
            + "1. For each theme above, ask: has the research today materially advanced it? If no → SKIP. If yes → lead with 'Since yesterday: [what's new/changed]'.\n"
            + "2. Actively hunt for themes that are NOT in the list above — new catalysts, fresh desk calls, new positioning data.\n"
            + "3. Your pulse should be notably different from yesterday's. If today's pulse would look 80%+ the same as yesterday's, you've failed.\n"
            + "4. Do NOT rewrite yesterday's themes with synonyms and new numbers. That's the same pulse in a trench coat."
        )

    # AUDIT-stage dedup reference — just the theme list, no directive. Passed
    # regardless of use_prev_context so manual pulses also get safety-net dedup.
    if prev_themes_list:
        audit_prev_block = (
            f"PREVIOUS PULSE THEMES (~{prev_age_hours}h ago) — use this list to CUT any theme in the draft that merely restates one of these without a materially new catalyst today:\n"
            + "\n".join(f"  - {t}" for t in prev_themes_list)
        )
    else:
        audit_prev_block = "PREVIOUS PULSE THEMES: (none — no recent prior pulse to dedupe against.)"

    # Append weekend notice to market_snapshot so it's co-located with the prices
    if market_status_note:
        market_snapshot = market_snapshot + market_status_note

    # ==========================================================
    # STAGE 1: DRAFT from research only (no live data)
    # ==========================================================
    # Programmatic theme classifier — count distinct banks per theme bucket
    # so DRAFT prompt can anchor INSIGHTS ordering on actual coverage,
    # not Gemini's gestalt of "what feels dominant."
    theme_map = _classify_themes(analyses)
    theme_coverage_block = _format_theme_coverage(theme_map)

    draft_prompt = DRAFT_USER.format(
        pdf_count=len(analyses),
        today=today_label,
        now=now_label,
        ticker_block=ticker_block,
        prev_pulse=prev_context,
        theme_coverage=theme_coverage_block,
        analyses_json=analyses_json,
    )
    draft_response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=draft_prompt,
        config=types.GenerateContentConfig(
            system_instruction=DRAFT_SYSTEM,
            max_output_tokens=12288,  # room for 1800-2200 word target with 3-6 substantial themes
            temperature=0.4,  # slightly higher for creative narrative
        ),
    )
    draft_markdown = draft_response.text
    stage1_in = draft_response.usage_metadata.prompt_token_count or 0
    stage1_out = draft_response.usage_metadata.candidates_token_count or 0
    log.info(f"Stage 1 (draft): {stage1_in} in / {stage1_out} out")

    # ==========================================================
    # STAGE 2: AUDIT against live data — rewrite RECAP + verify facts
    # ==========================================================
    # Derive a short session_status label for the audit prompt
    session_status = "closed (weekend)" if is_weekend else (
        "market hours — intraday" if "9:30" <= now_label[:5] < "16:00"
        else "pre-market or after-hours"
    )
    audit_prompt = AUDIT_USER.format(
        today=today_label,
        now=now_label,
        session_status=session_status,
        market_snapshot=market_snapshot,
        news_snapshot=news_snapshot,
        earnings_calendar=earnings_calendar,
        economic_calendar=economic_calendar,
        prev_pulse_themes=audit_prev_block,
        draft_markdown=draft_markdown,
    )
    audit_response = await client.aio.models.generate_content(
        model=settings.gemini_model,
        contents=audit_prompt,
        config=types.GenerateContentConfig(
            system_instruction=AUDIT_SYSTEM,
            max_output_tokens=12288,  # AUDIT may rewrite + add themes; needs same headroom as DRAFT
            temperature=0.2,  # lower temp for factual correction
        ),
    )
    markdown = audit_response.text
    stage2_in = audit_response.usage_metadata.prompt_token_count or 0
    stage2_out = audit_response.usage_metadata.candidates_token_count or 0
    log.info(f"Stage 2 (audit): {stage2_in} in / {stage2_out} out")

    input_tokens = stage1_in + stage2_in
    output_tokens = stage1_out + stage2_out

    log.info(
        f"Daily pulse synthesized (two-stage): {len(analyses)} PDFs, "
        f"{input_tokens} in / {output_tokens} out total"
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
