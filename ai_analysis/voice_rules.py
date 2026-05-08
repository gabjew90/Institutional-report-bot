"""Single source of truth for banned voice patterns.

Both the LLM prompts (AUDIT_SYSTEM via interpolation in prompts.py) and
the post-AUDIT linter (scripts/pulse_lint.py) import from here.

Updating a banned pattern in one place propagates to:
  1. The AUDIT prompt's voice rules section (prompts.py composes the block at module load).
  2. The deterministic regex linter that runs after AUDIT (pulse_lint.py).

No drift between what the prompts ask for and what the linter checks.
"""
import re

# === Banned content ===

BANNED_PUNCTUATION = ['—', ';']

BANNED_FILLER_PHRASES = [
    "it's worth noting",
    "importantly",
    "notably",
    "interestingly",
    "moreover",
    "furthermore",
    "meanwhile",
    "that said",
    "of course",
]

BANNED_AI_CLICHE_VERBS = [
    "delve", "delves", "delved", "delving",
    "navigate",
    # "leverage" as a verb is hard to disambiguate without context — skip in lint
]

BANNED_AI_CLICHE_ADJECTIVES = ["robust"]

BANNED_HEDGING_WEASELS = [
    "could potentially",
    "may or may not",
    "it remains to be seen",
]

BANNED_WRAPUP_SENTENCES = [
    "Overall,",
    "In summary",
    "All told",
    "At the end of the day",
]

BANNED_AI_TELLS = [
    "deep dive",
    "unpack",
    "double-click",
    "in this rapidly-evolving landscape",
    "stakeholders",
]

BANNED_META_NARRATION = [
    "cross-bank consensus",
    "research suggests",
    "the corpus shows",
    "multiple banks converge",
]

# Source-prefix story-connector pattern: "[Bank] [verb]s that..."
SOURCE_PREFIX_BANKS = [
    "Goldman", "JPMorgan", "JPM", "Citi", "BofA", "UBS", "RBC",
    "Barclays", "Mizuho", "TME", "Market Ear", "ANZ", "ING",
    "Crédit Agricole", "Credit Agricole", "Morgan Stanley",
    "Deutsche Bank", "Bernstein", "Hartnett", "TS Lombard", "SEB",
    "Wells Fargo", "Nomura", "MUFG", "Rabobank",
]

SOURCE_PREFIX_VERBS = [
    "says", "said", "notes", "noted", "flags", "flagged",
    "adds", "added", "argues", "argued", "observes", "observed",
    "leans on", "keeps hammering", "pushes", "points to",
    "thinks", "sees", "writes", "reports", "reported", "considers",
]

