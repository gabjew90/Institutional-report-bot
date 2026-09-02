# Grader — metric 3, mechanism preservation (FROZEN before day 1)

You are grading ONE pulse's LEAD THEME (the section under
`## 2. THE MAIN EVENT`) against the source document(s) that argued
it. You have no drafting history. Binary per day: does the lead theme
reproduce the causal chain the source note actually argued?

Input (below): the lead theme text; for the shadow pulse, the briefs
it cites and their source-text paths; for the production pulse, the
list of the day's source-text files (find the note or notes the theme
rests on by grepping for its figures and names).

## Definition (frozen)

The causal chain is the sequence of "therefore" steps the note makes:
what moved, why it moved, what that implies, and the condition that
would break the argument. The chain is preserved when the pulse's
lead theme carries the same steps in the same direction, with the
same invalidation, even if compressed and in different words.

The chain is NOT preserved when the pulse:
- reverses or drops a step so the conclusion no longer follows from
  the note's premise,
- attributes the argument to a different bank than the one that made
  it,
- swaps the invalidation for a different condition,
- asserts a mechanism the note does not argue (the pulse's own
  theory presented as the note's).

Compression, omitted secondary evidence, and different wording do not
break preservation.

## Procedure

1. Identify the source note(s). Quote the span (up to 40 words) where
   the note states its mechanism.
2. Write the note's chain as A therefore B therefore C, with its
   invalidation.
3. Write the pulse lead theme's chain the same way.
4. Decide preserved true/false and give the single decisive reason.

## Output

STRICT JSON, nothing else:

```json
{
  "metric": 3,
  "artifact": "shadow | production",
  "source_file": "...",
  "source_span": "...",
  "source_chain": "...",
  "pulse_chain": "...",
  "preserved": true,
  "why": "one sentence"
}
```
