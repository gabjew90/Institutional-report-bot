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

## Procedure

For every sentence: search the source texts (grep for the figures and
names first, then read the surrounding paragraph), decide the grade,
and quote the supporting or contradicting source span (verbatim, up
to 30 words) with the file it came from. Do not skip a sentence. Do
not grade from memory of what banks usually say.

## Output

STRICT JSON, nothing else:

```json
{
  "metric": 2,
  "artifact": "shadow | production",
  "sentences": [
    {"id": "s1", "grade": "faithful | distorted | unsupported",
     "live_market": false, "source_file": "...", "span": "...", "why": "..."}
  ],
  "faithful": 0, "distorted": 0, "unsupported": 0,
  "faithful_rate": 0.0
}
```

Counts must match the sentence list. `faithful_rate` is faithful over
all 15.
