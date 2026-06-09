"""Smoke test: 2026-06-09 end-to-end review batch — five shipments.

Background: a four-subagent end-to-end review of the pulse pipeline
(triggered by the user noticing AI capex at 9% of primary corpus
taking 40% of INSIGHTS slots) converged on five fixes:

  A. Conviction + source-diversity weighting in theme ranking.
     Ranking was pure bank-count: a 5-bank NEUTRAL consensus outranked
     a 1-bank HIGH-conviction call, and Goldman's 36% corpus share
     dominated narrative weight via repetition. Now: high_conviction
     (count of high-conviction directional stances) is the secondary
     sort key, top_source_share (concentration) is tertiary; the
     theme_coverage rows render both; _analyses_to_json adds a
     HIGH_CONVICTION summary per entry; the synthesis prompt tells
     DRAFT to lead with them.

  B. Cross-slot dedup. theme_map tracks per-theme instruments;
     _format_theme_coverage renders a CROSS-SLOT INSTRUMENT OVERLAP
     block when top themes share instruments; pulse_lint gets
     slot-lean-overlap (soft) catching two slots closing on the same
     ticker (2026-06-09: slots #1 + #5 both closed Long $MU).

  C. Live Treasury yields. market_data fetches ^FVX/^TNX/^TYX (Yahoo
     yield indices, quoted at yield*10), renders 5Y/10Y/30Y + 30Y-10Y
     spread in the snapshot. The pulse discusses yields constantly but
     only had $TLT as a proxy.

  D. Earnings whitelist expanded from 20 to ~45 tickers covering
     high-IV options movers (CRWD, PLTR, COIN, SMCI, AMD, AVGO...).

  E. Binding close-style: the `close in:` annotation is now MANDATORY
     with a literal last-paragraph compliance check + a _DRAFT NOTES
     escape hatch. QC found 3-4 of 5 slots ignored the steer on four
     consecutive pulses.
"""

import inspect
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# =====================================================================
# A — conviction + source-diversity weighting
# =====================================================================

def _identity_cluster(clustering_input):
    """Patch for report.synthesizer.cluster_themes — identity mapping,
    no Gemini embedding API calls. Mirrors the clusterer's documented
    API-failure degradation mode (no merging)."""
    return {tag: tag for tag in clustering_input}, []


def _no_discovery(contextual_triples, covered_labels):
    """Patch for discover_uncovered_clusters — no Phase B promotion."""
    return [], []


def _build_analyses_for_theme_map():
    """Two synthetic themes engineered to invert under the new ranking:
      - 'broad neutral theme': 4 banks, all neutral, 9 stance-touches,
        6 of them from Goldman (concentrated, no conviction)
      - 'sharp conviction theme': 4 banks, 2 high-conviction directional
        stances, 4 touches from 4 distinct banks (diverse, conviction)
    Old ranking (banks, pdfs): broad wins (same banks, more pdfs).
    New ranking (banks, high_conviction, concentration): sharp wins.
    """
    from ai_analysis.models import PdfAnalysis, ThemeStance

    analyses = []

    def mk(source, theme, stance, conviction, n=1, instruments=None):
        for i in range(n):
            analyses.append(PdfAnalysis(
                pdf_file_id=len(analyses) + 1,
                file_name=f"{source}_{theme}_{i}.pdf".replace(" ", "_"),
                source=source,
                title=f"{source} note {theme} {i}",
                report_type="research",
                priority="HIGH",
                key_insights=[f"{theme} insight"],
                theme_stances=[ThemeStance(
                    theme=theme,
                    stance=stance,
                    conviction=conviction,
                    key_argument=f"{source} argues {theme} #{i}",
                    evidence=f"verbatim evidence {source} {i}",
                    primary_instruments=instruments or [],
                )],
            ))

    # Broad neutral theme — Goldman-concentrated
    mk("Goldman Sachs", "broad neutral theme", "neutral", "medium", n=6)
    mk("JPMorgan", "broad neutral theme", "neutral", "medium")
    mk("Citi", "broad neutral theme", "neutral", "low")
    mk("UBS", "broad neutral theme", "neutral", "medium")
    # Sharp conviction theme — diverse + high conviction
    mk("Barclays", "sharp conviction theme", "supportive", "high",
       instruments=["MU", "SMH"])
    mk("Deutsche Bank", "sharp conviction theme", "supportive", "high",
       instruments=["MU"])
    mk("Morgan Stanley", "sharp conviction theme", "skeptical", "medium")
    mk("Nordea", "sharp conviction theme", "neutral", "medium")
    return analyses


