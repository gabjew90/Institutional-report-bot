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


@dataclass
class TradeIdea:
    description: str
    rationale: str
    risk: str
    conviction: str = ""  # high/medium/low
    time_horizon: str = ""  # intraday/swing/1-3mo/3-12mo/longer_term


@dataclass
class EntityMention:
    """An entity (company, crypto, index) referenced in the research.

    Used to build a reliable ticker lookup for cashtag formatting in synthesis.
    """
    name: str           # Full name (e.g., "Arista Networks", "Bitcoin")
    ticker: str         # Symbol (e.g., "ANET", "BTC"). Empty if no known ticker.
    asset_class: str    # stock/etf/crypto/index/fx/commodity/other


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
    pages_analyzed: int = 0
    total_pages: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    published_at: str | None = None  # Dropbox upload timestamp — when research landed
