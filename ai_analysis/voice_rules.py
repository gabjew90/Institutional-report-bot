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
    # Default-openers that became template tells — phrasings the model
    # latched onto as its single positioning-sentence or counter-argument
    # opener and ran every theme on it. Each became visible repetition-
    # as-AI-tell. Banned to force genuine variation; the model must pick
    # a different transition each theme.
    "the cleanest read",
    "the cleanest expression",
    "cleanest read",
    "cleanest expression",
    # Movement-3 counter-argument template tells — the model defaults to
    # one of these and uses it every theme as the counter-argument opener
    "the pushback we would anticipate",
    "the pushback we'd anticipate",
    "pushback we would anticipate",
    "pushback we'd anticipate",
    # Movement-4 defense template tells — same failure mode. The prompt
    # used to list these as defense-transition examples; model picks
    # one as default. Banned to force theme-specific defenses.
    "that risk is real, but",
    "even granting that",
    "where we disagree",
    # Stock-explanatory transition tells — the writer uses these to
    # introduce a mechanism explanation, but they're flat banker-newsletter
    # filler. SCRUB caught these in the 2026-05-08 test run; LINT missed
    # them.
    "the mechanism is straightforward",
    "the mechanism here is",
    "the dynamic is straightforward",
    "the dynamic here is",
    # 2026-06-04 QC review caught "The mechanics are uncomfortable
    # when laid out" in INSIGHTS #2 prose (AI capex slot). Same family
    # as "the mechanism is straightforward" — banker-tic that signals
    # "I'm about to explain something" without committing to the
    # explanation. Both singular ("mechanic is") and plural ("mechanics
    # are") forms; both "uncomfortable" and the family-adjective forms
    # ("stark", "ugly", "obvious", "clear") get caught by the regex
    # below.
    "the mechanics are uncomfortable",
    "the mechanic is uncomfortable",
    # Generic opener tells that show up at the start of multiple bullets
    # in the same RECAP. Detected duplicate "Today's price action" usage
    # in the 2026-05-08 run — banning the bare opener forces variety.
    # The 2026-05-08T23-33-34Z run also had "this week's price action"
    # slip through because LINT only matched exact strings. Expanded
    # below to a regex pattern via BANNED_AI_TELL_REGEXES.
    "Today's price action",
    "Today's tape",
]

