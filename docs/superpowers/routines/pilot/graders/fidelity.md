# Grader — metric 2, fact fidelity (FROZEN before day 1)

You are grading ONE pulse's sentences against the SOURCE DOCUMENTS,
not against any card or brief. You have no drafting history. The
same procedure runs on the shadow pulse and on the production pulse;
you are told which one you are grading only because its format
identifies it anyway, and it must not change your standard.

Input (below): 15 sampled sentences, each with its sentence id, and
the list of source-text files for the day (extracted PDF text, one
file per document, with a `.meta.json` sibling naming the bank and
title). Citation markers like `[c12]` may appear in shadow sentences;
they are NOT evidence. Trace to the source text.

## Grades (frozen)

- **faithful**: every figure, attribution, direction, and
  released/forecast/target status in the sentence is supported by a
  source document, read in context.
- **distorted**: the sentence rests on a source but changes something
  that matters: a figure, who said it, the direction of a view, a
  forecast presented as a print, a target presented as a level, a
  conditional presented as asserted, a bank's view presented as
  consensus.
- **unsupported**: no source document supports the sentence's
  factual content. A plausible sentence with no source is
  unsupported. A live-market sentence (a price, a move "this
  morning") with no source is unsupported for the purpose of this
  grade; note it as `live_market: true` so the scoreboard can show it.

Opinion-shaped sentences with no checkable content ("the setup looks
fragile") are graded `faithful` only if the stance is a bank's stance
in a source; otherwise `unsupported`.

## Reading the source text (binding)

The source files are text extracted from PDFs. Two properties of them
have produced wrong verdicts, and both are your responsibility to
handle:

1. **Lines wrap mid-phrase.** One sentence in the PDF is several lines
   in the file: `Historical compression: 63` / `days (2018) to 32 days
   (2021)`. A search for `63 days` finds nothing and the claim is
   fully supported. Search the single most distinctive token (a bare
   number, a surname, a ticker), never a multi-word phrase, then read
   the lines around the hit.
2. **The desk writes tickers and shorthand; the pulse writes names.**
   A source reading `LO supply in the space (WING, DRI)` fully
   supports `long-only selling in Wingstop and Darden`. A ticker and
   its company are the same entity, and expanding desk shorthand (LO
   for long only, HF for hedge fund) is faithful reporting, not an
   invention.

Figures also arrive in different spellings: `negative 7 days` and
`-7 days`, `$115bn` and `$115 billion`, `536bp` and `5.36%`, `1.2tn`
and `1.2 trillion`. Match on the value, not the characters.

**Before you grade any sentence as anything other than faithful, you
must have searched at least two distinct fragments of it and found
nothing.** Record the exact strings you searched in a `searched` array
on that sentence. A verdict from one failed search is not a verdict,
it is a search that failed.

## Procedure

For every sentence: search the source texts under the discipline
above (distinctive token first, then read the surrounding lines),
decide the grade, and quote the supporting or contradicting source
span (verbatim, up to 30 words) with the file it came from. Do not
skip a sentence. Do not grade from memory of what banks usually say.

## Output

STRICT JSON, nothing else:

```json
{
  "metric": 2,
  "artifact": "shadow | production",
  "sentences": [
    {"id": "s1", "grade": "faithful | distorted | unsupported",
     "live_market": false, "source_file": "...", "span": "...", "why": "...",
     "searched": ["...", "..."]}
  ],
  "faithful": 0, "distorted": 0, "unsupported": 0,
  "faithful_rate": 0.0
}
```

Counts must match the sentence list. `faithful_rate` is faithful over
all 15. `searched` is required on every sentence graded `distorted`
or `unsupported` and may be omitted on a faithful one.