def _classify_offline(analyses):
    """Run _classify_themes with the Gemini-backed clusterer patched out."""
    from report import synthesizer
    with patch("report.synthesizer.cluster_themes",
               side_effect=_identity_cluster), \
         patch("report.synthesizer.discover_uncovered_clusters",
               side_effect=_no_discovery):
        return synthesizer._classify_themes(analyses)


def test_theme_map_tracks_conviction_and_concentration():
    analyses = _build_analyses_for_theme_map()
    theme_map = _classify_offline(analyses)

    broad = theme_map.get("broad neutral theme")
    sharp = theme_map.get("sharp conviction theme")
    assert broad and sharp, (
        f"both synthetic themes must survive clustering, got: "
        f"{list(theme_map)}"
    )
    assert sharp["high_conviction"] == 2, sharp
    assert broad["high_conviction"] == 0, broad
    # Goldman wrote 6 of broad's 9 stance-PDF touches
    assert broad["top_source_share"] >= 0.6, broad
    assert broad["top_source"] == "Goldman Sachs", broad
    # Sharp is diverse — top source share 1/4
    assert sharp["top_source_share"] <= 0.34, sharp
    # Instruments pooled
    assert "MU" in sharp.get("instruments", []), sharp
    _ok("theme_map carries high_conviction + top_source_share + "
        "top_source + instruments")


def test_ranking_prefers_conviction_and_diversity_at_equal_banks():
    """Both themes have 4 banks. Old ranking would put broad first
    (more PDFs). New ranking puts sharp first (2 high-conviction, low
    concentration)."""
    analyses = _build_analyses_for_theme_map()
    theme_map = _classify_offline(analyses)
    ranked = sorted(
        theme_map.items(),
        key=lambda kv: (
            -kv[1]["banks"],
            -kv[1].get("high_conviction", 0),
            kv[1].get("top_source_share", 0.0),
            -kv[1]["pdfs"],
            kv[0],
        ),
    )
    order = [t for t, _ in ranked]
    assert order.index("sharp conviction theme") < order.index("broad neutral theme"), (
        f"at equal bank counts, conviction + diversity must outrank "
        f"PDF volume; got order: {order}"
    )
    _ok("ranking: 2-high-conviction diverse theme outranks Goldman-"
        "concentrated neutral theme at equal bank count")


def test_theme_coverage_renders_conviction_and_concentration():
    from report import synthesizer
    analyses = _build_analyses_for_theme_map()
    theme_map = _classify_offline(analyses)
    text = synthesizer._format_theme_coverage(theme_map)
    assert "HIGH-CONVICTION stance" in text, (
        "theme_coverage must render the high-conviction marker"
    )
    assert "SOURCE-CONCENTRATED" in text, (
        "theme_coverage must render the source-concentration warning "
        "for the Goldman-heavy theme"
    )
    assert "Goldman Sachs" in text
    _ok("theme_coverage renders HIGH-CONVICTION + SOURCE-CONCENTRATED "
        "markers")


def test_analyses_json_has_high_conviction_summary():
    from report import synthesizer
    from ai_analysis.models import PdfAnalysis, MarketMover, TradeIdea
    a = PdfAnalysis(
        pdf_file_id=1, file_name="t.pdf",
        source="Goldman Sachs", title="t", report_type="research",
        priority="HIGH", key_insights=["x"],
        market_movers=[
            MarketMover(ticker="NVDA", action="downgrade", rating="Sell",
                        price_target="$100", rationale="r",
                        conviction="high"),
            MarketMover(ticker="AAPL", action="reiterate", rating="Buy",
                        price_target="$250", rationale="r",
                        conviction="medium"),
        ],
        trade_ideas=[
            TradeIdea(description="Long MU into memory tightness",
                      rationale="r", risk="x", conviction="high",
                      time_horizon="1-3mo"),
        ],
    )
    import json
    out = json.loads(synthesizer._analyses_to_json([a]))
    entry = out[0]
    assert "HIGH_CONVICTION" in entry, (
        "entries with high-conviction items must carry the summary field"
    )
    hc = entry["HIGH_CONVICTION"]
    assert any("NVDA" in item for item in hc), hc
    assert any("Long MU" in item for item in hc), hc
    assert any("[1-3mo]" in item for item in hc), (
        f"trade-idea summaries should carry time_horizon, got {hc}"
    )
    # The MEDIUM-conviction AAPL reiterate must NOT be in the summary
    assert not any("AAPL" in item for item in hc), hc
    _ok("_analyses_to_json: HIGH_CONVICTION summary lists high items "
        "only, with horizon")