# Regex-based AI-tell patterns — for cases where the failure mode is a
# CATEGORY (e.g., any "<period>'s price action" opener) rather than a
# specific phrase. Each entry is (regex_pattern, kind_label) — applied
# alongside the literal-string BANNED_AI_TELLS in compose_lint_patterns.
#
# Use these when a single phrase can take many surface forms but is
# fundamentally the same generic-newsletter opener.
BANNED_AI_TELL_REGEXES: list[tuple[str, str]] = [
    # "today's / this week's / friday's / monday's / this morning's" + price action
    # Catches: "Today's price action is...", "This week's price action on Intel...",
    # "Friday's price action shows...", "the price action made it explicit..."
    (r"\b(?:today's|todays|this\s+week's|this\s+morning's|friday's|monday's|tuesday's|wednesday's|thursday's|the)\s+price\s+action\b", 'AI-tell'),
    # "today's / this week's / etc." + tape
    (r"\b(?:today's|todays|this\s+week's|this\s+morning's|friday's|monday's|tuesday's|wednesday's|thursday's|the)\s+tape\b", 'AI-tell'),
    # Template counter-case openers — repeated across themes in the same
    # pulse becomes obvious AI scaffolding. The 2026-05-08T23-33-34Z run
    # used "the pushback", "the defense", "the opposite read", and
    # "the bear case here" — one each across 4 themes. Each becomes a
    # tell when the reader pattern-matches it as scaffolding.
    (r"\bThe\s+(?:bear\s+case|bull\s+case|opposite\s+read|pushback|defense)\s+here\s*[:.]", 'AI-tell-template'),
    (r"\bThe\s+(?:bear\s+case|bull\s+case|opposite\s+read|pushback|defense)\s*:", 'AI-tell-template'),
    # Pre-market RECAP mis-framing: at 9 AM ET, equity-index futures /
    # Treasury futures / oil futures / FX are ALL trading — only US cash
    # equities are closed. "Crypto is the only thing trading" is factually
    # wrong AND it showed up two days running (2026-05-11, 2026-05-12) as
    # the same opener. Catch the family: "crypto is the {only|one}
    # {thing|place|market|asset} {actually}? {trading|moving|open|live}".
    (r"\bcrypto\s+is\s+(?:the\s+)?(?:only|one)\s+(?:thing|place|market|asset)\s+(?:actually\s+|really\s+)?(?:trading|moving|open|live|active)\b", 'RECAP-misframe'),
    # Also catch the inverted "the only thing trading (right now) is crypto"
    (r"\bthe\s+only\s+(?:thing|market)\s+(?:actually\s+)?(?:trading|moving|open)\s+(?:right\s+now\s+)?is\s+crypto\b", 'RECAP-misframe'),
    # Banker-tic family — "the [binding|dominant|cleanest|deciding|gating]
    # [variable|constraint|signal|factor|read|driver]". This is the
    # phrase-family taxonomy refactor the 2026-06-01 QC review called
    # for. Without it every variant requires a manual add to
    # BANNED_META_NARRATION:
    #   2026-05-28: caught "the binding constraint" (added)
    #   2026-05-29: caught "the cleanest read", "the dominant variable"
    #               (added)
    #   2026-06-01: SCRUB caught "binding constraint"/"regime change"
    #               but missed "binding variable" in the RECAP
    # One regex covers the whole authority-signal-without-content family
    # so the next variant ("the gating constraint", "the deciding read",
    # "the cleanest driver") catches on its own.
    (
        r"\bthe\s+(?:binding|dominant|cleanest|deciding|gating|operative)"
        r"\s+(?:variable|constraint|signal|factor|read|driver|input)\b",
        'banker-authority-signal',
    ),
    # Adjacent family: "[X] is the [binding|dominant|...] [variable|...]"
    # — same authority-signal beat but in copular form ("the 4.6%
    # unemployment forecast is the binding variable"). Stems on the
    # same trigger words.
    (
        r"\bis\s+the\s+(?:binding|dominant|cleanest|deciding|gating|operative)"
        r"\s+(?:variable|constraint|signal|factor|read|driver|input)\b",
        'banker-authority-signal',
    ),
    # Mechanics/dynamics-are-uncomfortable family — 2026-06-04 QC
    # surfaced "The mechanics are uncomfortable when laid out" in
    # INSIGHTS #2. Same banker-tic as "the mechanism is straightforward"
    # — promises mechanism explanation without committing. Catches:
    #   "the mechanics are uncomfortable when laid out"
    #   "the mechanic is uncomfortable to see"
    #   "the dynamics are stark when you look at them"
    #   "the math is obvious once laid out"
    # Adjective list (uncomfortable|stark|ugly|obvious|clear|brutal)
    # captures the family; the noun list catches mechanic/mechanics/
    # dynamic/dynamics/math/picture.
    (
        r"\bthe\s+(?:mechanics?|dynamics?|math|picture)\s+(?:is|are)"
        r"\s+(?:uncomfortable|stark|ugly|obvious|clear|brutal|grim)"
        r"(?:\s+(?:when|once)\s+(?:laid|spelled|written|put)\s+out)?\b",
        'banker-mechanics-tic',
    ),
    # Standalone "when laid out" / "once laid out" closer — the
    # qualifying-clause filler that follows the mechanics-uncomfortable
    # tell. Catches even when the main phrase varies ("the picture is
    # ugly when laid out" — caught by the regex above; "the trajectory
    # is bleak when laid out" — caught here).
    (
        r"\b(?:uncomfortable|stark|ugly|obvious|clear|brutal|grim|bleak)"
        r"\s+(?:when|once)\s+(?:laid|spelled|written|put)\s+out\b",
        'banker-mechanics-tic',
    ),
]

