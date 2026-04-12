"""Smart page selection for token-efficient PDF analysis.

Uses multi-signal scoring to select the most valuable pages for multimodal
analysis, reducing API costs ~75% while preserving high-value content.
"""

import re
import logging
from collections import Counter

from pdf_processing.models import PageText
from config import settings

log = logging.getLogger(__name__)

# Financial keyword categories with weights
MARKET_MOVING_KEYWORDS = {
    "upgrade", "downgrade", "price target", "overweight", "underweight",
    "buy", "sell", "outperform", "underperform", "conviction", "initiat",
    "reiterat", "strong buy", "neutral", "hold", "accumulate",
    "catalyst watch", "positive catalyst", "negative catalyst",
    "top call", "must read", "top pick", "key changes",
    "rating change", "tp change", "price target change",
    "coverage", "assuming coverage",
}

EARNINGS_KEYWORDS = {
    "eps", "revenue", "guidance", "beat", "miss", "surprise", "consensus",
    "estimate", "earnings", "quarter", "fiscal", "margin", "ebitda",
    "free cash flow", "fcf", "forward pe", "valuation",
    "earnings review", "earnings preview", "1q26", "2q26", "3q26", "4q26",
    "fy26", "fy27", "visible alpha", "adj. ebit", "organic growth",
    "dcf", "sotp", "results season", "reporting season",
}

MACRO_KEYWORDS = {
    "gdp", "cpi", "pce", "fed", "fomc", "rate cut", "rate hike",
    "inflation", "employment", "payroll", "unemployment", "yield curve",
    "treasury", "dollar", "dxy", "monetary policy", "fiscal policy",
    "soft landing", "recession", "quantitative",
    "ceasefire", "middle east", "iran", "hormuz", "geopolitical",
    "sanctions", "oil price", "brent", "crude", "natural gas",
    "ecb", "boj", "pboc", "rba", "rbnz", "central bank",
    "ppi", "ipca", "cgpi", "mpc", "nbp",
}

CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "eth", "ethereum", "defi", "staking", "on-chain",
    "spot etf", "crypto", "blockchain", "digital asset", "token",
    "halving", "mining", "web3", "altcoin", "solana", "sol",
}

TRADING_KEYWORDS = {
    "implied volatility", "iv", "options", "call", "put", "spread",
    "skew", "gamma", "delta", "vega", "theta", "positioning",
    "short interest", "flow", "dark pool", "open interest", "vix",
    "risk reversal", "straddle", "strangle", "iron condor",
    "v2x", "vol reset", "squeeze", "crowding", "crowded",
    "put spread", "hedging", "tail risk", "payoff",
    "momentum", "breakout", "200-day", "moving average", "rsi",
    "support", "resistance", "mean reversion",
}

STRUCTURAL_HEADERS = {
    "summary", "key takeaways", "conclusion", "recommendation",
    "investment thesis", "executive summary", "key findings",
    "overview", "highlights", "outlook", "forecast", "top picks",
    "action items", "key risks", "catalysts",
    "top call", "must read", "morning meeting", "today's morning meeting",
    "also published today", "key changes", "quick takes",
    "session outlook", "closing playbook", "what to watch",
    "premium research", "company comments", "industry comments",
    "what's priced in", "our view", "the bottom line",
}

DISCLAIMER_KEYWORDS = {
    "important disclosures", "this report is issued by", "legal disclaimer",
    "general disclosures", "analyst certification", "regulatory disclosures",
    "important information", "this document is for", "not for distribution",
    "conflict of interest", "required disclosures",
    "appendix a-1", "see appendix", "analyst affiliations",
    "rbc capital markets is the business name", "ubs does and seeks to do business",
    "this report has been prepared by", "investors should be aware",
    "past performance is not a guide", "not an offer to sell",
}

ALL_FINANCIAL_KEYWORDS = (
    MARKET_MOVING_KEYWORDS | EARNINGS_KEYWORDS | MACRO_KEYWORDS |
    CRYPTO_KEYWORDS | TRADING_KEYWORDS
)


def _keyword_density_score(text: str) -> float:
    """Score based on presence of high-value financial keywords."""
    text_lower = text.lower()
    if not text_lower.strip():
        return 0.0

    hit_count = 0
    for kw in ALL_FINANCIAL_KEYWORDS:
        hit_count += text_lower.count(kw)

    # Normalize by text length (per 1000 chars)
    density = hit_count / max(len(text_lower) / 1000, 1)
    return min(density / 5.0, 1.0)  # Cap at 1.0