def test_synthesis_prompt_has_conviction_weighting_clause():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    assert "HIGH_CONVICTION" in src, (
        "synthesis prompt must reference the HIGH_CONVICTION field"
    )
    assert "Lead with these" in src or "lead with" in src.lower()
    assert "SOURCE-CONCENTRATED" in src, (
        "synthesis prompt must explain the source-concentration tag"
    )
    assert "one house view" in src
    _ok("synthesis prompt: conviction-weighting + concentration clauses "
        "present")


# =====================================================================
# B — cross-slot dedup
# =====================================================================

def test_theme_coverage_renders_instrument_overlap_block():
    """Two top themes sharing $MU must produce the CROSS-SLOT
    INSTRUMENT OVERLAP alert."""
    from report import synthesizer
    theme_map = {
        "ai capex": {
            "banks": 5, "pdfs": 8, "sources": ["A", "B", "C", "D", "E"],
            "supportive": 3, "skeptical": 0, "neutral": 2,
            "high_conviction": 1, "top_source_share": 0.3,
            "top_source": "A", "instruments": ["MU", "NVDA", "SMH"],
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
        },
        "semis positioning": {
            "banks": 4, "pdfs": 4, "sources": ["A", "B", "C", "D"],
            "supportive": 2, "skeptical": 1, "neutral": 1,
            "high_conviction": 0, "top_source_share": 0.25,
            "top_source": "B", "instruments": ["MU", "SOXX"],
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
        },
        "hormuz oil": {
            "banks": 6, "pdfs": 9, "sources": ["A", "B", "C", "D", "E", "F"],
            "supportive": 1, "skeptical": 0, "neutral": 5,
            "high_conviction": 0, "top_source_share": 0.3,
            "top_source": "A", "instruments": ["BNO", "USO"],
            "discovered": False, "non_bank_only": False,
            "sibling_canonicals": [],
        },
    }
    text = synthesizer._format_theme_coverage(theme_map)
    assert "CROSS-SLOT INSTRUMENT OVERLAP" in text, (
        "shared-instrument themes must trigger the overlap alert block"
    )
    assert "$MU" in text
    assert "ai capex" in text and "semis positioning" in text
    # Hormuz shares nothing — must not appear in the overlap section
    overlap_section = text[text.find("CROSS-SLOT INSTRUMENT OVERLAP"):]
    assert "hormuz" not in overlap_section.lower().split("share:")[0] or \
           "hormuz" not in overlap_section.lower(), (
        "non-overlapping theme must not be flagged"
    )
    _ok("theme_coverage: CROSS-SLOT INSTRUMENT OVERLAP block fires on "
        "shared $MU, skips non-overlapping theme")


def test_lint_close_lean_overlap_detector():
    """The exact 2026-06-09 shape: two slots closing Long $MU."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_lint
    md = """# Test pulse

## 1. RECAP

Stuff happened.

## 2. INSIGHTS & ALPHA

### The AI capex supercycle survives

*Punchline.*

- bullet one
- bullet two

Mechanism paragraph explaining things at length here.

Long $MU into the AI build-out, the memory cycle is where the supply tightness is most visible.

### Semis are in a positioning regime

*Punchline two.*

- bullet

The mechanic is simple and explained here.

Long $MU calls into the memory-supply tightness, avoid $SOXX until the forced-flow overhang clears.

### Hormuz fragility

*Punchline three.*

- bullet

Setup paragraph.

Long $BNO into the next Hormuz headline, sized small.

## 3. WHAT TO WATCH

