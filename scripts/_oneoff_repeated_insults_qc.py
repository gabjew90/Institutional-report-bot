"""QC pass on today's ask log to detect repeated-insult patterns.

The user's hypothesis: the bot keeps recycling the same insults across
unrelated questions. This script:
  1. Parses today's log
  2. For each interaction, extracts the bot's answer text
  3. Builds n-gram frequencies (3-word, 4-word, 5-word) across answers
  4. Flags ngrams that appear in >= 3 distinct answers — those are
     repetition patterns
  5. Separately scans for known 'insult template' anchor words
     (degenerate, retarded, speedrun, fumbling, bagholder, slur tokens,
     'getting raped', 'cooked', etc.) and counts which interactions
     used each anchor
  6. Surfaces the most-repeated insults + which Q/A they appeared in
"""

from collections import Counter, defaultdict
import re
import sys

from ask_qc.parser import parse_ask_log


def _norm(s: str) -> str:
    """Lowercase + collapse whitespace + strip markdown markers."""
    s = re.sub(r"[*_`>]+", "", s.lower())
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _tokenize(s: str) -> list[str]:
    return re.findall(r"[a-z']+|\$[A-Z]+", _norm(s))


def _ngrams(tokens: list[str], n: int) -> list[str]:
    if len(tokens) < n: return []
    return [" ".join(tokens[i:i+n]) for i in range(len(tokens) - n + 1)]


# Known insult templates — these phrases or stems are the bot's
# go-to register. The QC wants to know how often these recur across
# different questions.
INSULT_ANCHORS = [
    "degenerate", "degen",
    "retarded", "retard",
    "speedrun", "speedrunning",
    "fumble", "fumbling", "fumbled",
    "bagholder", "bag holder",
    "cooked",
    "bleeding out",
    "blow up", "blowing up", "blown up",
    "tilted", "tilting", "tilt cycle",
    "nigga",
    "homelessness",
    "chocolate", "playing in the mud",
    "yacht", "trust fund",
    "biohacking",
    "your account is",
    "your port is",
    "got raped", "getting raped",
    "shitter", "shitcoin",
    "bing bong",
    "to zero", "going to zero",
    "down bad",
    "average down",
    "exit framework",
    "stop loss",
]


def main():
    text = open(".tmp_ask_2026_06_02.md", encoding="utf-8").read()
    interactions = parse_ask_log(text)
    print(f"Parsed {len(interactions)} interactions from 2026-06-02 log")
    print()

    answers = [(i.ts_utc, i.asker_label, i.answer or "") for i in interactions]

    # n-gram analysis: count UNIQUE-interaction occurrences (not raw
    # total) so the same phrase repeated 10x within one answer counts as 1.
    for n in (5, 4, 3):
        ngram_to_interactions = defaultdict(set)
        for idx, (_ts, _asker, ans) in enumerate(answers):
            tokens = _tokenize(ans)
            for ng in set(_ngrams(tokens, n)):
                ngram_to_interactions[ng].add(idx)
        # Filter: only ngrams that appear in 3+ distinct answers
        recurring = sorted(
            [(ng, ix_set) for ng, ix_set in ngram_to_interactions.items()
             if len(ix_set) >= 3],
            key=lambda x: -len(x[1]),
        )
        # Drop noise: ngrams that are pure stopwords / cashtag / time markers
        stopword_pattern = re.compile(
            r"^(the|a|an|of|to|and|or|but|for|in|on|at|with|by|"
            r"this|that|is|was|are|been|being|has|have|had|do|"
            r"does|did|will|would|could|should|may|might|must|"
            r"i|you|he|she|we|they|it|its|my|your|his|her|"
            r"after.hours?|pre.market|post.market|today|yesterday|"
            r"this morning)$"
        )
        recurring = [(ng, ix) for ng, ix in recurring
                     if not all(stopword_pattern.match(w) for w in ng.split())]
        if recurring:
            print(f"=== {n}-grams appearing in 3+ distinct answers ===")
            for ng, ix_set in recurring[:15]:
                print(f"  {len(ix_set):2d} answers  | {ng!r}")
            print()

    # Insult-anchor scan: for each anchor, list every interaction that
    # used it. Surfaces the bot's "favorite" insult templates.
    print("=== Insult anchor usage across answers ===")
    anchor_hits = defaultdict(list)
    for idx, (ts, asker, ans) in enumerate(answers):
        ans_low = _norm(ans)
        for anchor in INSULT_ANCHORS:
            # Word-boundary match for simple anchors, substring for
            # multi-word phrases (which already include spaces)
            if " " in anchor:
                if anchor in ans_low:
                    anchor_hits[anchor].append((idx, ts, asker))
            else:
                if re.search(rf"\b{re.escape(anchor)}\b", ans_low):
                    anchor_hits[anchor].append((idx, ts, asker))
    for anchor, hits in sorted(anchor_hits.items(), key=lambda x: -len(x[1])):
        if len(hits) >= 2:  # only surface anchors used twice+
            askers = sorted({h[2].split('(')[0].strip() for h in hits})
            print(f"  {len(hits):2d}x  {anchor!r:25s}  across {len(askers)} askers: {', '.join(askers[:6])}")
    print()

    # Final flag: what % of ALL answers contained at least one anchor?
    contaminated = 0
    for _idx, (_ts, _asker, ans) in enumerate(answers):
        ans_low = _norm(ans)
        if any(
            (anchor in ans_low if " " in anchor
             else re.search(rf"\b{re.escape(anchor)}\b", ans_low))
            for anchor in INSULT_ANCHORS
        ):
            contaminated += 1
    pct = (100.0 * contaminated / len(answers)) if answers else 0.0
    print(f"Answers containing at least one insult-anchor: "
          f"{contaminated}/{len(answers)} ({pct:.0f}%)")


main()
