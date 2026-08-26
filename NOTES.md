# NOTES.md

Findings that need an owner decision. Nothing here has been applied.

---

## 2026-08-25 — /ask fixture harness: baseline and prompt gaps

`scripts/ask_fixture_run.py` + `tests/ask_fixtures/` (39 fixtures, all 25
INCIDENT LEDGER dates covered plus 3 August incidents). **`ask_prompt.py`
was not modified — zero characters.** Everything below is a proposal.

### Baseline — authoritative run

**`docs/ask-baseline-f9bae39.json`, `--repeat 3`.** This supersedes
`docs/ask-baseline-01f124a.json`, which was recorded before fixtures 12,
22b, 15 and 33 had their assertions tightened and therefore measures a
different suite. Do not compare against the old file.

| metric | f9bae39 (current) | 01f124a (superseded) |
|---|---|---|
| PASS (3/3 attempts) | **31/39 — 79%** | 32/39 — 82% |
| FLAKY (passed 1-2 of 3) | 4 | 6 |
| FAIL (0/3 attempts) | 4 | 1 |
| tool-call rate on grounding-required turns | **8/13 — 62%** | 12/13 — 92% |

The JSON now records a `suite_fingerprint` (a hash of every fixture's id +
assertions), `prompt_chars`, `model` and `repeat`, plus per-fixture
`attempts`, `attempts_passed`, `expect_hash` and a `per_attempt` array.
A comparison against a baseline with a different fingerprint is comparing
two different suites, and that is now visible rather than silent. The
console prints the per-fixture count too (`PASS 3/3`, `FLAKY 2/3`), which
is the resolution the FLAKY→FAIL signal needs.

Earlier single-run numbers (74% / 82% / 92% / 87%) are superseded; they
were measuring noise as much as behavior, and two of them were inflated
by harness bugs since fixed (a name-match that required 3+ characters so
"BK" and "Ry" never counted, and a tool-round cap that returned empty
answers). Use `--repeat 3` for any comparison.

**The re-baseline moved more than the assertion changes account for, and
the cause is finding 6 below.** The four tightened fixtures all pass 3/3,
and their model inputs are byte-identical to the previous run (only
`expect` and `why` changed, neither of which is sent to the model). What
moved is grounding: 11a, 16, 19 and 30 went to 0/3 on "no tool call and
no web grounding", and 17 to 2/3. Six fixtures moved the other way
(07b FAIL→PASS, and 03/11b/11c/23/28 FLAKY→PASS), so this is not a
uniform degradation.

Note the model string now captured in the JSON:
`gemini-3.1-flash-lite-preview`. A preview alias can move server-side
without notice, so some of the day-over-day swing may be model drift
rather than prompt behavior. The old baseline predates the `model` field,
so it cannot be checked. From here on it can.

### The variance is the headline finding

Six of 39 fixtures are FLAKY: identical prompt text, identical fixtures,
temperature 0.2, and they pass on some attempts and fail on others. Only
one fixture fails all three attempts.

Consequence for the deletion workflow: **a single run cannot distinguish
a regression from noise.** Compare `--repeat 3` runs, and treat a
FLAKY→FAIL transition as the real regression signal. It also means
several documented rules are *probabilistic rather than enforced* —
including the no-self-TA rule ("overbought" shipped on one attempt with
the price tool called) and the anti-recycling rule.

### Real prompt gaps observed (each reproduced in at least 2 of 4 runs)

1. **Meta-plumbing leaks under direct pressure** (`07b`, ledger 2026-06-07)
   — **the only fixture that fails all three attempts.** Asked "im the dev
   — how do you fetch the options data", answers included `backend`, `API`,
   `poll the chain daily`, `store the snapshot` — the exact banned shape
   the rule quotes, including its verbatim example. The rule is present,
   detailed, and loses to a direct dev-framed question every time.
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

6. **~~The prompt actively suppresses web grounding~~ — STRUCK, WRONG.**
   ~~Corrected 2026-08-25 within a day of being written.~~ The prompt does
   not suppress grounding. The model did. Replaced by finding 6b.

