"""Dataclasses for AI analysis results."""

from dataclasses import dataclass, field


@dataclass
class TriageResult:
    priority: str  # "high", "medium", "low"
    report_type: str  # e.g. "equity_research", "macro", "crypto"
    key_tickers: list[str]
    summary: str
    source: str = ""  # Bank/publisher name as identified during triage
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class MarketMover:
    ticker: str
    action: str  # upgrade/downgrade/initiate/reiterate
    rating: str
    price_target: str
    rationale: str
    conviction: str = ""  # high/medium/low — contrarian + explicit "high conviction" = high


@dataclass
class SectorView:
    sector: str
    stance: str  # overweight/neutral/underweight
    rationale: str


@dataclass
class MacroIndicator:
    indicator: str
    reading: str
    interpretation: str
    # 2026-07-15 QC: the pulse shipped consensus CPI estimates (3.88%)
    # as the released print (3.5%). The qualifier is captured HERE or
    # never — synthesis cannot recover it. released | forecast | target.
    # Defaults empty for pre-existing rows; extraction prompt requires it.
    status: str = ""
    period: str = ""  # reference period ("2026-06", "Q2 2026"); "" if unstated


@dataclass
class TradeIdea:
    description: str
    rationale: str
    risk: str
    conviction: str = ""  # high/medium/low
    time_horizon: str = ""  # intraday/swing/1-3mo/3-12mo/longer_term
    # US-listed tickers the trade is ON (2026-07-29). market_movers,
    # entities_mentioned and theme_stances all expose tickers
    # structurally; trade_ideas buried them in `description`, so
    # "every trade idea on $NVDA" was unanswerable. Additive — the
    # description still carries the full prose structure.
    instruments: list[str] = field(default_factory=list)


@dataclass
class EntityMention:
    """An entity (company, crypto, index) referenced in the research.

    Used to build a reliable ticker lookup for cashtag formatting in synthesis.
    """
    name: str           # Full name (e.g., "Arista Networks", "Bitcoin")
    ticker: str         # Symbol (e.g., "ANET", "BTC"). Empty if no known ticker.
    asset_class: str    # stock/etf/crypto/index/fx/commodity/other


@dataclass
class KeyDataPoint:
    """A specific numeric data point extracted as a standalone unit.

    Synthesis weaves these in directly without re-mining prose for figures.
    Goal: every specific number a trader would care about — capex levels,
    yield breakouts, dissent counts, positioning percentiles — gets surfaced
    as a citeable unit, not buried in a key_insights paragraph.
    """
    figure: str         # The numeric value (e.g., "$751B", "+1.7% MoM", "8-4")
    metric: str         # What it measures (e.g., "2026 hyperscaler capex", "Retail Sales", "FOMC dissent count")
    source_bank: str    # Which bank cited this figure (e.g., "Goldman Sachs", "UBS")
    context: str = ""   # Brief context — change vs prior, vs estimate, percentile, as-of date
    # released | forecast | target | level — same qualifier discipline as
    # MacroIndicator.status (2026-07-15 estimate-as-actual QC failure).
    figure_status: str = ""
    # Verbatim quote from the source document containing the figure —
    # redesign sequencing step 2 (2026-08-27, claim-card spec §3). This
    # is the string ai_analysis/anchor_check.py verifies against the
    # extracted PDF text with normalized matching, making extraction
    # fidelity measurable instead of assumed. Empty on rows analyzed
    # before the field existed; the checker counts those separately
    # rather than as failures.
    anchor: str = ""


@dataclass
class TensionPoint:
    """A bull-vs-bear framing pulled from the research.

    Used by synthesis to construct the bull/bear paragraph in each INSIGHT
    without having to reconstruct the framing from risk_factors + cross_bank.
    Populate when the research explicitly frames both sides.
    """
    theme: str               # Short theme label (e.g., "AI capex", "Rate repricing")
    bull_case: str           # The optimistic read with specifics
    bear_case: str           # The risk / counter-thesis with specifics
    what_invalidates: str = ""  # Specific level/event that breaks the bull thesis


