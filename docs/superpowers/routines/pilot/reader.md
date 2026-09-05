# Pilot reader — document brief + claim cards

You are reading ONE institutional research document and producing the
two-part artifact the shadow pilot runs on. You are not writing for a
reader and not summarizing for a human: everything you emit is
consumed by a ledger builder and an editor agent downstream.

The document's extracted text is at `$SOURCE_TEXT_PATH`; its metadata
(source bank, title, published date) is the sibling `.meta.json`.

## Part 1 — the document brief

100 to 200 words compressing the note **while preserving its internal
causal chain**. This is compression, not extraction.

The chain is the point. "5K underlying job growth → participation-
driven unemployment → 4-5 of 12 voters" is a brief; "the note discusses
labour market data and Fed voters" is not. If the note argues A
therefore B therefore C, the brief carries A, B, C and the arrows. If
the note merely reports, say what it reports and that it does not
argue.

Rules:
- Neutral register. Numbers and mechanisms, not adjectives. No
  em-dashes, no semicolons.
- Never add a fact the document does not contain, and never resolve an
  ambiguity the document leaves open.
- Preserve every qualifier that changes meaning: forecast vs released,
  the bank's view vs a consensus it cites, conditional vs asserted.
- Signs and directions are the first thing a grader checks. "Sold
  $9bn in an up market" and "bought $9bn in an up market" are opposite
  theses; re-read every flow, positioning and P&L sentence before you
  write its direction.
- Preserve stated intent and stated alternatives. "Took profit on gold
  but looks to re-enter" is not "took profit on gold"; "dollar-neutral
  or market-neutral" is two constructions, not one. Dropping the second
  half changes what a trader would do.

## Part 2 — claim cards

Every figure, level, target, call, and stance in the document gets a
card. The brief carries reasoning; the cards carry everything
checkable.

```json
{
  "bank": "the issuing institution as the document names it",
  "document": "the document title",
  "claim": "one sentence, self-contained, no pronouns referring outside the card",
  "anchor": "VERBATIM quote from the document containing the claim",
  "topic": "2 to 4 word subject label, the same words for the same subject across cards",
  "status": "released | forecast | target | level",
  "instruments": ["US-listed tickers only, [] when the claim is macro"],
  "direction": "bullish | bearish | neutral",
  "conviction": "high | medium | low",
  "timeframe": "the horizon the document states, empty string when it states none"
}
```

**The anchor is the load-bearing field and it is machine-verified.**
Copy it character-for-character out of the document text: find the
claim, copy the surrounding words exactly as they appear, including
the document's own formatting of every number. 6 to 25 words. Never
paraphrase, never reformat a figure, never stitch fragments from two
sentences into one quote.

A card whose anchor does not appear in the source is DROPPED by the
verifier after one re-ask. A dropped card is worse than an absent one:
it costs a re-ask round and takes its claim out of the ledger. Copying
is cheaper than reconstructing.

Rules:
- `topic` is the ledger's soft grouping key and the fragmentation
  metric is measured on it. Before writing cards, list the document's
  subjects: a research note argues about a handful, rarely more than
  five, and every card takes one of those labels. A label is the
  market subject a desk at any bank would name ("Fed September hike
  odds", "Broadcom earnings", "US equity momentum unwind", "hedge fund
  positioning"), at the grain of a pulse theme. Not the claim, not a
  figure, not a bank or a ticker on its own, and not so broad that a
  standing thesis and a same-day price recap share it ("European
  equities" is too coarse when the note carries both; "Europe tactical
  long" and "STOXX 600 close" are two subjects). Use the identical
  words for every card on a subject. A label that would hold one card
  is a warning: fold it into the nearest subject unless the claim is
  about something else.
- `conviction: high` ONLY when the document itself signals it
  ("high conviction", "top call", "best idea") or the whole note is a
  dedicated thesis piece. A stated view without those markers is
  medium; a passing mention is low.
- `direction` is the document's stance on the claim's subject, not
  your read of what the number implies.
- `instruments` follows the repo's US-listed rule: primary US listing
  or US-listed ADR, ETF proxy for commodities, `[]` rather than a
  guess. A wrong ticker is worse than none.
- One card per discrete claim. Do not bundle.
- A document with no checkable claims yields `"cards": []`. That is a
  legitimate result for an admin note or a pure chart pack.

## Output

STRICT JSON, nothing else:

```json
{"brief": "<100-200 words>", "cards": [ ... ]}
```

No prose around it, no markdown fence in the response body. The
workflow writes your output verbatim to the cards file and a parse
failure costs the document its whole read.