- stuff
"""
    issues = pulse_lint._check_slot_lean_overlap(md)
    mu_issues = [i for i in issues if "$MU" in i["snippet"]]
    assert len(mu_issues) == 1, (
        f"expected exactly one $MU lean-overlap issue, got: {issues}"
    )
    assert mu_issues[0]["kind"] == "slot-lean-overlap"
    # $BNO appears in only one close — must not be flagged
    assert not any("$BNO" in i["snippet"] for i in issues), issues
    # $SOXX appears via 'avoid' which is not a lean verb — must not flag.
    # (it also appears only once, so double-safe)
    assert not any("$SOXX" in i["snippet"] for i in issues), issues
    _ok("lint: slot-lean-overlap fires on Long $MU in 2 closes, "
        "ignores single-slot leans")


def test_lint_lean_overlap_is_soft():
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_lint
    assert "slot-lean-overlap" in pulse_lint.SOFT_ISSUE_KINDS, (
        "slot-lean-overlap must be SOFT (advisory) — SCRUB can't "
        "repartition trade leans, only DRAFT can"
    )
    _ok("slot-lean-overlap registered as soft issue")


def test_lint_lean_extractor_only_scans_last_paragraph():
    """Evidence mentions of a ticker mid-body must NOT count as a lean."""
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import pulse_lint
    slot = """### Theme title

*Punchline.*

- UBS reads $NVDA's $124B purchase commitment as long $MU support evidence

Mechanism paragraph that says you could buy $AAPL here in theory.