@dataclass
class ThemeStance:
    """The bank's directional view on a specific cross-bank theme.

    Replaces the prior `theme_tags: list[str]` with a structured per-theme
    stance + conviction + key argument. This is the input for downstream
    cross-bank theme adjudication (cluster by theme, count stances, weight
    by conviction, surface dissent).

    Empty list is acceptable — admin/wrapper PDFs and pure earnings recaps
    often have no directional theme view.
    """
    theme: str                          # Short canonical label (e.g., "Hormuz peace deal", "AI hyperscaler capex")
    stance: str = "neutral"             # supportive | skeptical | neutral
    conviction: str = "medium"          # high | medium | low
    key_argument: str = ""              # The bank's one-sentence reasoning for this stance
    primary_instruments: list[str] = field(default_factory=list)  # Tickers/instruments most affected
    vs_consensus: str = ""              # contrarian | with_consensus | out_of_consensus | "" (unspecified)
    evidence: str = ""                  # Verbatim ≤15-word phrase from the report that grounds the stance.
                                        # Anti-hallucination anchor — empty if no clear quote exists.


@dataclass
class PdfAnalysis:
    pdf_file_id: int
    file_name: str
    source: str
    title: str
    report_type: str
    priority: str
    key_insights: list[str] = field(default_factory=list)
    market_movers: list[MarketMover] = field(default_factory=list)
    sector_views: list[SectorView] = field(default_factory=list)
    earnings_insights: list[str] = field(default_factory=list)
    macro_indicators: list[MacroIndicator] = field(default_factory=list)
    crypto_views: list[str] = field(default_factory=list)
    trade_ideas: list[TradeIdea] = field(default_factory=list)
    risk_factors: list[str] = field(default_factory=list)
    charts_described: list[str] = field(default_factory=list)
    vol_and_positioning: list[str] = field(default_factory=list)
    geopolitical: list[str] = field(default_factory=list)
    cross_bank_references: list[str] = field(default_factory=list)
    entities_mentioned: list[EntityMention] = field(default_factory=list)
    key_data_points: list[KeyDataPoint] = field(default_factory=list)
    tension_points: list[TensionPoint] = field(default_factory=list)
    # Bank's directional view on 1-3 cross-bank themes (replaces prior theme_tags).
    # Each entry: theme + stance (supportive/skeptical/neutral) + conviction + key_argument.
    # Powers organic cross-bank theme aggregation AND future adjudication
    # (consensus/dissent/conviction-weighted). Empty list when the report is
    # admin/wrapper or has no directional theme view.
    theme_stances: list[ThemeStance] = field(default_factory=list)
    # Topical mentions throughout the report — names, events, regimes, and
    # actors discussed contextually but NOT promoted to theme_stances.
    # Powers corpus-level theme discovery: topics mentioned in many PDFs as
    # context (Iran strikes inside risk_factors, Hormuz inside macro
    # interpretation, etc.) but never surfaced as a primary theme by any
    # single bank get clustered downstream and promoted to candidate themes.
    # Each entry is a 3-12 word phrase specific enough to embed without
    # ambiguity ("US strikes on Iranian targets", not bare "Iran").
    contextual_mentions: list[str] = field(default_factory=list)
    # Anchor-verification stats from ai_analysis/anchor_check.py —
    # {total, matched, missed, empty, too_short, match_rate, misses}.
    # Warn-only measurement (redesign step 2): rides into analysis_json
    # via asdict so QC and the shadow pilot can read fidelity per
    # document. Empty dict on rows analyzed before the field existed.
    anchor_check: dict = field(default_factory=dict)
    pages_analyzed: int = 0
    total_pages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    published_at: str | None = None  # Dropbox upload timestamp — when research landed