# Jargon terms that MUST be translated to plain English on first use (or
# in the same paragraph). The audience is a self-directed US options/crypto
# trader — smart but NOT a finance professional. They read the WSJ, not
# institutional research. If the term appears in a pulse without a plain
# equivalent in the same paragraph, the reader stalls.
#
# The mapping is `jargon -> plain_translation`. AUDIT's voice scrub walks
# every paragraph; if a key appears without translation context nearby,
# AUDIT rewrites either using the value verbatim or restructures the
# sentence to drop the jargon entirely.
JARGON_WITH_TRANSLATIONS: dict[str, str] = {
    "duration": "long-dated bonds — the longer the maturity, the bigger the price move when yields shift",
    "term structure": "what the market expects rates to do over time",
    "term curve": "what the market expects rates to do over time",
    "breakevens": "the inflation rate priced into the bond market",
    "convexity": "leverage that pays off bigger as the price moves further in your favor",
    "delta one": "a position that moves dollar-for-dollar with the underlying",
    "bear-flatten": "short-term yields rising faster than long-term",
    "bear-steepen": "long-term yields rising faster than short-term",
    "fixed-rate receiver": "a bet that rates fall (you receive a fixed rate vs paying floating)",
    "carry": "what you earn just holding the position when nothing changes",
    "rate differential": "the gap between two countries' interest rates — drives the currency",
    "front-end issuance": "more short-term Treasury supply",
    "long-end": "30-year Treasuries",
    "short-end": "2-year Treasuries",
    "vol surface": "the option market's pricing of risk across strikes and expiries",
    "skew": "puts costing more than calls (or vice versa) — a sentiment signal",
    "gamma": "how fast the option's delta changes — short gamma means dealers buy strength and sell weakness",
    "basis": "the gap between cash and futures prices",
    "inversion": "short-term yields higher than long-term — historically a recession signal",
    "products draw": "refined fuel inventories falling fast",
    "products tightness": "refined fuel inventories falling fast",
    "selective single-name": "specific stock picks vs the broad sector",
    "tactically short": "betting against in the near term",
    "tactically long": "betting for in the near term",
    "prime brokerage flows": "what hedge funds are doing",
    "coupon supply": "new Treasury bonds being auctioned",
    "issuance": "new supply of bonds (or stock)",
    "NII": "interest income banks earn from loans",
    "bps": "hundredths of a percent",
    "CTAs": "trend-following computer funds that buy when markets rise and sell when they fall",
    "RSI 70": "the market is technically overheated, like a rubber band stretched too far",
    "short gamma": "dealers are on the hook to buy more the higher the market goes",
    "term structure normalized": "the panic has faded",
    "skew catching a bid": "traders are paying more for downside protection",
}


# === Helpers for prompt interpolation ===

def compose_audit_voice_block() -> str:
    """Build the AUDIT_SYSTEM voice rules section from the constants above.

    Called at module load by prompts.py; the returned string is interpolated
    into AUDIT_SYSTEM. If a banned phrase changes here, the prompt
    automatically reflects it on next module load (i.e. next routine fire).
    """
    def quoted_list(items):
        return ', '.join(f'"{p}"' for p in items)

    parts = [
        "**Banned punctuation (rewrite on sight):**",
        '- NO em-dashes (—). Use commas, periods, parentheses, or "but/and" instead.',
        '- NO semicolons (;). Break into two sentences or use "and"/"but".',
        '- NO subheadings or bolded labels INSIDE an insight body (no "**The Setup:**", "**Key data:**", "**Bottom line:**", "**Trade Implication:**", "**Hint:**"). Only the italicized one-line punchline at the top of an INSIGHT is structural.',
        "",
        "**Banned vocabulary (rewrite on sight):**",
        f"- Filler phrases: {quoted_list(BANNED_FILLER_PHRASES)}.",
        f"- AI-cliche verbs: {quoted_list(BANNED_AI_CLICHE_VERBS)}. Use plain alternatives.",
        f"- AI-cliche adjectives: {quoted_list(BANNED_AI_CLICHE_ADJECTIVES)}. Use \"strong\", \"solid\", \"well-supported\".",
        f"- Hedging weasels: {quoted_list(BANNED_HEDGING_WEASELS)}.",
        f"- Wrap-up sentences: {quoted_list(BANNED_WRAPUP_SENTENCES)}.",
        f"- Other AI-tells: {quoted_list(BANNED_AI_TELLS)}.",
        f"- Meta-narration: {quoted_list(BANNED_META_NARRATION)}. State views directly without commenting on the corpus.",
        "- Heuristic: if a phrase sounds like ChatGPT writing a LinkedIn post, rewrite it.",
        "",
        "**Source-prefix story-connectors (rewrite on sight):**",
        f"For any sentence opening with a bank name from {{{', '.join(SOURCE_PREFIX_BANKS[:6])}, ...}} followed by a generic verb ({', '.join(SOURCE_PREFIX_VERBS[:6])}, ...), rewrite. Either move the attribution to a parenthetical at sentence end, or strip the attribution entirely if a specific number/level isn't being attributed. The bank name should appear ONLY when paired with a specific data point or call.",
        "",
        "**Plain-English jargon scrub (BINDING — most-cut feedback from readers).** The audience is a self-directed US options/crypto trader, smart but NOT a finance professional. Every technical term below MUST be translated in plain English in the SAME paragraph it appears (parenthetical or inline rephrase). If the draft uses one of these without a translation present in the same paragraph, REWRITE the sentence — either embed the translation, replace the term with the plain equivalent, or restructure to drop the term entirely.",
        "",
        "Walk every paragraph in INSIGHTS bodies and RECAP. For each jargon term you find, ensure a translation is present nearby. The full term-to-translation map (use the right column verbatim or as a guide for inline rewriting):",
        "",
    ]
    for term, translation in JARGON_WITH_TRANSLATIONS.items():
        parts.append(f'- **{term}** → "{translation}"')
    parts.extend([
        "",
        "Goal: a smart 28-year-old crypto trader reading the pulse on a phone should understand every sentence on first read. If they need to look up a term, you've failed. The jargon scrub is non-negotiable.",
    ])
    return '\n'.join(parts)