def _structural_score(page: PageText, total_pages: int) -> float:
    """Score based on structural importance of the page."""
    text_lower = page.text.lower()

    # Page 1 is always important (executive summary / cover)
    if page.page_number == 0:
        return 0.9

    # Pages 2-3 often have key findings
    if page.page_number in (1, 2):
        return 0.6

    # Check for structural headers
    first_500 = text_lower[:500]
    for header in STRUCTURAL_HEADERS:
        if header in first_500:
            return 0.85

    # Disclaimer/disclosure pages (typically last pages)
    for kw in DISCLAIMER_KEYWORDS:
        if kw in text_lower:
            return 0.0  # Skip disclaimers

    # Last 3 pages are often disclaimers
    if page.page_number >= total_pages - 3:
        disclaimer_signals = sum(1 for kw in DISCLAIMER_KEYWORDS if kw in text_lower)
        if disclaimer_signals >= 1:
            return 0.0

    return 0.3  # Default middle score


def _visual_content_score(page: PageText) -> float:
    """Score based on charts, images, and tables (where multimodal adds most value)."""
    score = 0.0

    if page.has_tables:
        score += 0.5

    if page.has_images:
        # Multiple charts = high value
        if page.image_count >= 2:
            score += 0.5
        else:
            score += 0.3

        # But a single large image on an otherwise empty page is likely decorative
        if page.image_count == 1 and page.char_count < 100:
            return 0.05  # Likely a cover image

    return min(score, 1.0)


def _information_density_score(page: PageText) -> float:
    """Score based on information density of the text."""
    text = page.text.strip()
    if not text:
        return 0.0

    # Ratio of alphanumeric chars
    alnum = sum(1 for c in text if c.isalnum())
    alnum_ratio = alnum / max(len(text), 1)

    # Number density (financial data)
    numbers = len(re.findall(r'\d+\.?\d*%?', text))
    number_density = min(numbers / max(len(text) / 200, 1), 1.0)

    # Unique words (vocabulary diversity)
    words = text.lower().split()
    unique_ratio = len(set(words)) / max(len(words), 1)

    return (alnum_ratio * 0.3 + number_density * 0.4 + unique_ratio * 0.3)


def _novelty_score(page: PageText, previous_pages: list[PageText]) -> float:
    """Penalize pages too similar to already-selected pages."""
    if not previous_pages:
        return 1.0

    page_words = set(page.text.lower().split())
    if not page_words:
        return 0.0

    max_similarity = 0.0
    for prev in previous_pages:
        prev_words = set(prev.text.lower().split())
        if not prev_words:
            continue
        # Jaccard similarity
        intersection = len(page_words & prev_words)
        union = len(page_words | prev_words)
        similarity = intersection / max(union, 1)
        max_similarity = max(max_similarity, similarity)

    # Invert: high similarity = low novelty score
    return max(1.0 - max_similarity, 0.0)


def select_pages(
    pages: list[PageText],
    max_pages: int | None = None,
) -> list[int]:
    """Score and rank pages, returning indices of the most valuable pages.

    Weights:
    - Keyword density: 0.25
    - Structural importance: 0.25
    - Visual content: 0.30
    - Information density: 0.10
    - Novelty: 0.10
    """
    max_pages = max_pages or settings.max_pages_per_pdf
    total_pages = len(pages)

    if total_pages <= max_pages:
        # If PDF is short enough, use all pages
        return [p.page_number for p in pages]

    # Score each page
    scored: list[tuple[int, float]] = []
    for page in pages:
        kw = _keyword_density_score(page.text)
        struct = _structural_score(page, total_pages)
        visual = _visual_content_score(page)
        info = _information_density_score(page)

        # Skip pages scored 0 structurally (disclaimers)
        if struct == 0.0:
            continue

        # Composite score (without novelty, applied later)
        base_score = (kw * 0.25 + struct * 0.25 + visual * 0.30 + info * 0.10)
        scored.append((page.page_number, base_score))

    # Sort by base score descending
    scored.sort(key=lambda x: x[1], reverse=True)

    # Greedily select pages with novelty penalty
    selected: list[int] = []
    selected_pages_data: list[PageText] = []

    for page_num, base_score in scored:
        if len(selected) >= max_pages:
            break

        page = pages[page_num]
        novelty = _novelty_score(page, selected_pages_data)
        final_score = base_score * 0.90 + novelty * 0.10

        # Only include if score is above minimum threshold
        if final_score > 0.1:
            selected.append(page_num)
            selected_pages_data.append(page)

    # Sort selected pages in document order (preserves reading flow)
    selected.sort()

    log.info(
        f"Selected {len(selected)}/{total_pages} pages "
        f"(max {max_pages}, skipped {total_pages - len(scored)} disclaimer pages)"
    )
    return selected