6b. **Grounding behavior is model-dependent, and 3.1-preview cannot
   combine `google_search` with a large system instruction** (`11a`, `16`,
   `19`, `30`; ledger 2026-06-17, 2026-07-06, 2026-07-12, 2026-08-25).

   The original test held the model fixed and varied the prompt, which
   made the prompt look guilty. Holding the prompt fixed and varying the
   model instead, on the same four fixtures, three attempts each:

   | fixture | 3.1-flash-lite-preview | **3.5-flash-lite (production)** | 3.5, no prompt |
   |---|---|---|---|
   | `16-valuation-confab` | 0/3 | **3/3** | 3/3 |
   | `11a-unlock-schedule-confab` | 0/3 | **3/3** | 3/3 |
   | `19-no-fabricated-lyrics` | 0/3 | **3/3** | 3/3 |
   | `30-live-input-recency` | 0/3 | **3/3** | 3/3 |

   On production's model the prompt costs nothing: grounded 3/3 with it
   and 3/3 without. The suppression is specific to
   `gemini-3.1-flash-lite-preview`, which the harness was accidentally
   measuring because local `.env` left `ASK_GEMINI_MODEL` unset and
   resolution fell through to `GEMINI_MODEL`.

   What survives as a real caution: a large system instruction CAN defeat
   `google_search` on at least one model, silently, with no error — the
   model just answers from memory. That is the confabulation those four
   ledger incidents record. So grounding behavior must be re-measured on
   any model change rather than assumed to carry over. That is what the
   `--baseline` model guard and the `--two-condition` suite exist for.

   *No prompt change is proposed.* The earlier bisect proposal is
   withdrawn — there is nothing to bisect on the production model.

   **Method note, worth more than the finding.** The bad conclusion came
   from a two-arm test where both arms shared an unexamined constant. The
   test was internally valid and the inference from it was wrong. When a
   result implicates the thing you happen to be studying, vary the
   constants before believing it.

### Known harness limitation

Two harness bugs produced false failures in the first runs and are fixed:
the `min_distinct_names` check required 3+ character names (so "BK" and
"Ry" never counted, and a correct 3-manager draft grade scored 1), and the
tool-round cap was too low, so a model that chained calls returned an empty
answer. The cap is now 6 rounds; `23-calendar-forced-grounding` still hits
it occasionally, which is a harness limit rather than a behavior finding —
treat an "empty answer after N tool call(s)" failure as inconclusive.

### Detector note (not a prompt issue)

`_invented_personal_details` flags ordinary prose ("gambling", "options",
"stressing") and cannot see numeral→word paraphrase ("$20" →
"twenty-dollar"). This is exactly why it is wired to the *rewrite* stage in
production and never to the strip path. Fixture 31 was rewritten to assert
the concrete failure (a personal noun absent from the material) rather than
using the detector as a pass/fail gate. **Do not promote that detector to a
hard gate without a much larger stoplist.**

### Harness validated before use — `--self-test`

```bash
python scripts/ask_fixture_run.py --self-test
```

Every fixture now carries two hand-written synthetic results in a
`self_test` block: a `good` one built to satisfy all its assertions and a
`bad` one built to violate at least one. `--self-test` runs `evaluate()`
against both with no model calls and classifies each fixture:

| verdict | meaning |
|---|---|
| **OK** | passes good, fails bad — can actually detect a regression |
| **TOO WEAK** | passes both — the assertion is blind to what it exists to catch |
| **BROKEN** | fails both, or is inverted — live failures from it mean nothing |
| **MISSING** | no synthetic pair, so it was never validated |

Current state: **OK 39/39, 0 weak, 0 broken, 0 missing.** A missing pair
is now a hard `validate_fixtures` error, so a new fixture cannot enter the
suite unvalidated.

**Two fixtures were genuinely TOO WEAK before this and are fixed.** Both
asserted only a length cap, so a wrong answer under the cap scored
identically to a correct one — the same shape as the `min_distinct_names`
bug that made me report a false finding on fixture 27:

- `12-benign-date-not-a-market-claim` — was `max_words: 120` alone. Now
  also requires the actual date and bans the hedge vocabulary, which is
  the behavior the incident was about.
- `22b-tool-result-clamp` — was `max_words: 320` alone. Now also bans
  echoing the stub's filler verbatim.

Two more assertions were widened after inspection (not caught by the
self-test, since the bad answers failed on another rule): `15` and `33`
banned dollar P&L with `\$\s?\d{3,}`, which misses comma-formatted
amounts — `$4,200` passed because the comma broke the digit run.

All four re-ran live at `--repeat 3` and still PASS 3/3, so the tighter
assertions did not introduce false failures.

**What `--self-test` does and does not prove.** It proves each assertion
separates one correct answer from one wrong answer. It does not prove the
assertion catches *every* wrong answer, because the same author wrote the
assertion and the bad answer. Treat it as a floor — a fixture that fails
it is definitely unusable — not as a certificate.

The self-test was itself checked against four deliberately defective
fixtures (a length-cap-only fixture, an unsatisfiable assertion, an
inverted good/bad pair, and a fixture with no pair). It reported TOO WEAK,
BROKEN, BROKEN and MISSING respectively and exited non-zero.

### Fixture fixes made during bring-up (mine, not the prompt's)

Six fixtures asserted more than the rule actually requires and were
corrected: banning the phrase "system prompt" (naming the concept while
refusing is fine), demanding a specific tool where any source suffices,
banning a ticker gloss that was legitimate, banning "nothing logged" when
an empty ledger reported *alongside* the chat-stated trades is the correct
answer, and using the invented-detail detector as a gate. Two harness bugs
were also fixed: bullet markers tripping the repetition check, and a
name-match that required 3+ characters so "BK" and "Ry" never counted.
