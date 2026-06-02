"""Probe: why did _has_repetition_glitch miss the 2026-06-01 glitches?

Feeds each shipped-with-glitch answer through the detector and reports
whether each gate fires. Helps locate the gap in either tail size,
threshold, or token regex.
"""

import sys
from discord_bot.bot import _has_repetition_glitch, _REP_TOKEN_RE, _REP_STOPWORDS


SHIPPED_GLITCHES = [
    (
        "16:50:30 AVGO/CRWD earnings answer",
        # The actual tail that shipped
        "Bias: long $AVGO if you want the pure AI-infra play, but keep the "
        "size tight, these names are priced for perfection and any guidance "
        "hiccup will get punished by the hyperscalers will get punished "
        "instantly kills the momentum instantly-overbought tech sector will "
        "get punished hardliners will get punished hard",
    ),
    (
        "16:57:51 HPE historicals answer",
        "Context: Rallying on AI server demand and sympathy with $DELL, the "
        "setup is less about the historical average and more about whether "
        "they can convert the $5B+ AI backlog into recognized revenue without "
        'the "lumpiness" that plagued previous quarters past quarters-lumpy" '
        'delays that plagued recent-history" narrative dragging" delays',
    ),
    (
        "21:54:25 TMO 'first to the first' glitch",
        "Market Role: It is a massive, diversified proxy for R&D spending "
        "across the global healthcare sector, when pharma companies increase "
        "their research budgets, $TMO is the first to the first to see the "
        "volume.",
    ),
]


def trace(label: str, text: str):
    print(f"\n=== {label} ===")
    glitched = _has_repetition_glitch(text)
    print(f"detector says: glitched={glitched}")

    tokens = [m.group(0).lower() for m in _REP_TOKEN_RE.finditer(text)]
    print(f"total alpha tokens: {len(tokens)}")
    print(f"last 25 tokens: {tokens[-25:]}")

    # Gate 1 trace
    tail = tokens[-15:]
    counts: dict[str, int] = {}
    for t in tail:
        if t in _REP_STOPWORDS or len(t) < 4:
            continue
        counts[t] = counts.get(t, 0) + 1
    top = sorted(counts.items(), key=lambda x: -x[1])[:5]
    print(f"gate-1 (tail-15, content-word >= 3): top counts: {top}")
    if counts and max(counts.values()) >= 3:
        print("  -> GATE 1 FIRES")
    else:
        print("  -> gate 1 misses")

    # Gate 2 trace
    tail_start = max(0, int(len(tokens) * 0.75))
    tail_tokens = tokens[tail_start:]
    bigram_counts: dict[tuple[str, str], int] = {}
    for i in range(len(tail_tokens) - 1):
        a, b = tail_tokens[i], tail_tokens[i + 1]
        if a in _REP_STOPWORDS or b in _REP_STOPWORDS:
            continue
        if len(a) < 4 or len(b) < 4:
            continue
        bg = (a, b)
        bigram_counts[bg] = bigram_counts.get(bg, 0) + 1
    rep_bigrams = [(k, v) for k, v in bigram_counts.items() if v >= 2]
    print(f"gate-2 (last 25% bigrams >=2): {rep_bigrams[:5]}")
    if rep_bigrams:
        print("  -> GATE 2 FIRES")
    else:
        print("  -> gate 2 misses")


CLEAN_ANSWERS_FROM_TODAY = [
    (
        "14:23:59 Meta announcement (CLEAN, must NOT fire)",
        "Meta recently rolled out global \"Plus\" subscription tiers for "
        "Instagram, Facebook, and WhatsApp (announced May 27, 28), alongside "
        "tests for a unified \"Meta One\" AI subscription umbrella. The stock "
        "initially popped ~3% on the news, but the \"give up\" Nft_spaceman is "
        "referring to is the subsequent price action as the market re-evaluates "
        "the monetization timeline versus the company's massive, ongoing AI "
        "infrastructure spend.",
    ),
    (
        "16:17:46 Schwab donkey list (CLEAN)",
        "Consider it done. Schwab's been on the donkey list since they "
        "started flagging every high-conviction setup as \"untradeable\" right "
        "when the ripper starts. They're the ultimate cockblock for when "
        "you're trying to size up, probably too busy managing their own "
        "bag-holders to let you catch a move.",
    ),
    (
        "18:59:47 SEC PDT rule (CLEAN, long factual)",
        "The SEC-approved FINRA rule change eliminating the Pattern Day "
        "Trader (PDT) designation and the $25,000 minimum equity requirement "
        "takes effect June 4, 2026. The rule doesn't move the requirement to "
        "$2,500, it abolishes the PDT framework entirely and replaces it with "
        "a risk-based intraday margin system, requiring only the standard "
        "$2,000 minimum equity for margin accounts. While the rule is "
        "effective June 4, brokerages have a transition period to implement "
        "the new systems, with full compliance required by October 20, 2027.",
    ),
    (
        "21:54:25 TMO (truncated, CLEAN portion before glitch)",
        "$TMO (Thermo Fisher Scientific) is the world's largest provider of "
        "life sciences tools, analytical instruments, and laboratory services, "
        "effectively the \"picks and shovels\" supplier for the entire biotech "
        "and pharmaceutical industry. Business Segments: Operates across four "
        "main pillars: Life Sciences Solutions (reagents/consumables), "
        "Analytical Instruments (mass spec/electron microscopes), Specialty "
        "Diagnostics (clinical tests), and Laboratory Products/Biopharma "
        "Services (outsourced clinical trials and supply chain). Revenue "
        "Driver: High-margin recurring revenue from consumables and reagents, "
        "which labs must replenish constantly, provides a defensive moat "
        "compared to one-off instrument sales.",
    ),
]


if __name__ == "__main__":
    print("=== SHIPPED GLITCHES (should ALL detect now) ===")
    for label, text in SHIPPED_GLITCHES:
        trace(label, text)
    print("\n=== CLEAN ANSWERS (should NONE detect) ===")
    false_fires = 0
    for label, text in CLEAN_ANSWERS_FROM_TODAY:
        print(f"\n--- {label} ---")
        glitched = _has_repetition_glitch(text)
        print(f"  detector says: glitched={glitched}")
        if glitched:
            false_fires += 1
            print(f"  ** FALSE FIRE **")
    print(f"\n=== summary: false fires on clean answers: {false_fires}/{len(CLEAN_ANSWERS_FROM_TODAY)} ===")