Long $TLT into the print, taking it off if cool.
"""
    leans = pulse_lint._extract_close_leans(slot)
    assert leans == {"TLT"}, (
        f"only the LAST paragraph's lean counts; mid-body 'long $MU' "
        f"evidence + 'buy $AAPL' prose must not be extracted, got {leans}"
    )
    _ok("lean extractor scans last paragraph only (no mid-body false "
        "positives)")


# =====================================================================
# C — Treasury yields
# =====================================================================

def test_treasury_yields_fetch_and_scaling():
    from report import market_data

    def fake_yahoo(symbol):
        # Yahoo yield indices quote yield*10: ^TNX 44.4 = 4.44%
        return {
            "^FVX": {"last_price": 41.2, "prev_close": 41.0},
            "^TNX": {"last_price": 44.4, "prev_close": 44.1},
            "^TYX": {"last_price": 49.8, "prev_close": 50.1},
        }.get(symbol)

    with patch("report.market_data._fetch_yahoo_extended_hours",
               side_effect=fake_yahoo):
        yields = market_data._fetch_treasury_yields()

    assert yields["10Y"]["yield_pct"] == 4.44, yields
    assert yields["5Y"]["yield_pct"] == 4.12, yields
    assert yields["30Y"]["yield_pct"] == 4.98, yields
    # change_bps: (44.4 - 44.1) * 10 = +3.0 bps
    assert abs(yields["10Y"]["change_bps"] - 3.0) < 0.01, yields
    assert abs(yields["30Y"]["change_bps"] - (-3.0)) < 0.01, yields
    _ok("treasury yields: /10 scaling + bps change computed correctly")


def test_market_snapshot_renders_yields_block():
    from report import market_data

    def fake_yahoo(symbol):
        return {
            "^FVX": {"last_price": 41.2, "prev_close": 41.0},
            "^TNX": {"last_price": 44.4, "prev_close": 44.1},
            "^TYX": {"last_price": 49.8, "prev_close": 50.1},
        }.get(symbol)

    with patch("report.market_data._fetch_yahoo_extended_hours",
               side_effect=fake_yahoo), \
         patch("report.market_data._fetch_crypto", return_value={}), \
         patch("report.market_data._fetch_traditional_markets",
               return_value={}):
        snap = market_data.fetch_market_snapshot()

    assert "US Treasury yields" in snap, snap
    assert "10Y: 4.44%" in snap, snap
    assert "30Y: 4.98%" in snap, snap
    # 30Y-10Y spread = (4.98 - 4.44) * 100 = +54 bps
    assert "30Y-10Y spread: +54 bps" in snap, snap
    _ok("market snapshot renders yields block + 30Y-10Y spread")


def test_yields_failure_degrades_gracefully():
    """Total Yahoo failure → no yields block, no crash, snapshot still
    renders other sections."""
    from report import market_data
    with patch("report.market_data._fetch_yahoo_extended_hours",
               return_value=None), \
         patch("report.market_data._fetch_crypto",
               return_value={"BTC": {"price": 62000.0,
                                     "change_utc_day": 1.0}}), \
         patch("report.market_data._fetch_traditional_markets",
               return_value={}):
        snap = market_data.fetch_market_snapshot()
    assert "US Treasury yields" not in snap
    assert "BTC" in snap
    _ok("yields fetch failure degrades gracefully (no block, no crash)")


# =====================================================================
# D — earnings whitelist
# =====================================================================

def test_earnings_whitelist_expanded():
    from report import news_data
    required_new = ["CRWD", "PLTR", "COIN", "SMCI", "AMD", "AVGO",
                    "QCOM", "MU", "HOOD", "MSTR", "LLY", "COST"]
    missing = [t for t in required_new if t not in news_data._MAJOR_TICKERS]
    assert not missing, f"missing from whitelist: {missing}"
    # Originals preserved
    for t in ("AAPL", "NVDA", "JPM", "TSM"):
        assert t in news_data._MAJOR_TICKERS
    assert len(news_data._MAJOR_TICKERS) >= 40, (
        f"expected ~45 tickers, got {len(news_data._MAJOR_TICKERS)}"
    )
    _ok(f"earnings whitelist expanded to "
        f"{len(news_data._MAJOR_TICKERS)} tickers incl high-IV movers")


# =====================================================================
# E — binding close-style
# =====================================================================

def test_close_style_annotation_is_mandatory():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    assert "MANDATORY, not advisory" in src, (
        "the close-style annotation must be marked MANDATORY"
    )
    assert "Compliance check before you ship each section" in src, (
        "prompt must include the literal last-paragraph compliance check"
    )
    # The compliance check names the verifiable shape per style
    assert "no time-bound" in src or "time-bound" in src
    assert "no numbered list" in src or "numbered list" in src
    assert "question mark" in src
    _ok("close-style: MANDATORY marker + literal compliance check")


def test_close_style_has_escape_hatch():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    assert "Escape hatch" in src, (
        "binding close-style needs the _DRAFT NOTES escape hatch so a "
        "genuinely-unfittable steer doesn't force a contrived close"
    )
    assert "_DRAFT NOTES" in src
    assert ("override WITHOUT a note is a compliance failure" in src
            or "An override WITHOUT a note" in src)
    _ok("close-style: _DRAFT NOTES escape hatch with note requirement")


def test_close_style_anchors_qc_dates():
    from ai_analysis import prompts
    src = inspect.getsource(prompts)
    # The rule must anchor the four consecutive QC observations
    assert "06-08" in src and "06-09" in src, (
        "rule should anchor the consecutive QC runs that found the "
        "steer ignored"
    )
    _ok("close-style: anchors the 4-consecutive-QC-runs failure pattern")


if __name__ == "__main__":
    print("=== 2026-06-09 pulse quality batch smoke ===")
    print("\n--- A: conviction + source-diversity ---")
    test_theme_map_tracks_conviction_and_concentration()
    test_ranking_prefers_conviction_and_diversity_at_equal_banks()
    test_theme_coverage_renders_conviction_and_concentration()
    test_analyses_json_has_high_conviction_summary()
    test_synthesis_prompt_has_conviction_weighting_clause()
    print("\n--- B: cross-slot dedup ---")
    test_theme_coverage_renders_instrument_overlap_block()
    test_lint_close_lean_overlap_detector()
    test_lint_lean_overlap_is_soft()
    test_lint_lean_extractor_only_scans_last_paragraph()
    print("\n--- C: treasury yields ---")
    test_treasury_yields_fetch_and_scaling()
    test_market_snapshot_renders_yields_block()
    test_yields_failure_degrades_gracefully()
    print("\n--- D: earnings whitelist ---")
    test_earnings_whitelist_expanded()
    print("\n--- E: binding close-style ---")
    test_close_style_annotation_is_mandatory()
    test_close_style_has_escape_hatch()
    test_close_style_anchors_qc_dates()
    print("\nALL 2026-06-09 BATCH SMOKE TESTS PASS")
