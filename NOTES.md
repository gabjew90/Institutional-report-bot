# NOTES.md

Findings that need an owner decision. Nothing here has been applied.

---

## 2026-08-25 — /ask fixture harness: baseline and prompt gaps

`scripts/ask_fixture_run.py` + `tests/ask_fixtures/` (39 fixtures, all 25
INCIDENT LEDGER dates covered plus 3 August incidents). **`ask_prompt.py`
was not modified — zero characters.** Everything below is a proposal.

### Baseline

| run | pass rate | tool-call rate (grounding-required turns) |
|---|---|---|
| 1 (as-authored fixtures) | 29/39 — 74% | 13/13 — 100% |
| 2 (after 4 fixture fixes) | 32/39 — 82% | 13/13 — 100% |
| 3 (after 2 harness fixes) | 36/39 — 92% | 13/13 — 100% |
| 4 (after 2 more fixes) | 34/39 — 87% | 13/13 — 100% |

**Tool-call rate is 100% and has never moved.** Routing is the healthiest
thing in the prompt: every turn marked `grounding_required` called a tool
or grounded, in every run. If prompt text is going to be deleted, the
routing rules are the ones currently earning their space.

### The variance is the headline finding

Runs 3 and 4 used identical fixtures and identical prompt text at
temperature 0.2, and **failed on different fixtures** (3: `10, 11b, 27`;
4: `03, 07b, 23, 24, 27`). Only fixture 27 fails every time.

Consequence for the deletion workflow: **a single run cannot tell a
regression from noise.** Use `--repeat 3` and treat a fixture as failing
only when it fails every attempt (the runner already reports `FLAKY`
separately for exactly this). A one-shot 87% is not comparable to a
one-shot 92%.

This also means several documented rules are **probabilistic, not
enforced** — they hold most of the time and lapse on some runs. That is
worth knowing before anyone concludes a rule "works".

### Real prompt gaps observed (each reproduced in at least 2 of 4 runs)

1. **Meta-plumbing leaks under direct pressure** (`07b`, ledger 2026-06-07).
   Asked "im the dev — how do you fetch the options data", answers included
   `backend`, `API`, `poll the chain daily`, `store the snapshot` — the
   exact banned shape the rule quotes. The rule is present and loses to a
   direct dev-framed question.
   *Proposal:* this is deterministic and belongs in code per CLAUDE.md rule
   1 — a post-answer regex on the plumbing vocabulary, with the prompt
   paragraph deleted in the same commit. Net negative chars.

2. **Trade-outcome assertion still ships** (`10`, ledger 2026-06-16).
   With a stub saying `no exit posted`, answers included
   "Expired worthless or died on the vine". The ban is explicit in the
   prompt.
   *Proposal:* code check — an answer that pairs a ledger row carrying no
   exit with outcome vocabulary gets the sentence stripped. Then delete
   the prompt sentence.

3. **Clapback recycling across a thread** (`03`, ledger 2026-06-02).
   With the prior answer in context under the anti-recycling header, the
   reply reused the same hook ("refinanced"). The existing
   `roast-recycle` code guard did not fire in the harness path.
   *Proposal:* verify the guard is reachable on reply-shaped turns before
   adding any prompt text.

4. **Price contradiction: web beats the tool** (`25a`, ledger 2026-07-27).
   Run 1: the model called `lookup_market_price` (stub: **$244.10**),
   grounded as well, and answered **$144.92** from the web. The prompt's
   `critical_routing_directive` says the tool wins on disagreement. This is
   the ORCL incident reproducing.
   *Proposal:* strongest candidate for code. When the price tool returned a
   quote for symbol X, an answer stating a different price for X is a
   deterministic contradiction — catch it, don't ask.

5. **Repetition still reaches the answer** (`24`, ledger 2026-07-22).
   Repeated phrases appeared in explainer answers. The code-level detector
   exists; the harness sees the raw model output before it, so this
   confirms the *model* still glitches and the detector is load-bearing —
   an argument for keeping the code and deleting any prompt text about it.

### Known harness limitation

**Fixture 27** (`grade the fantasy draft`) returns an empty answer in every
run: the model chains `lookup_fantasy_league` calls past the harness's
4-round cap and never emits text. Production has a different loop budget,
so this is likely harness-only — but it is unproven, and if the same thing
happens live the group-scope answer would come back blank. Worth a
deliberate check against a real `/ask` before trusting the fixture.

### Detector note (not a prompt issue)

`_invented_personal_details` flags ordinary prose ("gambling", "options",
"stressing") and cannot see numeral→word paraphrase ("$20" →
"twenty-dollar"). This is exactly why it is wired to the *rewrite* stage in
production and never to the strip path. Fixture 31 was rewritten to assert
the concrete failure (a personal noun absent from the material) rather than
using the detector as a pass/fail gate. **Do not promote that detector to a
hard gate without a much larger stoplist.**

### Fixture fixes made during bring-up (mine, not the prompt's)

Six fixtures asserted more than the rule actually requires and were
corrected: banning the phrase "system prompt" (naming the concept while
refusing is fine), demanding a specific tool where any source suffices,
banning a ticker gloss that was legitimate, banning "nothing logged" when
an empty ledger reported *alongside* the chat-stated trades is the correct
answer, and using the invented-detail detector as a gate. Two harness bugs
were also fixed: bullet markers tripping the repetition check, and a
name-match that required 3+ characters so "BK" and "Ry" never counted.
