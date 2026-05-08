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


def _normalize_theme_tag(tag: str) -> str:
    """Canonicalize a theme tag for cross-PDF aggregation.

    Lowercase, strip punctuation/articles, collapse whitespace. So
    'AI hyperscaler capex super-cycle' and 'ai hyperscaler capex super cycle'
    cluster as the same theme. Pure-substring fuzzy matching at this layer;
    near-duplicates further merged in _merge_similar_tags() below.
    """
    import re
    if not tag:
        return ""
    t = tag.lower().strip()
    # Drop leading articles
    for art in ("the ", "a ", "an "):
        if t.startswith(art):
            t = t[len(art):]
    # Replace dashes/slashes with spaces, collapse whitespace
    t = re.sub(r"[/\-_]", " ", t)
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _merge_similar_tags(tag_to_sources: dict[str, set[str]]) -> dict[str, set[str]]:
    """Merge tag clusters whose normalized forms are substrings/supersets of each other.

    Example: 'hormuz oil shock' and 'hormuz energy shock' both normalize cleanly
    but stay separate. We merge if one contains all words of the other.
    Picks the more frequently-attested form as the canonical label.
    """
    items = sorted(tag_to_sources.items(), key=lambda kv: (-len(kv[1]), -len(kv[0])))
    merged: dict[str, set[str]] = {}
    seen_words: list[tuple[set[str], str]] = []
    for tag, srcs in items:
        words = set(tag.split())
        # Look for an existing cluster whose words are a subset or superset
        merged_into = False
        for existing_words, existing_tag in seen_words:
            # Merge if one is subset of the other AND >= 2 words shared
            shared = words & existing_words
            if len(shared) >= 2 and (
                words.issubset(existing_words) or existing_words.issubset(words)
            ):
                merged[existing_tag] |= srcs
                merged_into = True
                break
        if not merged_into:
            merged[tag] = set(srcs)
            seen_words.append((words, tag))
    return merged