# === Helpers for the linter ===

def compose_lint_patterns() -> list[tuple[str, str]]:
    """Return the regex patterns the post-AUDIT linter uses.

    Each entry: (regex_pattern, kind_label). Patterns are designed for
    re.IGNORECASE matching against the final pulse markdown (with fenced
    code blocks stripped first).

    The linter (scripts/pulse_lint.py) calls this and iterates the patterns.
    """
    patterns: list[tuple[str, str]] = []

    # Punctuation
    patterns.append((r'—', 'em-dash'))
    patterns.append((r';', 'semicolon'))

    # Phrase-level patterns — exact-match with optional trailing punctuation
    def word_pat(phrase: str) -> str:
        # \b boundaries — escape the phrase so commas/colons in entries don't break
        return r'\b' + re.escape(phrase) + r'\b'

    for phrase in BANNED_FILLER_PHRASES:
        patterns.append((word_pat(phrase), 'filler'))
    for v in BANNED_AI_CLICHE_VERBS:
        patterns.append((word_pat(v), 'AI-cliche-verb'))
    for adj in BANNED_AI_CLICHE_ADJECTIVES:
        patterns.append((word_pat(adj), 'AI-cliche-adj'))
    for h in BANNED_HEDGING_WEASELS:
        patterns.append((word_pat(h), 'hedge'))
    for w in BANNED_WRAPUP_SENTENCES:
        # Wrap-ups often appear at sentence start with comma/period — match leading boundary
        patterns.append((r'\b' + re.escape(w), 'wrap-up'))
    for tell in BANNED_AI_TELLS:
        patterns.append((word_pat(tell), 'AI-tell'))
    for m in BANNED_META_NARRATION:
        patterns.append((word_pat(m), 'meta-narration'))

    # Source-prefix story-connectors — single composite pattern
    banks_alt = '|'.join(re.escape(b) for b in SOURCE_PREFIX_BANKS)
    verbs_alt = '|'.join(re.escape(v) for v in SOURCE_PREFIX_VERBS)
    patterns.append((rf"\b({banks_alt})('s)?\s+({verbs_alt})\b", 'source-prefix'))

    return patterns


def compose_jargon_lint_patterns() -> list[tuple[str, str]]:
    """Soft-warning patterns for the jargon scan.

    Flags any bare use of a jargon term from JARGON_WITH_TRANSLATIONS.
    The linter classifies these as 'jargon-bare' (soft) — they don't
    block commits because the model is supposed to translate the term
    in the same paragraph, and the linter can't context-check that.
    Soft warnings let the routine surface jargon hits for review.
    """
    patterns: list[tuple[str, str]] = []
    for term in JARGON_WITH_TRANSLATIONS:
        # Word-boundary, case-insensitive applied at use site.
        # Multi-word terms get \b boundaries on both ends; single tokens
        # like "skew" or "duration" need stricter boundaries to avoid
        # matching inside other words.
        patterns.append((r'\b' + re.escape(term) + r'\b', 'jargon-bare'))
    return patterns
