# NOTES.md

Findings that need an owner decision. Nothing here has been applied.

---

## STANDING RULE 1 — diff the config before believing the finding

> **A measurement that implicates the thing you are studying is more
> likely a config artifact than a discovery. Diff the config against
> production BEFORE writing the finding down.**

This is the project's first standing rule because it has been paid for
four times. Every case had the same shape: the harness resolved a
different configuration than the deployed worker, nothing in the output
said so, and the resulting number looked like a discovery about the
prompt.

### The seven divergences

| # | divergence | what it did |
|---|---|---|
| 1 | `ASK_GEMINI_MODEL` unset locally, resolution fell through to `GEMINI_MODEL` | two baselines measured `gemini-3.1-flash-lite-preview`, a model production never runs |
| 2 | local `GEMINI_MODEL` pinned to a `-preview` alias | the alias moved server-side between runs and read as a prompt regression |
| 3 | `SLEEPER_LEAGUE_ID` unset locally | `lookup_fantasy_league` was never declared, so fixture 27 asserted a tool absent from the request and could not pass for any prompt |
| 4 | `max_output_tokens` 1200 vs production 5000; `temperature` 0.2 vs 0.3 | produced the "empty answer" failures and inflated a 32/39 that was never comparable |
| 5 | suite fingerprint computed over the `--only` subset | made every partial run a false mismatch — a bug in the guard built to catch divergences |
| 6 | `thinking_budget` 0 vs production 2000 | suppressed tool use, which became the withdrawn grounding-cost finding below |
| 7 | no round-cap final-answer rung | "empty answer after N tool calls" was recorded in this file for weeks as an unavoidable harness limitation; it was a production rung the harness lacked |

### The four findings they produced or retracted

1. **Finding 6, "the prompt suppresses web grounding" — RETRACTED.**
   0/3 grounded with the prompt, 3/3 without, on the same tool config.
   The arms shared an unexamined constant: both ran 3.1-preview. On
   production's 3.5-flash-lite the same four fixtures ground 3/3 WITH
   the prompt. Cause was divergence 2, not the prompt.

2. **"Pass rate regressed 32/39 → 31/39" — RETRACTED.**
   The 32/39 was measured at 1200/0.2. Nothing got worse; the earlier
   number described a system nobody deploys. Cause was divergence 4.

3. **"The TOOLS migration costs grounding" — WITHDRAWN.**
   Fixture `19-no-fabricated-lyrics` grounded 8/8 pre-migration and 4/8
   post, reproduced on the pinned production model, and was recorded as
   a measured cost of trading prompt weight for tool-selection noise.
   It was measured with **no thinking budget** while production sets
   2000. With that matched, `19` goes **0/3 → 3/3** and suite grounding
   goes **11/13 → 14/14**. There is no grounding cost to the
   declarations migration. Cause was divergence 6.

4. **"Empty answer after N tool calls is a harness limitation" —
   RETRACTED.** Treated as inconclusive-by-nature and used to excuse
   failures on `08`, `21`, `22a`, `23`. It was divergence 7. With the
   rung added, all of them pass.

5. **"The August Gemini cost is the harness, not the bot" — RETRACTED.**
   Asserted from request counts: production traffic was at its lowest
   month on record, the harness was the only new consumer, so the harness
   must be the cost. The billing data says $46.31 spread evenly across
   Aug 1-26 at ~$1.80/day, starting weeks before any harness run, +12%
   over July. A steady month-long line cannot come from three days of
   activity. Same error as the others: a cause inferred from counts
   without looking at the SKU-level data that would settle it.

