# Grader — metric 1, grouping integrity (FROZEN before day 1)

You are grading the pilot ledger's groupings for ONE day. You have no
drafting history. You judge subjects, not prose.

Input (below): the ledger's reader topic labels with the cards under
each, and the instrument groups. Every card has an id, a bank, a
claim, and its instruments.

## Definitions (frozen)

- **Fragmentation**: two or more labels that a careful reader would
  say cover the SAME underlying subject (the same event, thesis, or
  instrument story), split only by wording. "AI capex cycle" and
  "hyperscaler spending acceleration" describing the same capex
  argument are one subject under two labels.
- **Mis-merge**: one label holding two DISTINCT subjects that a
  careful reader would never put in one theme. "Oil" holding both a
  Hormuz supply-risk thesis and a refiner margin call is a mis-merge
  if those would be different themes in a pulse.
- **Card mass**: count cards, not labels. Fragmented mass is the
  number of cards sitting in labels you judged fragmented, over all
  cards.
- A mis-merge "would have changed theme selection" when an editor
  choosing the day's lead themes from the labels would have picked a
  different theme, or missed one, because of the merge.

## Procedure

1. Read every label and its cards. Do not stop at the first twenty.
2. List fragmentation sets: each set is two or more labels you judge
   to be one subject. Name the subject in plain words.
3. List mis-merges: each is one label plus the two or more subjects it
   holds.
4. Count fragmented card mass and compute the share.
5. For each mis-merge, decide whether it would have changed theme
   selection, and say why in one sentence.

## Output

STRICT JSON, nothing else:

```json
{
  "metric": 1,
  "total_cards": 0,
  "fragmentation_sets": [{"subject": "...", "labels": ["...", "..."], "cards": 0}],
  "fragmented_mass_share": 0.0,
  "mis_merges": [{"label": "...", "subjects": ["...", "..."], "would_change_theme_selection": false, "why": "..."}],
  "pass": true,
  "notes": "one or two sentences at most"
}
```

`pass` is true when `fragmented_mass_share` <= 0.10 AND no mis-merge
has `would_change_theme_selection` true. Compute it yourself and let
the scoreboard recompute it from your numbers.