def _classify_themes(analyses: list[PdfAnalysis]) -> dict[str, dict]:
    """Aggregate organic theme stances extracted at deep-analysis time.

    Each analysis carries 1-3 `theme_stances` (theme + stance + conviction +
    key_argument) extracted by Gemini from the document's actual content. We
    aggregate across all PDFs in the window, normalize the theme labels for
    case/punctuation, fuzzy-merge near-duplicates, and count distinct banks
    per resulting theme — also capturing per-theme stance breakdowns
    (supportive vs skeptical vs neutral) for downstream adjudication.

    Returns {theme_name: {
        "banks": int, "pdfs": int, "sources": list[str],
        "supportive": int, "skeptical": int, "neutral": int,
    }}.

    Backward-compat: PDFs whose deep analysis predates this schema will have
    theme_stances populated from legacy theme_tags during deserialization
    (stance defaults to neutral for those).
    """
    tag_sources: dict[str, set[str]] = {}
    tag_pdf_counts: dict[str, int] = {}
    tag_stance_counts: dict[str, dict[str, int]] = {}

    for a in analyses:
        source = (a.source or "Unknown").strip()
        seen_for_this_pdf: set[str] = set()
        for ts in a.theme_stances or []:
            raw_tag = (ts.theme or "").strip()
            norm = _normalize_theme_tag(raw_tag)
            if not norm or len(norm) < 4:  # filter junk like "ai" or "us"
                continue
            if norm in seen_for_this_pdf:
                continue  # don't double-count within one PDF
            seen_for_this_pdf.add(norm)
            tag_sources.setdefault(norm, set()).add(source)
            tag_pdf_counts[norm] = tag_pdf_counts.get(norm, 0) + 1
            stance = (ts.stance or "neutral").lower().strip()
            if stance not in ("supportive", "skeptical", "neutral"):
                stance = "neutral"
            # Anti-hallucination: directional stances (supportive/skeptical)
            # only count toward the consensus tally if grounded by evidence
            # OR a non-empty key_argument. Ungrounded directional calls fall
            # back to "neutral" — Gemini doesn't get to swing the consensus
            # without text-anchored support.
            if stance in ("supportive", "skeptical"):
                evidence = (ts.evidence or "").strip()
                key_arg = (ts.key_argument or "").strip()
                if not evidence and not key_arg:
                    stance = "neutral"
            buckets = tag_stance_counts.setdefault(
                norm, {"supportive": 0, "skeptical": 0, "neutral": 0}
            )
            buckets[stance] += 1

    # Merge near-duplicates (e.g., 'hormuz oil shock' vs 'hormuz energy shock')
    merged = _merge_similar_tags(tag_sources)
    # Recompute pdf counts and stance breakdown from the original counts,
    # summing for any tag merged into a canonical bucket.
    canonical_pdf_counts: dict[str, int] = {}
    canonical_stance_counts: dict[str, dict[str, int]] = {}
    for canonical_tag in merged:
        cwords = set(canonical_tag.split())
        n = 0
        stance_agg = {"supportive": 0, "skeptical": 0, "neutral": 0}
        for orig_tag in tag_sources.keys():
            owords = set(orig_tag.split())
            shared = cwords & owords
            if (orig_tag == canonical_tag) or (
                len(shared) >= 2 and (
                    owords.issubset(cwords) or cwords.issubset(owords)
                )
            ):
                n += tag_pdf_counts.get(orig_tag, 0)
                for k, v in tag_stance_counts.get(orig_tag, {}).items():
                    stance_agg[k] = stance_agg.get(k, 0) + v
        canonical_pdf_counts[canonical_tag] = n
        canonical_stance_counts[canonical_tag] = stance_agg

    return {
        tag: {
            "banks": len(srcs),
            "pdfs": canonical_pdf_counts.get(tag, 0),
            "sources": sorted(srcs),
            "supportive": canonical_stance_counts.get(tag, {}).get("supportive", 0),
            "skeptical": canonical_stance_counts.get(tag, {}).get("skeptical", 0),
            "neutral": canonical_stance_counts.get(tag, {}).get("neutral", 0),
        }
        for tag, srcs in merged.items()
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
        sup = info.get("supportive", 0)
        skp = info.get("skeptical", 0)
        neu = info.get("neutral", 0)
        # Only show stance breakdown when at least one directional vote exists
        if sup or skp:
            stance_str = f" — stance: {sup} support / {skp} skeptical / {neu} neutral"
        else:
            stance_str = ""
        lines.append(
            f"  - {theme}: {info['banks']} banks / {info['pdfs']} PDFs "
            f"({srcs_str}){stance_str}"
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
        if a.theme_stances:
            entry["theme_stances"] = [asdict(ts) for ts in a.theme_stances]
        compact.append(entry)
    return json.dumps(compact, indent=1)


def build_pulse_context(
    analyses: list[PdfAnalysis],
    use_prev_context: bool = True,
) -> dict:
    """Assemble all the inputs the synthesis prompts need, without calling any LLM.

    Returned dict has every key DRAFT_USER and AUDIT_USER expect plus the
    structured prompt bodies themselves so the routine agent can apply them
    directly. Used by the HTTP API endpoint that feeds the Opus routine.

    The keys map 1:1 to the fields used inside synthesize_daily_pulse below —
    when that function evolves, mirror changes here.
    """
    import pytz
    from config import settings as _settings

    today = date.today().isoformat()
    try:
        tz = pytz.timezone(_settings.timezone)
        now_local = datetime.now(tz)
        day_of_week = now_local.strftime("%A")
        today_label = f"{today} ({day_of_week})"
        now_label = now_local.strftime("%H:%M %Z")
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
            "not 'today's move.' Phrase price references as 'as of Friday's close' "
            "or 'heading into Monday.' Crypto trades 24/7 so BTC/ETH commentary is fine, "
            "but weekend volumes are thin."
        )

    analyses_json = _analyses_to_json(analyses)
    market_snapshot = fetch_market_snapshot()
    news_snapshot = fetch_news_snapshot(since_hours=48, limit=15)
    earnings_calendar = fetch_earnings_calendar(days_ahead=7)
    economic_calendar = fetch_economic_calendar(days_ahead=7)

    ticker_map = _build_ticker_map(analyses)
    if ticker_map:
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

    theme_map = _classify_themes(analyses)
    theme_coverage_block = _format_theme_coverage(theme_map)

    # Each pulse is fully standalone now. We no longer compute prev-pulse
    # diff context — the corresponding ctx fields (prev_pulse_block,
    # audit_prev_block) and prompt placeholders ({prev_pulse},
    # {prev_pulse_themes}) have been removed from DRAFT_USER and AUDIT_USER.
    # The use_prev_context parameter is retained for API compatibility but
    # has no effect on output.

    if market_status_note:
        market_snapshot = market_snapshot + market_status_note

    session_status = "closed (weekend)" if is_weekend else (
        "market hours — intraday" if "9:30" <= now_label[:5] < "16:00"
        else "pre-market or after-hours"
    )

    return {
        "today": today,
        "today_label": today_label,
        "now_label": now_label,
        "is_weekend": is_weekend,
        "session_status": session_status,
        "pdf_count": len(analyses),
        "analyses_json": analyses_json,
        "market_snapshot": market_snapshot,
        "news_snapshot": news_snapshot,
        "earnings_calendar": earnings_calendar,
        "economic_calendar": economic_calendar,
        "ticker_block": ticker_block,
        "theme_coverage": theme_coverage_block,
        # Structured form of the same theme aggregation rendered in
        # `theme_coverage`. Routine adjudication step needs this to rank
        # themes, filter per-theme evidence, and emit the stance_counts
        # field (which must match these pre-aggregated counts exactly per
        # the adjudication lint rules).
        "theme_map": theme_map,
    }


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