Only ONE finding in this class survived contact with a matched config:
the [rule-2 exception](#measured-exception-to-claudemd-rule-2), where
anti-fabrication rules moved into tool schemas measurably weakened.

**What makes the next one cheap:** `config_guard()` asserts 9 keys
against `PRODUCTION_CONFIG` and exits before the first API call;
`config_fingerprint` is recorded in every baseline so a mismatched
comparison is refused; and `ALLOWED_CONFIG_DIFFS` requires a written,
printed reason for any difference that is deliberate.

---

## STANDING RULE 2 — a fix validated only on what motivated it is unvalidated

> **A fix tested only against the fixtures that prompted it has not been
> tested. Validation runs the full suite.**

Two wrong results this week came from exactly this, one of them twice in
a day:

1. **The fixture self-test.** Each fixture's assertions were checked
   against a good and a bad answer *written by the same author who wrote
   the assertions*. It proved each assertion separates those two
   answers, and nothing more. It was recorded at the time as a floor
   rather than a certificate, which was correct — but the same shape
   then reappeared without that caution.

2. **The round-cap rung.** Added to fix "empty answer after N tool
   calls", tested on fixtures `08` and `27`, both went 3/3, declared
   fixed. The next full 42-fixture run still had **four** empty-answer
   failures. The rung resent `contents` unchanged; production appends an
   `[ANSWER NOW]` turn telling the model its tool budget is spent.
   Without it the model simply requests another tool and returns no
   text — which only shows up on fixtures whose tool chains differ from
   the two it was tuned against.

The mechanism is the same both times: a fix derived from N cases and
tested on those same N cases is a tautology. The cases that would falsify
it are, by construction, the ones nobody looked at.

**Consequence.** A change to anything that touches how a turn terminates,
what is declared, or what is sent — the ladder, the tool list, the
generation config, the round loop — is validated by a full `--repeat 3`
run and nothing less. Spot-checking the motivating fixtures is a smoke
test to see whether it is worth running the suite at all, never evidence
that it worked.

Corollary, learned the same day: **do not certify a divergence as
harmless.** The grounding-skip experiment was reverted rather than
measured, because "we measured it and it was fine" is how "empty answers
are a harness limitation" survived for weeks.

---

## 2026-08-25 — /ask fixture harness: baseline and prompt gaps

`scripts/ask_fixture_run.py` + `tests/ask_fixtures/` (39 fixtures, all 25
INCIDENT LEDGER dates covered plus 3 August incidents). **`ask_prompt.py`
was not modified — zero characters.** Everything below is a proposal.

### Baseline — authoritative run

**`docs/ask-baseline-3.5-92a8ff2.json`, `--repeat 3`, on
`gemini-3.5-flash-lite` — the model production actually runs.**

| metric | 3.5 (authoritative) | f9bae39 INVALID | 01f124a INVALID |
|---|---|---|---|
| model | **gemini-3.5-flash-lite** | 3.1-flash-lite-preview | unrecorded |
| PASS (3/3 attempts) | **32/39 — 82%** | 31/39 | 32/39 |
| FLAKY (1-2 of 3) | 5 | 4 | 6 |
| FAIL (0/3) | **2** | 4 | 1 |
| tool-call rate, grounding turns | **13/13 — 100%** | 8/13 | 12/13 |

**Both earlier baselines are marked `invalid_for_deletion_evidence` in
their own JSON and the runner refuses to compare against them.** They were
measured on `gemini-3.1-flash-lite-preview`: local `.env` left
`ASK_GEMINI_MODEL` unset, so resolution fell through to `GEMINI_MODEL`,
while Railway sets `ASK_GEMINI_MODEL=gemini-3.5-flash-lite`. Their numbers
describe a model no user reaches. `.env` now matches Railway, and the
runner no longer reads the model from the environment at all —
`HARNESS_MODEL` is pinned in the file and overridable only with `--model`.

Grounding on the production model is **13/13, not 8/13**. Every fixture
that looked like a confabulation failure was an artifact of the wrong
model. The two genuine failures are `07b-no-meta-plumbing` and
`27-group-scope-answer`, both 0/3.

The JSON records `suite_fingerprint`, `prompt_chars`, `model`,
`model_pinned_in_runner`, `model_versions_seen` (the server-returned
build, since the requested string is a movable alias), `repeat`,
`ran_subset` and `compared_against`; per fixture it records `attempts`,
`attempts_passed`, `expect_hash` and a `per_attempt` array. `--baseline`
refuses to compare across a model change, an assertion change, or an
invalid baseline without an explicit override.

### Two-condition test: is the prompt fighting tool routing?

Every fixture run twice on `gemini-3.5-flash-lite`, once with the system
prompt and once with none, recorded under `two_condition` in the baseline
JSON. "Sourced" means the first turn either grounded or called a tool.

| outcome | count |
|---|---|
| same with and without the prompt | **33 / 39** |
| prompt suppressed sourcing | 6 |
| prompt induced sourcing | 0 |

**The raw 6 overstates it.** Five of the six are turns where *not*
sourcing is the correct behavior — the prompt teaches the model to answer
from the injected profile and chat blocks instead of re-fetching what it
already has:

| fixture | grounding required? | tool the no-prompt arm reached for |
|---|---|---|
| `03-sustained-clapback-rotation` | no | `lookup_user_profile` |
| `07b-no-meta-plumbing` | no | `lookup_options_chain` |
| `18-personal-color-beats-pnl` | no | `lookup_user_profile` |
| `29-praise-is-not-an-attack` | no | `lookup_market_price` |
| `32-quote-is-not-biography` | no | `search_chat_messages` |
| **`27-group-scope-answer`** | **yes** | `search_chat_messages` |

On a two-word "Good boy" (`29`) the no-prompt arm calls
`lookup_market_price`. That is the prompt working, not fighting.

**The real delta is one fixture.** On grounding-required turns the split
is **12/13 with the prompt, 13/13 without**, and the single loss is `27`,
which is also one of the two hard FAILs: asked to grade the fantasy
draft, it never calls `lookup_fantasy_league`. It reaches for
`query_data` and `search_chat_messages` instead, so it counts as
"sourced" in the aggregate tool-call metric while calling the wrong tool
entirely — worth knowing that the 13/13 headline hides a wrong-tool case.

Read against the 3.1-preview result, where the prompt cost 4 of 13
grounding-required turns outright, the production model shows **no
general tension between the prompt and tool routing.** One fixture picks
the wrong tool. That is a routing bug in one place, not a systemic effect,
and nothing here has been changed in response — it is the owner's call
whether it moves the tool-schema migration up the queue.

### The variance is the headline finding

Five of 39 fixtures are FLAKY on the production model: identical prompt
text, identical fixtures, temperature 0.2, and they pass on some attempts
and fail on others (`01`, `03`, `11c`, `28`, `29`). Two fail all three.

Consequence for the deletion workflow: **a single run cannot distinguish
a regression from noise.** Compare `--repeat 3` runs, and treat a
FLAKY→FAIL transition as the real regression signal. It also means
several documented rules are *probabilistic rather than enforced* —
including the no-self-TA rule and the anti-recycling rule.

### Real prompt gaps observed

**Re-measured on `gemini-3.5-flash-lite` — most did not survive.** The
list below was written against 3.1-preview. Status on the production
model, from `docs/ask-baseline-3.5-92a8ff2.json`:

| # | fixture | on 3.1-preview | **on 3.5 (production)** |
|---|---|---|---|
| 1 | `07b` meta-plumbing | FAIL 0/3 | **FAIL 0/3 — survives** |
| 2 | `10` trade-outcome | failing | **PASS 3/3 — gone** |
| 3 | `03` clapback recycling | FLAKY | **FLAKY 1/3 — survives, worse** |
| 4 | `25a` price contradiction | failing | **PASS 3/3 — gone** |
| 5 | `24` repetition | FLAKY | **PASS 3/3 — gone** |
| — | `27` group-scope | PASS | **FAIL 0/3 — new** |

Only findings 1 and 3 are real on the model users hit. Findings 2, 4 and
5 were model artifacts and their code-migration proposals should not be
acted on without re-deriving the evidence. `27` is new and is described
in the two-condition section above.

The unedited original text follows, kept because the proposals in 1 and 3
still stand.


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

### TOOLS migration: the 7,000-char target and the keep-list conflict

Per-tool documentation moved from the prompt's `## TOOLS` section into
`discord_bot/tool_docs.py`, prepended to each FunctionDeclaration at
build time. **TOOLS 18,735 -> 9,204 chars; prompt 64,090 -> 54,559.**

The 7,000-char target was reached (6,909) and then deliberately given up,
because measuring it showed two of the moves were regressions:

| moved rule | fixture | result when moved to the schema |
|---|---|---|
| ZERO UNFORCED TRADE-OUTCOME ASSERTIONS | `10` | 3/3 -> **1/3**, "expired worthless" shipped twice |
| NO SELF-GENERATED TECHNICAL ANALYSIS | `11c` | 2/3 -> **0/3** |

Both are anti-fabrication rules, which the brief said to keep in the
prompt. They were moved only to fit the char target, and the fixtures
priced that decision immediately. Restored, both recover (`10` 3/3,
`11c` 3/3) and TOOLS lands at 9,204.

**The two constraints are arithmetically incompatible.** What the
keep-list requires, at current sizes: date-locked lines 4,007 + routing
priority 416 + Google-is-default 700 + code execution 1,430 +
trade-outcome 891 + no-self-TA 1,286 + headings ~470 = **~9,200**. Under
7,000 is only reachable by dropping the code-execution block (1,430,
which has no declaration to move into — Gemini's built-in sandbox tool
takes no description) or by re-moving an anti-fabrication rule the
measurement says is load-bearing. *Owner's call; nothing further changed.*

### MEASURED EXCEPTION to CLAUDE.md rule 2

CLAUDE.md's prompt-enforcement policy, rule 2, says tool mechanics belong
in the tool declaration rather than the system prompt. That is correct
for mechanics. **It does not hold for anti-fabrication rules, and the
fixtures priced the difference immediately.**

Both rules below were moved from the prompt into the relevant tool
schemas during the TOOLS migration, changing nothing else:

| rule | fixture | in the prompt | moved to the schema |
|---|---|---|---|
| ZERO UNFORCED TRADE-OUTCOME ASSERTIONS | `10` | 3/3 | **1/3** — "expired worthless" shipped on two of three attempts |
| NO SELF-GENERATED TECHNICAL ANALYSIS | `11c` | 2/3 | **0/3** |

Restored to the prompt, both recover: `10` to 3/3 and `11c` to 3/3, and
suite pass rate goes 30/39 to 34/39.

**The reason is what the rule governs, not what it mentions.** A schema
description is read while the model is CHOOSING a tool. An
anti-fabrication rule does not govern that choice — it governs what the
model may write once the result is in hand, which happens after tool
selection is over and the declaration has stopped being the salient
context. A rule about composing the answer has to live where the answer
is composed.

The dividing line, for future migrations:

- **Schema** — parameter semantics, status codes, usage shapes, examples,
  per-tool when-to-call. Anything that helps PICK the tool.
- **System instruction** — what you may and may not ASSERT once you have
  the data. Anti-fabrication, provenance, and outcome discipline.

Rule 2 stands for mechanics. This exception is measured, not argued, and
should be cited before anything else is moved out of the prompt.

### Harness bugs found and fixed (none of these are "limitations")

- `min_distinct_names` required 3+ character names, so "BK" and "Ry"
  never counted and a correct 3-manager draft grade scored 1.
- The tool-round cap was 4, below production's 6. Now 6, matching.
- **"Empty answer after N tool call(s)" — no longer a limitation.**
  This section previously said to treat it as inconclusive by nature.
  That was wrong for weeks and excused real failures on `08`, `21`,
  `22a` and `23`. Production, on ending a round with tool calls but no
  text, makes one more call with the data-fetching tools withheld (code
  execution kept) to force text out. The harness returned `""`. It was
  divergence 7 in STANDING RULE 1, not a property of the harness. With
  the rung added, every fixture that failed this way passes.

The lesson generalises: **"known limitation" is where unexamined
divergences go to be forgiven.** Before writing that phrase again, check
whether production has a rung the harness lacks.

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