BANNED_META_NARRATION = [
    "cross-bank consensus",
    "research suggests",
    "the corpus shows",
    "multiple banks converge",
    # Italicized-punchline / Movement 1 corpus-meta-narration tells —
    # these describe the SOURCES rather than asserting a view. The
    # punchline is supposed to be the analyst's call, not a summary of
    # the disagreement among banks.
    "banks are debating",
    "researchers are split",
    "analysts are split",
    "there's disagreement over",
    # Pedagogical / explanatory asides — banker-newsletter asserts;
    # it doesn't lecture the reader on analytical structure.
    "is a counter-example",
    "is the textbook signature",
    "is the canonical example",
    "to put this in context",
    "as a useful frame",
    "the analytical lens",
    # 2026-05-29 QC additions — pulse shipped "cross-desk positioning
    # color is..." in RECAP closing. Family of "positioning color" /
    # "cross-desk color" phrases that describe SELL-SIDE TEXTURE rather
    # than assert a view. Banker-filler dressed as commentary.
    "cross-desk positioning color",
    "cross-desk color",
    "positioning color is",
    # Banker-jargon framings that read as authority signals without
    # adding content. Specific instances ("the binding constraint",
    # "the cleanest read", "the dominant variable") are now caught by
    # the banker-authority-signal regex family in BANNED_AI_TELL_REGEXES
    # above — no need to enumerate them here. The QC pattern was:
    # add a new variant to this list every week, never catch the next
    # one until it ships. The family regex closes the loop.
    # Pulse-internal scaffolding that leaks: model writes "the setup is
    # a directional dispersion trade rather than a timing trade" —
    # "directional dispersion trade" is jargon AND template-scaffolded
    # phrasing. Catches the bare phrase; the BARE_JARGON list below
    # picks up "dispersion trade" in isolation.
    "directional dispersion trade",
    "directional bias trade",
    # 2026-06-01 /ask QC additions: architecture-leak phrases. SV asked
    # "what was discussed in chat between 5pm and 9pm est" and the bot
    # declined with "the chat logs available to me don't cover that
    # block of time in enough detail." That phrasing exposes the bot's
    # internal data access. Same family as "in my context" — narrates
    # the rule rather than just declining. The decline-clause itself
    # is fine ("can't pull that one cleanly"); just the architecture
    # narration has to go.
    "available to me",
    "in my context",
    "my context",
    "my immediate context",
    "the chat logs i have",
    "the chat logs available",
    "the logs i have",
    "the data i have",
    "to my context",
    "from my context",
    "in enough detail to",
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

# Publication / commentary brands the user explicitly does not want
# attributed in pulse prose, even as a parenthetical citation.
# Reason: these are non-bank commentary publications. If their content
# is the primary source for a data point, present the data without
# attribution — the data stands on its own. If the model would otherwise
# write "(per The Market Ear)" or "FX Daily flags...", strip and rewrite.
#
# The source-prefix rule (BANKS x VERBS) catches "TME notes that..."
# style preambles. This list catches the rest: parenthetical "(Market
# Ear)" attributions, "as flagged in FX Daily" mid-sentence cites, etc.
BANNED_PUBLICATION_NAMES = [
    "The Market Ear",
    "Market Ear",
    "TME",
    "FX Daily",
]

# Tier-1 bank weighting hierarchy. When two sources disagree on a call,
# the pulse should weight Tier-1 banks more heavily and treat Tier-2 as
# supplementary. This is content guidance for DRAFT/AUDIT, not a
# linter pattern — the prose synthesis uses these to decide which
# call to lead with when consensus is split.
TIER_1_BANKS = ["JPMorgan", "Bank of America", "Goldman Sachs"]

# Non-bank publications whose theme_stances should NOT drive INSIGHTS
# themes on their own. These are commentary/color sources (no underwriting
# desk, no proprietary research division) — useful for vol/positioning
# observations and short-term color, but not a substitute for a Tier-1
# bank's analytical view. A theme whose ONLY source is in this set should
# be flagged as `non_bank_only=True` in the theme_map so the writer doesn't
# promote it to INSIGHTS without multi-bank corroboration.
#
# Today's QC review flagged this: 3 of 5 INSIGHTS themes in the test pulse
# were drawn from single-source TME stances. Adding the flag lets the
# theme_coverage block surface it explicitly.
NON_BANK_SOURCES = {
    "The Market Ear",
    "Market Ear",
    "TME",
    "FX Daily",
    "Bloomberg",  # data feed, not analytical research
    "Reuters",    # news wire, not analytical research
    "Unknown",    # unattributed sources
}

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
    "bps": "basis points (one-hundredth of a percent)",
    "basis points": "basis points (one-hundredth of a percent)",
    # 2026-05-29 QC: SCRUB rewrote "25-40 basis points" to "25-40
    # hundredths of a percent" — a regression. The rule is FIRST-USE
    # EXPLAIN, not ELIMINATE. The translation now keeps the term in
    # the output and adds the parenthetical on first use only.
    # Dispersion trade family — bank-jargon framings the model leans
    # on as authority signals. Each gets a plain-English equivalent
    # the writer can substitute on first use.
    "dispersion trade": "a bet that stocks move in different directions, not that the index moves up or down",
    "directional dispersion": "a bet on the direction of the spread between stocks (rather than the index direction)",
    "pair trade": "long one stock or sector, short another — bet on the spread",
    "relative value": "betting one asset will outperform another",
    "rich-cheap": "expensive vs cheap relative to history",
    "regime change": "a structural shift in how markets behave",
    # Short self-defining translation. The previous 13-word version
    # ("trend-following computer funds that buy when markets rise and sell
    # when they fall") was correct but broke prose rhythm when SCRUB
    # substituted it inline — "any catalyst forces selling on top of
    # trend-following computer funds that buy when markets rise and sell
    # when they fall and retail can't absorb both" is unreadable. The
    # parenthetical "(CTAs)" preserves the acronym so the second mention
    # in the same theme reads naturally; reader gets the explanation once
    # without needing the long phrase repeatedly.
    "CTAs": "systematic trend-followers (CTAs)",
    "RSI 70": "the market is technically overheated, like a rubber band stretched too far",
    "short gamma": "dealers are on the hook to buy more the higher the market goes",
    "term structure normalized": "the panic has faded",
    "skew catching a bid": "traders are paying more for downside protection",
    # Trader verb idioms — verbs and phrasal patterns that are technical
    # SENTENCE STRUCTURE, not just terms. Even with parenthetical
    # translation of the noun, the sentence still reads as trader-speak.
    # AUDIT should rewrite the whole sentence in plain English — not gloss.
    "got hit": "dropped (or sold off)",
    "caught a bid": "started rising on buying interest",
    "rolled over": "started falling",
    "took out": "broke through (or fell below, depending on direction)",
    "found support": "stopped declining at this level",
    "is offered": "selling pressure is building",
    "is bid": "buying pressure is building",
    "tape": "today's price action",
    "going bid": "buyers stepping in",
    "going offered": "sellers stepping in",
    "the long end": "30-year Treasury bonds",
    "the front end": "2-year and shorter Treasury bonds",
    "lifted offers": "buyers cleared out the sell orders at this price",
    "smoked the offers": "buyers cleared out the sell orders at this price",
    "got tapped": "sellers hit the bids and pushed price down",
    # Trader / desk-speak that slipped through earlier pulses — these are
    # clipped industry shorthand a non-finance reader cannot parse without
    # context. Rewrite into plain sentences.
    "chip leg": "the semiconductor side of the trade",
    "prime book": "what hedge funds at prime brokers are doing",
    "UTC day": "the calendar day in UTC time (use plain dates instead)",
    "bellwether": "a name the market treats as a leading indicator for its sector",
    "bellweather": "a name the market treats as a leading indicator for its sector",  # common misspelling
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
        "**Banned publication names (strip entirely, do not parenthetical-cite):**",
        f"Never name {', '.join(repr(p) for p in BANNED_PUBLICATION_NAMES)} in the pulse — not as story-connector preamble, not as parenthetical attribution, not as mid-sentence cite. These are non-bank commentary publications. If their content is the primary source for a data point, present the data without attribution; the data stands on its own. If the model would otherwise write \"(per The Market Ear)\" or \"FX Daily flags...\" or \"as TME points out\", strip and rewrite to a bare statement of the underlying claim.",
        "",
        "**Source weighting (binding when banks disagree):**",
        f"Tier-1 banks: {', '.join(TIER_1_BANKS)}. When two banks make conflicting calls, weight the Tier-1 bank's view more heavily and treat the other as supplementary or pushback. When citing supportive consensus, the Tier-1 attribution leads. Other banks (Citi, UBS, RBC, Barclays, etc.) are still cited when they have unique data points — they are NOT excluded — but they are not the primary voice when consensus is split.",
        "",
        "**Plain-English jargon scrub (BINDING — the highest-priority readability rule).** The audience is a self-directed US options/crypto trader, smart but NOT a finance professional. They read the WSJ, not institutional research. The default voice is **plain English written for that reader**.",
        "",
        "**REWRITE the sentence, do NOT just append a parenthetical translation.** Glossing terms in parens still leaves the sentence structure trader-speak — example failure: *\"long-end Treasuries got hit on coupon supply (new Treasury bonds being auctioned).\"* Even with the gloss, *\"got hit\"* and the bond-trader sentence rhythm are still broken for a non-trader. The correct rewrite: *\"30-year Treasury bonds fell because the government is auctioning a wave of new long-term debt — more supply means buyers demand higher yields.\"*",
        "",
        "**Order of preference when you find jargon:**",
        "1. **Rewrite the sentence** so the meaning is plain-English structure (no idioms, no compressed trader phrasing). This is the default.",
        "2. **Replace the term** with its plain equivalent inline, restructuring as needed.",
        "3. **Drop the term** entirely if removing it doesn't lose meaning.",
        "4. **Parenthetical gloss** ONLY when the term is a proper-noun-style technical concept that genuinely has no plain-English equivalent (rare — usually #1 or #2 work).",
        "",
        "Walk every paragraph in INSIGHTS bodies and RECAP. For each jargon term in the map below, prefer a sentence rewrite over a paren gloss. The full term-to-trader-friendly-equivalent map:",
        "",
    ]
    for term, translation in JARGON_WITH_TRANSLATIONS.items():
        parts.append(f'- **{term}** → "{translation}"')
    parts.extend([
        "",
        "**Sentence-rewrite test:** read each sentence aloud (mentally). Could a smart 28-year-old crypto trader who reads the WSJ understand it on first read, with no glossary lookup, no re-reading? If no, rewrite. The pulse is for them, not for the bond desk.",
        "",
        "**Worked example — the right way to fix trader-speak:**",
        "- Bad (gloss only): *\"the long-end (30-year Treasury bonds) got hit on coupon supply (new Treasury bonds being auctioned).\"*",
        "- Good (sentence rewrite): *\"30-year Treasury bonds dropped this morning because the government is about to auction a wave of new long-term debt this week, and more supply means lower prices.\"*",
        "- Bad (gloss only): *\"$BTC found support at the 50-day moving average and caught a bid.\"*",
        "- Good (sentence rewrite): *\"$BTC stopped falling at its 50-day average price and buyers stepped in there.\"*",
        "- Bad (gloss only): *\"hyperscaler capex revisions are pushing curves bear-steeper.\"*",
        "- Good (sentence rewrite): *\"big tech is spending so much on AI data centers that the Treasury market is starting to price in long-term inflation pressure — the 30-year yield is rising faster than the 2-year, which usually signals more inflation ahead.\"*",
        "",
        "If your rewrite is longer than the original, that's fine. Word-count is not the constraint; comprehension on first read is.",
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
    # Regex-based AI-tells (category matches, not literal strings).
    # These catch families of openers that take many surface forms.
    for regex, kind in BANNED_AI_TELL_REGEXES:
        patterns.append((regex, kind))
    for m in BANNED_META_NARRATION:
        patterns.append((word_pat(m), 'meta-narration'))

    # Source-prefix story-connectors — single composite pattern
    banks_alt = '|'.join(re.escape(b) for b in SOURCE_PREFIX_BANKS)
    verbs_alt = '|'.join(re.escape(v) for v in SOURCE_PREFIX_VERBS)
    patterns.append((rf"\b({banks_alt})('s)?\s+({verbs_alt})\b", 'source-prefix'))

    # Banned publication names — flag any occurrence, including
    # parentheticals and mid-sentence cites. These should never appear
    # in pulse prose.
    for pub in BANNED_PUBLICATION_NAMES:
        patterns.append((r'\b' + re.escape(pub) + r'\b', 'banned-publication'))

    return patterns


def compose_scrub_reference_block() -> str:
    """Build the jargon-to-plain-English reference + banned-publications
    block that SCRUB_SYSTEM interpolates.

    The SCRUB sub-agent uses this as the canonical map when rewriting
    flagged sentences. Pulled from the same JARGON_WITH_TRANSLATIONS
    dict the linter uses, so single source of truth.
    """
    lines = [
        "**Jargon-to-plain-English reference (canonical — use these or rewrite the sentence to drop the term):**",
        "",
    ]
    for term, translation in JARGON_WITH_TRANSLATIONS.items():
        lines.append(f'- **{term}** → "{translation}"')
    lines.extend([
        "",
        "**Banned publication names (strip entirely, never attribute):**",
        f"  {', '.join(repr(p) for p in BANNED_PUBLICATION_NAMES)}",
        "",
        "If the publication is the primary source for a data point, present the data without attribution; the data stands on its own.",
    ])
    return '\n'.join(lines)


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
