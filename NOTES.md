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

### Where the Gemini money actually goes (Aug 2026, SKU-level)

$46.31/month, and the shape matters more than the number:

- **$31.47 (68%) — gemini-3.1-flash-lite, declining.** PDF ingestion
  (37.3M input tokens recorded in `pdf_analyses`) plus the trade
  classifier. Steady, and shrinking as PDF volume falls.
- **$13.47 (29%) — gemini-3.5-flash-lite, growing 166%.** The `/ask`
  path and profile refresh. This is the whole month-over-month increase.
- **$1.03 — embeddings.** Flat.
- **$0.00 — grounding.** 686 free search queries.

**The clearest waste, though it is not the largest line:** the trade
classifier runs on every message with any text — the only gate is that
the string is non-empty ([analyst_log/ocr.py](analyst_log/ocr.py)). In
August that was **42,514 Gemini calls producing 41,628 rows marked
`is_trade=0`** against **936 real trades**: a 97.8% miss rate, on
captions like "theres a giant onion" and "im not free till sunday".
Roughly 20M input tokens (~$5) to find 936 trades, and 41.6k junk rows
in a table `query_data` reads and whose tool docs already warn it is
wins-biased.

A regex pre-filter (cashtag, strike notation, calls/puts/entry/filled
vocabulary, a date, or a decimal) keeps 3,388 of August's 46,060
messages — **93% fewer calls**, with all 936 real trades inside the
retained 7%. Worth doing for the data quality regardless of the money.

The bigger *cost* lever is the 3.5 side: profile refresh runs 4x/day
(03/09/15/21) over all channels with `profile_sample_size=500`. Nobody
has measured its token draw. That is the number to get before touching
anything else.

### The nine divergences

| # | divergence | what it did |
|---|---|---|
| 1 | `ASK_GEMINI_MODEL` unset locally, resolution fell through to `GEMINI_MODEL` | two baselines measured `gemini-3.1-flash-lite-preview`, a model production never runs |
| 2 | local `GEMINI_MODEL` pinned to a `-preview` alias | the alias moved server-side between runs and read as a prompt regression |
| 3 | `SLEEPER_LEAGUE_ID` unset locally | `lookup_fantasy_league` was never declared, so fixture 27 asserted a tool absent from the request and could not pass for any prompt |
| 4 | `max_output_tokens` 1200 vs production 5000; `temperature` 0.2 vs 0.3 | produced the "empty answer" failures and inflated a 32/39 that was never comparable |
| 5 | suite fingerprint computed over the `--only` subset | made every partial run a false mismatch — a bug in the guard built to catch divergences |
| 6 | `thinking_budget` 0 vs production 2000 | suppressed tool use, which became the withdrawn grounding-cost finding below |
| 8 | no repetition retry-then-strip rung | fixtures 24/01 scored raw output at a stage where production strips; `01` now passes 3/3 with `guard=stripped` |
| 9 | **local Python 3.10 vs container Python 3.12** | a mid-pattern `(?i)` warned locally and raised `re.error` in the container, crash-looping the live bot for 7 minutes. Divergences 1-8 are about what gets SENT; this one is what the code RUNS ON |
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

5. **"The August Gemini cost is the harness / it's grounding quota" —
   RETRACTED, both halves, by SKU-level billing data.**

   Asserted from request counts and from the CLAUDE.md note that
   grounded prompts bill at ~$14/1000 past a free tier. Neither claim
   survived the actual invoice:

   | SKU | tokens | cost | vs prev |
   |---|---|---|---|
   | 3.1-flash-lite input text | 91.9M | **$22.97** | -12% |
   | 3.5-flash-lite input text | 32.1M | **$9.62** | +166% |
   | 3.1-flash-lite output text | 4.7M | **$7.00** | -10% |
   | 3.5-flash-lite output text | 1.2M | **$2.98** | +131% |
   | 3.1-flash-lite input image | 5.3M | $1.32 | +17% |
   | gemini-embedding-001 | 6.9M | $1.03 | +2% |
   | 3.5-flash-lite CACHED input | 29.2M | $0.87 | +691% |
   | **search query gemini 3 — FREE** | **686** | **$0.00** | 0% |

   **Grounding cost $0.00.** 686 search queries all month, billed free.
   The grounding-quota theory was wrong, and the grounding-skip built on
   it would have saved nothing — it was a config divergence bought for
   zero dollars. Removed by owner call before it was ever measured, which
   was the right instinct for the right reason.

   **The total is not the harness.** The 3.1 family is **$31.47 (68%)**
   and it is DECLINING (-12% / -10%): that is PDF ingestion plus the
   trade classifier, flat since long before any harness run, matching
   the level ~$1.80/day line from Aug 1.

   **The month-over-month INCREASE is the 3.5 family**, +$8.45, offset by
   3.1 falling -$3.80, netting the reported +$4.86. Everything on 3.5 is
   the `/ask` path and profile refresh — and the harness runs that exact
   path on that exact model, ~1,006 turns this week. So the original
   claim was wrong about the bill and partly right about the delta. State
   deltas and totals separately; they have different causes.

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

## STANDING RULE 4 — a green suite measures the assertions, nothing else

> **Absence of a failing test is not absence of a problem. A pass rate
> reports what the fixtures assert and is silent on everything else.**

Every case below was green at the time:

| what was green | what was actually true |
|---|---|
| "empty answer after N tool calls" logged as a harness limitation for weeks | production has a round-cap rung the harness lacked; four failures were artifacts |
| fixture 27 failing "because the router can't reach the tool" | `lookup_fantasy_league` was never REGISTERED locally — the fixture asserted a tool absent from the request |
| two baselines, 82% and 92% | measured on a model production never runs |
| class 3 shipped at 41/42, best suite result yet | it was stripping `$3 parking receipts`, a BTC holdings count, revenue and open interest out of correct answers |
| class 1 shipped and deployed | it was stripping **the refusal its own rule prescribes** — "I only have the current snapshot" flagged on the word `snapshot` |
| fixture `07a` recorded a PASS | the answer said "open interest down 10.7% **over the past 5 days**" — a textbook violation of 07a's own rule. Its regex was markdown-blind, so the fixture certified its own violation as correct |

The last two are the sharpest: in both, the suite went UP while the
validator deleted correct content. The fixtures did not assert the
deleted sentences, so nothing turned red.

**What this buys.** A pass rate answers "did the things I thought to
check still work". It cannot answer "is this change harming things I did
not think to check". That second question needs a different instrument —
here, `scripts/validator_sweep.py` over the recorded corpus, plus
negative fixtures that assert what the validator must LEAVE ALONE.

---

## STANDING RULE 3 — an erroring gate is a failed gate

> **A check that raises instead of returning a verdict has FAILED. It is
> never "inconclusive", never skipped, never a pass.**

Violated twice on 2026-08-26, hours apart:

- The deprecation guard written to prevent the outage shipped with a
  literal newline inside a string, so `smoke_ask_prompt_diet.py` could
  not parse and the gate could not run at all. A gate that cannot run
  guards nothing, and its silence is indistinguishable from success.
- `DeprecationWarning: Flags not at the start of the expression` printed
  in harness output for hours before the outage — the signal that
  predicted it exactly — and was read past on every run.

Enforced in `scripts/preflight_push.py`: every check is wrapped and an
exception inside one is recorded as a FAILURE naming it, while
`check_gates_are_runnable` asserts the diet smoke renders a verdict
rather than a traceback.

---

## DETERMINISTIC FIRST — the strongest evidence in the project

A 788-char prompt block that quoted the violating sentence almost
verbatim caught **none** of the seven violations it was written to
prevent. Three regexes caught **all seven**, with zero false positives.

| enforcement | caught | false positives |
|---|---|---|
| NEVER META-NARRATE, 788 chars of prose | **0 / 7** | — |
| `check_meta_plumbing`, three detectors | **7 / 7** | **0** |

Both columns are measured on the same set: every `07b` answer recorded
across every run, each produced while the full prompt block was in
force. The block did not merely fail to help — it named the exact shape
never to repeat, and the model reproduced that shape while reading it.

This is the evidence base for CLAUDE.md rule 1. When a rule is
checkable, prose is not enforcement; it is a description of the
enforcement someone still has to write.

---

## SESSION TEMPLATE — moving one rule class from prompt to code

Required steps, in order. **Step 3 is not optional** — it was missing
from the Session 5 spec, which is how one class briefly ended up
enforced more weakly than before it started: the prose was deleted while
`validate()` was still an unwired module.

1. **Build the detector** in `scripts/ask_response_validate.py` as a
   `check_<class>()` returning `Violation`s. Add it to `_CHECKS`.
2. **Prove it on recorded answers** — every logged violation of that
   class from the baseline JSONs, plus the correct answers that must NOT
   fire. Extend `_BAD` / `_GOOD` so `--self-test` covers both directions.
2b. **SWEEP FOR FALSE POSITIVES** — `python scripts/validator_sweep.py
   --rule <class>`. Report BOTH numbers: catch rate against the class's
   fixtures, and false-positive rate against the recorded corpus. **No
   prompt prose is deleted until false positives are ZERO.**

   This step exists because class 3 passed every fixture, raised the
   suite to 41/42, and was silently stripping correct sentences the
   whole time. The reference process is its 17 -> 5 -> 2 -> 0:

   | cut | flags | what the sweep exposed |
   |---|---|---|
   | first | 17 | `$3` parking receipts, a BTC holdings count, revenue, open interest |
   | +price cue, exclusions | 5 | quantities still read as levels |
   | +unit exclusion | 2 | markdown `**818,000**` broke the unit match |
   | +markdown tolerance | **0** | one flag left, a genuine catch |

   A flag on an answer the fixture ACCEPTED is the signal. When such a
   flag is genuinely a violation the fixture does not assert, add it to
   `REVIEWED_TRUE_POSITIVES` with the reason — never to silence a flag
   nobody read.

3. **WIRE IT INTO THE SEND PATH** in `bot.py` via
   `resolve_violations()`. Never write a second copy of the ladder — one
   decision function, or production and the harness drift.
4. **Only then delete the prompt prose**, leaving at most one line
   naming the behavior. Repoint the diet smoke's concept anchor at it.
5. **Gate it** — the validator's `--self-test` is already a smoke check,
   so a new class inherits it.
6. **Measure** — the class's fixture at `--repeat 3`, then the FULL
   suite against the current baseline (STANDING RULE 2). Report chars
   removed.
7. **RUN THE PUSH GATE.** `python scripts/preflight_push.py` must exit 0
   before ANY push to the deploy branch. Required, not advisory: a push
   auto-redeploys the live bot, and one took the worker down for seven
   minutes on 2026-08-26. No override flag by design.

Steps 1-3 are additive and safe to land alone; step 4 is the only
destructive one and must never precede step 3. Step 7 gates the push
regardless of which steps ran.

---

### Class 1 production impact: measured, not estimated

**Zero answers affected.**

The meta-plumbing validator was live in production with 17 known false
positives for roughly four and a half hours — wired at 07:54 PDT
(`200c328b`), fixed at 13:23 PDT (`67aabf1b`). During that window
production served **15 `/ask` turns** (12:32–19:55 UTC).

The published ask-log records the guard list on every turn, so this is
countable rather than inferable. Every one of the 15 reads
`guards: —` except 13:41:04, which reads `guards: repetition` — the
pre-existing detector, not this validator. **No turn shows `validate:`
or `validate-strip`.** The validator never fired on a real answer.

Why: all 15 turns routed `LOCAL/BANTER`. The FP-prone vocabulary
(`snapshot`, `feed`, `index`) lives in options and data questions, and
none were asked in that window. The exposure was real; the harm was
zero, by luck of traffic mix rather than by design.

Stated separately from the 17 -> 0 number on purpose. "17 false
positives found and fixed" describes the DETECTOR. "0 answers affected"
describes the BLAST RADIUS. Conflating them would have let a fix report
sound like a damage report.

**Worth keeping:** `ask-logs/YYYY-MM-DD.md` on the `pulse-data` branch
carries `guards:` per turn, which makes "did a guard mangle a real
answer" an answerable question after the fact. That is the only
production-side instrument in this system that can answer it.

### Class 2 is well-scoped, not broken — and unvalidated in production

The sweep reported **0 true positives and 0 false positives** across 591
answers. That is ambiguous on its face: a detector that never fires is
either well-scoped or dead, and the sweep cannot tell you which.

Synthetic proof it fires:

| answer | tools called | verdict |
|---|---|---|
| "CPI came in at 3.1% headline, hotter than the 2.9% consensus" | none | **FIRES** |
| "core PCE printed 2.6% year over year" | none | **FIRES** |
| "CPI ran 3.1%" | `lookup_market_price`, `search_chat_messages` | **FIRES** |
| "core PCE at 2.6%" | `lookup_economic_calendar` | silent (correct) |

So the detector works. The zero is real: **no recorded answer states a
macro figure without the calendar tool**, because the fixtures that ask
macro questions reliably call it. The rule it enforces has not been
violated since the corpus began.

**Status: UNVALIDATED IN PRODUCTION.** It has never fired on a real
answer, only on synthetic input. That is not a reason to remove it — the
prose it replaced had a worse record — but it is a reason not to count
it as proven. If it never fires in another month of traffic, the honest
read is that the 2026-06-05/06-08 incidents were fixed by the routing
rule rather than by this backstop.

### Disposition: the undated incident narratives

**They move to the docstring ledger.** Picking, because the current
state is the worst of both: the date gate enforces the letter while the
same narratives walk back in without timestamps.

Four remain in the prompt body, all undated:

| narrative | lives in |
|---|---|
| "the observed $GEO dodge: asked when GEO reports, answered with old results" | Google-is-default |
| "observed inventions: a '$27 breakout of consolidation zone,' 'as long as ES holds 7293,' '$NOW breaks $115,' **'RSI creeping toward overbought'**" | NO SELF-GENERATED TECHNICAL ANALYSIS |
| "SPCX failure: three different invented tranche schedules" | confabulation ban |
| "'how does MSTR make money' shipped from memory asserting a premium that had compressed to parity" | live-input recency |

**Why not "they earn their chars as concrete anti-patterns".** That was
the better argument until this project measured it. DETERMINISTIC FIRST
records the answer: a 788-char block quoting the violating sentence
almost verbatim caught **0 of 7** violations, while three regexes caught
7 of 7. Quoting the shape does not prevent the shape.

The NO SELF-TA list is the direct test, not an analogy. It quotes
**"RSI creeping toward overbought"** verbatim, and `11c` has spent this
week shipping "RSI reading" and "overbought". The model produced the
exact phrase the prompt names as forbidden — the 07b result reproducing
in a second rule. Concrete anti-patterns are not enforcement; they are a
description of enforcement someone still has to write.

**Consequence for the gate.** `test_no_incident_dates_in_prompt` checks
for `20\d\d-\d\d-\d\d` and nothing else, so it cannot see any of the
four. A narrative gate — "observed", "the exact shape", "shipped
from memory", quoted violating sentences — is the honest version of that
check. Until it exists the date check is a proxy that measures
timestamps rather than narrative.

**Not executed in the same run as the six structural prompt edits.**
`11c` is the fixture directly at risk from deleting the NO SELF-TA
examples, and it has been the most volatile fixture in the suite. Per
STANDING RULE 2 that deletion gets its own run so a regression is
attributable to it. It is the next queued prompt change, with the
expected outcome stated in advance: if `11c` holds, the examples were
decoration; if it drops, they were load-bearing and the disposition
flips for that one block.

### Validator class queue

Order is by ledger weight, not by discovery order.

| # | class | status |
|---|---|---|
| 1 | meta-plumbing | shipped, prose deleted |
| 2 | macro print figures | shipped, prose deleted |
| 3 | **unforced PRICE assertions** | **next** — largest of the three remaining ZERO UNFORCED blocks, most ledger incidents behind it |
| 4 | unforced MARKET-DATA assertions | queued |
| 5 | unforced TIME-SERIES claims | queued |
| 6 | mid-answer clause restatement | **WITHDRAWN — no such failure class** |

**Class 6 — BUILT, SWEPT, AND WITHDRAWN THE SAME DAY.**

The case for it was fixture `24` failing on repeated 4-word phrases
mid-answer. Building the detector and sweeping it first, as step 2b now
requires, showed the case was wrong.

The answer `24` failed on:

> if dealer gamma is **positive**, they are basically speed bumps — they
> sell when the market spikes and buy when it dips.
> if dealer gamma is **negative**, they become gas pedals — they have to
> chase the market higher.

That is not a loop. It is good explanatory prose using deliberate
parallel construction, and it is exactly what this bot should write.

**The defect was the FIXTURE.** `no_repeated_phrase: 4` cannot tell
parallel explanation from repetition. Raised to 5 on `24` and `01`:

| n | parallel explainer | real loop ("dealers have to buy shares" x3) |
|---|---|---|
| 4 | **flagged** (wrong) | flagged |
| 5 | clean | **flagged** |

Precision bought at some recall, deliberately and recorded.

The detector itself, tuned to zero false positives, caught **one** thing
across 634 answers — and the seven "true positives" it lost on the way
were all legitimate parallel construction: macro revisions, chain
listings, unlock tranches, quoted lyrics. A validator with no
demonstrated real violation should not ship; it can only cost. The code
stays in `ask_response_validate.py`, unregistered, as the record of what
was tested and why it was rejected.

**The starting corpus was the tell.** Tail-scoping the repetition class
released 18 mid-answer candidates that looked like class 6's evidence.
Sixteen were parallel structure. A corpus assembled by relaxing another
detector is not evidence of a new failure class — it is the other
detector's false positives, wearing a new label.

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

## 2026-09-01 — End-to-end code review: what shipped

Review at 5c4b4c23 (report: claude.ai artifact "Report Bot Code Audit").
Everything below P0 was implemented the same day. P0 (member data on the
public repo: `pulse-data` profile snapshots and ask logs, root dumps,
fixture snowflakes) is UNTOUCHED pending the owner's choice between
going private (move web fragments out) and purging history.

P1 — one SQLite connection shared across threads: per-thread connections
(`db.get_connection`), schema once under a lock, `busy_timeout`, tests
in `tests/test_db_threading.py` (8 threads x 25 commits, 200 rows).

P1 — Dropbox cursor reset: caught in `list_new_files`, re-lists from
scratch with a `dropbox_modified_at` floor, pages ops. Tests in
`tests/test_dropbox_reset.py`.

P2 — heartbeat workflow against `/healthz`; `==` pins from the prod
freeze plus `requirements.lock`; `discord_bot/ask_tools.py` (34 nodes,
2.1k lines out of bot.py, every name re-exported); smoke manifest with
tiers and `scripts/run_smokes.py` in preflight; tests for tool_docs,
ingestion_feed, page_selector; ops alerts moved to
`discord_bot/ops_alert.py` with a sync variant for threads.

P3 — CLAUDE.md drift, `.env.example` now generated
(`scripts/gen_env_example.py`), `test_pulse.py` NameError,
`hmac.compare_digest` on the API token and command password, 14 unused
imports removed. 13 smokes that pinned pre-diet /ask prompt text were
retired function-by-function (3 whole files are stubs).

NOT done, deliberately: the `_answer_with_gemini` phase split and the
`db.py` split. Both are multi-day changes on the live bot and belong in
a quiet week as their own session, not at the end of a day that already
changed the DB model and the tool layer.

Omnicalendar review (same day): AVGO ($1.7T, AMC 9/2) came back
unpriceable because the pricer chose the expiry ON the report date,
which settles before an after-close print. Expiry choice is now
session-aware. BF.B returned an empty chain because Yahoo wants BF-B.
A warm-cap name with no logo row never fetched a logo (AVGO rendered
bare); shown names missing both now get one bounded profile fetch. The
both-feeds-down notice posted to the pulse channels, not the calendar
channel. Design note, not built: the sheet renders at 00:00 UTC, the
least complete moment of Finnhub's day; a 12:30 UTC refresh that
re-posts when the confirmed set changed would remove the whole class
that the $5B cap floor only patches.

## 2026-09-01 — /ask pipeline split into phases (verbatim)

`_answer_with_gemini` was 3,145 lines in one try block. It is now ~380
lines calling eleven `_ask_NN_*` phase functions in order. The split is
mechanical and verbatim: an AST pass partitioned the try body into
blocks of at least 150 lines at statement boundaries, computed each
block's true inputs (locals read before written, evaluation-order
aware) and outputs (locals later blocks read), and emitted one async
function per block with those as parameters and return tuple. The one
early return (budget refusal in phase 2) rides out as `_AskEarly`.
Reconstructing the original body from the phases and diffing it showed
exactly one differing line: the closing paren of that wrapper.

Verified: 232 unit tests, 49/49 fixtures self-test, fixtures 03 and 49
live through the real model, fast smoke tier (157), pyflakes clean.

Found while doing it: fixture 49 failed LIVE before and after the
split, for the same reason: with `lookup_earnings_slate` declared, the
model still answered "who reports after close" from chat search plus
Google. Fixed deterministically (slate prefetch in phase 2, mirrored
in the harness). The harness's production fingerprint moved from 10 to
11 function tools because production did.

## 2026-09-01 — db.py split by subject (facade kept)

db.py was 6,326 lines. Now ~1,500 lines of core (connection model,
schema, migrations, shared helpers) plus six subject modules under
db_parts/ (pdf 46 fns, chat 31, pulse 25, analyst 20, summaries 8,
ask 4). Classification by the tables a function's SQL touches; helpers
with no SQL follow their single caller.

Verbatim moves with one deliberate rewrite: inside a moved body every
reference to a db.py function reads `_db.<name>`, so monkeypatches on
the facade (two smokes replace `db.get_connection`) and the
thread-local connection model keep working. Nothing outside db.py
changed. Verified with the full unit suite, the fast smoke tier,
pyflakes on all seven files and the container import gate.

## 2026-09-01 — Gate bypass incident (mine) and the repair

Two pushes (f04712ad, ea74d04d) went out with the pre-push gate
FAILING. The ad-hoc shell chain was `preflight | tail -2 && git push`:
the `&&` tested tail's exit code, not the gate's. The same pipe hid
two earlier fast-tier runs that had actually failed. The gate itself
was right both times: 41 wiring smokes read
`inspect.getsource(bot._answer_with_gemini)` and the phase split had
moved the strings they pin into the phase functions; one smoke patched
`db.SLEEPER_DUMP_MIN_ROWS`, which the db split had imported by name.

Behaviour was never wrong (unit suite, fixtures, live runs were read
from their output, not their pipe status), but the process was.

Repair: `bot._ask_pipeline_source()` returns the caller plus all
eleven phases and the 45 pins now read it; constants read inside
db_parts/ go through `_db.<CONST>` like functions do, so facade
patches keep working; a local `.git/hooks/pre-push` runs the gate and
refuses the push on non-zero, so a shell mistake can no longer skip
it. Rule for every future chain: never put the gate behind a pipe.

## 2026-09-01 — Shadow pilot build session 2 (pieces 5-8)

Shadow editor (13:55 UTC), frozen graders (17:00 UTC, two agents per
dimension), the grader separation gate with seeded known-bad fixtures,
the scoreboard, the runbook. All on GitHub Actions against the
pilot-data orphan branch; production untouched.

Two things the dry run taught before anything ran live: the ledger's
bank-dedup must not drive the pack order (a bank's second card on the
same name landed at the end of the pack), and the citation verifier
needs word-bounded bank names ("ing" matched "holding") and must not
treat calendar years as figures.

Deviations recorded in RUNBOOK.md: shadow omits WHAT TO WATCH as well
as RECAP (no live data on the runner); grader fixtures are frozen in
this repo beside the prompts, with the gate verdict copied to the data
branch; metric 5 comes from ops/<date>.json written by the readers.

Next: PILOT_PUBLISH_ENABLED on, two shakedown days, run the grader
gate from the Actions tab until every dimension separates, then commit
pilot/DAY1.

## 2026-09-02 — Shadow pilot shakedown day 1 (uncounted)

What flowed: 9 HIGH documents published to pilot-data overnight and
through the morning; readers produced cards with anchors verifying at
100% on every completed read (51/51, 67/67, 50/50, 43/43, 40/40,
32/32, 28/28, 22/22, 6/6).

What broke, and the fix for each:

1. GitHub's cron dropped most schedules. The 30-minute heartbeat fired
   twice all day, the readers' 09-14 UTC hourly window fired once, and
   the 13:55 editor never fired (zero runs on an active workflow).
   Fix: github_bridge/workflow_dispatch.py dispatches the pilot
   workflows from the worker's APScheduler; gated on
   PILOT_DISPATCH_ENABLED. It needs Actions: read and write on the
   worker's GITHUB_TOKEN (the current fine-grained PAT answers 403,
   probed from the container). Until the owner extends the token, the
   missed steps were run locally with the same scripts, and the
   artifacts committed to pilot-data by hand. Runbook updated.
2. Reader failure rate 40% on the 13:12 run (2 of 5). Reproduced
   locally: a 432 KB JPMorgan strategy deck exhausted the 12-turn
   budget reading itself and returned "Reached max turns" instead of
   JSON. With 30 turns it produced 43 cards, all verified. Fix: the
   readers workflow scales the turn budget with document size (12 /
   20 / 30 at 150 KB / 300 KB).
3. Metric 1 was meaningless as built. Cards carried no reader topic
   label, so the ledger's soft key was the claim's leading clause and
   the grouping grader measured 48% fragmentation by construction.
   Fix: readers now emit `topic` (reader.md), the ledger groups on it
   with a claim fallback for older cards. This is a reader-prompt
   change made during shakedown, before the freeze.
4. The citation verifier flagged "Nasdaq 100" and "Russell 2000" as
   figures; index names are now excluded. The shadow pulse's 3
   residual failures were 2 of those plus one real one.
5. The production-arm fidelity grade exhausted 30 turns tracing 15
   sentences through nine source files. Graders now get 60.

Grader separation gate: run locally, every dimension separates on the
first try (bad brief: 3 material distortions vs 0; bad sentences: 1
distorted plus 1 unsupported vs 0; bad ledger: 33% fragmented vs 0;
bad mechanism: not preserved vs preserved). Verdict recorded on
pilot-data under grader-gate/.

Shadow pulse for the day: 1,899 words, MAIN EVENT plus nine briefs,
99 card citations covering 94 of 124 cards, edge-quintile share 39%
(no attention flag), 11 lean lines, unread at edit 6 (the backlog the
dropped reader runs left; all 9 documents were read by end of day).

Day-1 grades (uncounted, two agents each): shadow fidelity 93% / 93%
with zero unsupported and one distorted sentence; brief fidelity 1 and
3 MATERIAL distortions (agents disagree on count, agree on the worst:
a TME/BofA brief inverted "$9bn sold in an up market" into "bought";
a JPM brief dropped "but looks to re-enter" after "took profit on
gold"; another collapsed "dollar-neutral or market-neutral" into one
construction). Those are real reader errors and the reader prompt now
names both failure shapes; this is the pre-freeze iteration the
shakedown exists for. Mechanism: shadow preserved by both agents,
production not preserved by both.

Three measurement defects found by the day-1 grades, fixed the same
day: (1) production was graded against the HIGH-only source set while
its pulse draws on MEDIUM documents, so real sentences read
"unsupported"; MEDIUM text is now published to source-text-all/ for
the graders only. (2) The sampler drew production's RECAP and WHAT TO
WATCH sentences, which the shadow does not have; sampling is limited
to THE MAIN EVENT and BRIEFS. (3) The 2a soft ceiling was computed as
distortions per brief (333%); it is the share of audited briefs with
any non-material distortion. Day-1's 7% production fidelity is
therefore a shakedown artifact, not a result.

## 2026-09-02 — Calendar: floor names need listed options; important econ rows are bold

Owner: the Thursday 9/3 sheet still showed a name with no move and no
confirmed session. It was PDI, a $7.4B PIMCO closed-end fund with no
options chain, admitted by the $5B cap floor. Rule added: above the
floor a name still renders unpriced, but only if Yahoo lists options
for it at all. The check is three-state (True / False / None for a
fetch failure) so a Yahoo outage keeps the dash-fallback behaviour and
only a positively empty chain drops a name, in both the normal path
and the wholesale-failure fallback. The posted 9/3 sheet self-corrects
at the 7:30 AM ET refresh once the deploy is live.

Owner: bold only the important economic events. EconRow.important is
decided in the data layer (Tier-1 series by name: CPI, PCE, PPI, GDP,
ISM, FOMC decision, payrolls, claims, retail sales, Powell/Warsh; or
anything the feed rates high impact; Fed member speeches, revisions,
second prints and minor series stay regular) and the renderer draws
those rows in semibold at full brightness, the rest regular and
dimmed.

## 2026-09-02 — Calendar posts at 3:00 PM ET

Owner: post an hour before the close so people can bet on the morning
prints. Right: a BEFORE OPEN name is only bettable during today's
session and options do not trade after hours, so the 4:20 PM post made
that column information rather than a trade. Quotes are live at 3 PM,
which is better for the pricer than closing quotes anyway. The 7:30 AM
ET in-place refresh is unchanged.

## 2026-09-02 — /ask structural fix 1: the deterministic router

Owner asked for the long-term fix behind the recurring /ask findings.
Every one of them was the model answering a data question from memory
or reaching for the wrong tool, both decided before it wrote a word.
discord_bot/ask_router.py now shapes the question in code (thirteen
shapes, regex tables, tested against the room's own questions from the
08-31 to 09-02 logs), restricts the declared tools per shape, prefetches
the shape's tool before the first model call and injects the result as
the authoritative block (the earnings-slate prefetch, generalised), and
sets WEB/FACT without the Gemini classifier for recognised shapes.
Unknown shapes are unchanged: full tools plus the classifier.

Paying deletions under the /ask policy: the prompt's HARD ROUTING RULES
block, the tool-priority sentence, the options-first and macro-first
paragraphs and two "search is required" bullets, 1,989 chars; the
prompt is 48,478 chars.

Not yet done, next: structural fix 2, a general figure-provenance check
that replaces the per-shape "unforced" validators.

## 2026-09-02 — Calendar: important companies are bold too

Owner asked how often the research carries in-depth earnings previews
(measured: 26 explicit previews in 1,222 PDFs over 22 days, ~1.2 a
day; 2 preview themes in 15 pulses) because he wants the calendar to
bold the companies that matter. Rule: a row is bold when the name is on
the pulse's major-ticker list, is a $50B+ cap, or was NAMED inside a
bank's earnings insight (or preview-shaped title) in the last 7 days.
The first cut counted any ticker mentioned anywhere in a note with an
earnings line and bolded 436 names; requiring the ticker or company
name inside the earnings text itself brings it to 78, and on the 9/3
slate that is Ciena, Lululemon and Zscaler beside the mega-caps, which
is the desk coverage the owner meant. A failed coverage query costs
the bold, never the sheet.


## 2026-09-02 — Review of the day's work, and a cleanup

The code-review skill ran over 5c4b4c23..HEAD (eight finder angles;
the verifier agents hit the session rate limit, so every candidate was
verified by hand against the working tree). Ten findings, all confirmed
and nine fixed in this commit:

- Calendar: yfinance answers an empty expirations tuple both for "no
  listed options" and for a rejected Yahoo session, so a 401 at 3 PM
  read as "no chain" and would have dropped every cap-floor name and
  emptied the wholesale fallback. The chain fetch now records a spot
  when Yahoo answered; no spot means unknown, and unknown keeps the
  dash. Chain presence is memoised per build, so the floor path and
  the fallback no longer re-fetch chains the walk just pulled.
- Router: "quote TSLA" and "what did powell say" routed to room
  history (no price tool, no Google); "what's the price of gold" made
  PRICE the ticker and "when is the next fed meeting" made NEXT one,
  which blocked the macro route; SPX/NDX/VIX/DOW were stopwords so
  index price questions had no route at all; "who reports next week"
  injected today's slate as authoritative; ledger and chat shapes were
  hard-coded BANTER. Fixed with a public-figure guard, a lead-in
  stopword list, index symbols mapped to Yahoo's caret form, no
  prefetch for week questions, and the two shapes added to
  FACTUAL_SHAPES. Prefetches now run together under one deadline each.
- The grader gate captured tee's exit status (no pipefail), so it could
  never fail. .env.example was unloadable (Python repr for the callers
  list, GEMINI_MAX_TOKENS blanked by a substring match, PORT emitted
  under its field name); gen_env_example writes JSON, matches secret
  suffixes, and uses the alias, and Settings(_env_file='.env.example')
  loads.
- Prompt: the directive that pointed at the deleted HARD ROUTING RULES
  block now states the price rule in one sentence; the fantasy
  reconstruction sentence and the macro-print bullet the router
  enforces are gone. Adds 467 chars, removes 583, net -116; the prompt
  is 48,362 chars.

Not fixed: calendar_posts.lineup_json is never written, so the 7:30 AM
refresh (gated off) would still rebuild from pre-market chains and
downgrade priced rows if re-enabled. The real fix is a row-level merge
in the refresh job; tracked in the TODO.

Cleanup in the same commit: root scratch dumps, one-off probe scripts
and screenshots deleted and ignored; the dead slate regex in bot.py
(router owns it), the unused citation helper, the editor pack's
abandoned ledger-walk scaffolding, the duplicate sentence splitter in
the grader inputs, the duplicate file-send body in sender.py, and the
dispatcher's hardcoded repo and unused tz parameter. CLAUDE.md's stale
market-context, session-ledger and TODO sections were rewritten.

## 2026-09-03 — Fantasy routing: the topic follows the question

Owner asked whether the bot can answer any league question off the
Sleeper API and still run Python. Both were true in principle and the
sandbox does survive the fantasy tool filter (a fantasy question sees
exactly two tools, lookup_fantasy_league and code execution). Two gaps
made "any question" wrong:

1. The gate required "draft grade" or "draft pick", so "who won the
   draft" and "grade my draft" fell to UNKNOWN, where Google is allowed
   and nothing forces the league tool. The gate now catches bare draft
   (DraftKings excluded), start-or-sit, faab, free agent, add/drop,
   trending adds, projected points and playoff wording.
2. Every fantasy question prefetched topic='standings' and the injected
   block called it authoritative. Pre-season standings are all zeros,
   which the tool's own docs warn about, so a draft question was handed
   zeros as the answer. Same defect class as the week-slate prefetch
   fixed on 09-02. `fantasy_topic()` now maps the question to one of
   Sleeper's eight topics, and a test pins every mapped topic against
   report.sleeper_data.TOPICS.

Two rules that came out of the testing and are worth keeping: the
fantasy shape strips Google and every market tool, so a false positive
is expensive, and "my team" and "first place" are therefore NOT gate
words (they hit "my team is bleeding on this trade" and "first place in
the s&p sectors"). And topic='roster' is never prefetched: it needs a
manager the router cannot resolve, and prefetching it injected "could
not match a manager" as the authoritative block.

The injected lead for the fantasy tool now names the topic it fetched
and tells the model to call again for a different slice rather than
answering from the wrong one.

Separately, on ingestion: the fantasy and cemini-alerts channels ARE
ingested (901 and 111 messages since 2026-08-13, current). The reason
profiles carry no draft content is the profile sampler, not ingestion.
Draft day was 08-23 (294 messages, ten times any other day), every full
rebuild ran on or after 08-28, and the per-channel floor takes the 40
MOST RECENT messages, so draft day fell outside the sample for heavy
posters. Light posters whose whole history fit under the 1500 cap kept
it, which is exactly the split in the data (BK: 364 fantasy messages,
nothing in his dossier; D3clan: 41, present). Not fixed yet: the floor
should spread across the window instead of taking the tail.

## 2026-09-03 — The football channel decides the route

Owner: "can't you just make it that if the asker is asking in the
football channel, it's gonna be about the sleeper fantasy". Yes, and it
is the stronger signal. `classify()` now takes `channel_name`
(`_answer_with_gemini` already had it, both call sites already passed
it, it just never reached the router).

Layered so the channel is a fallback, not an override:

- A question the shapes already claim keeps its shape. "whats nvda at",
  "when is CPI", "who reports today" work the same in the football
  channel as anywhere.
- A question nothing claimed becomes a league question if it carries any
  football word, using a looser list that would be reckless as a global
  gate (start, sit, bench, points, my team, first place, record, snaps,
  targets, injury, QB/RB/WR/TE). "hows my team looking" and "whats
  declans record" now route.
- Pure banter still falls through to the Gemini classifier. "you good?"
  and "lol what" stay UNKNOWN in that channel.
- A ledger question in the football channel is the league standing
  unless it names trading material, so "whats declans record" is
  standings while "abes trade log" is still the trade log.

Two policy changes came with it, both required once more of the channel
routes to FANTASY. Google is now ALLOWED on the fantasy shape: half of
what gets asked there is player news, injuries and outlooks that the
league tool cannot answer, and stripping the web would have made the
channel worse, not better. League STATE still comes only from the
injected payload, the same split PRICE uses for the number vs the why.
And chat search joins the fantasy tool list, because the fantasy gate
runs first and was swallowing "what did BK say about the draft".

The channel fallback deliberately prefetches NOTHING when no topic word
appears. "any injury news on CMC" has no league topic, and the old
standings default would have labelled an irrelevant payload
authoritative. A question that does name a topic still prefetches it.

Incidental fix found while testing: `_LOWER_LEADIN_RE` required an
apostrophe, so "whats nvda at" produced no ticker and lost the price
route. It now accepts the bare "whats"/"hows" spelling the room types.

## 2026-09-03 — The six league questions the room will actually ask

Owner listed them: who wins the match or the league, how's my matchup
or outlook, compare two teams or players, rank a position, who should I
drop for X, should I pick up X. Each now routes and prefetches the
Sleeper topic that answers it:

| ask | topic |
|---|---|
| who's gonna win the matchup | matchups |
| whos gonna win the league | standings |
| hows my matchup | matchups |
| hows my outlook | projections + member |
| compare bk and declan teams | projections |
| is puka better than nabers | projections |
| rank the qbs | projections |
| who should i drop for puka | roster + member |
| should i pick up puka | trending |

The unlock is first-person resolution. `sleeper_data.DISCORD_TO_MANAGER`
inverts the existing SLEEPER_TO_DISCORD map, so a question that says my
or I is aimed at the asker's own team: "who should I drop" prefetches
BK's roster instead of coming back "could not match a manager". SV has
no Sleeper user record so he maps to the roster he co-owns. A
non-manager asking "my roster" still gets NO prefetch, which is the
right degradation. Someone else's roster is never aimed at the asker.

Topic-table notes worth keeping. "should I pick him up" goes to trending
(is he hot across Sleeper) while "who picked him up" goes to
transactions (what moved in our league) — the two readings of the same
words want different endpoints. And the projections topic absorbed
compare/versus/rank/tiers/outlook/rest-of-season, because projected PPR
is the league-side half of every one of those and Google supplies the
rest.

Accepted trade-off: in the football channel a market question with no
strong shape ("compare NVDA and AMD") now routes fantasy and loses the
price tool. A ticker guard would fix it but would also break "compare BK
and Declan", since BK reads as a ticker. Erring toward fantasy in the
fantasy channel is what was asked for, and Google plus code execution
are still available there.

## 2026-09-03 — Incident: every routed /ask answered "Something broke"

Owner: "the asks are breaking in the fantasy football channel". It was
not the fantasy channel and not the routing. Commit 3f2cea1e rewrote the
phase-2 prefetch loop to run under one `asyncio.gather`, and built the
"tool has no executor" list as

    for _pf_tool, _ in set(_ask_route.prefetch) - set(_pf_plan):

A prefetch entry is `(tool, args)` and args is a dict, so `set()` raises
`TypeError: unhashable type: 'dict'` on the FIRST entry. Every question
the router recognised — price, slate, econ, earnings date, options,
history, league — raised before the model was ever called and answered
"Something broke on my end. Try again in a sec." Questions the router
did NOT recognise had an empty prefetch list, so `set([])` succeeded and
they worked. That is why it read as a fantasy-channel problem: the
fantasy shape always prefetches, and the questions that still worked in
that channel ("who are my starters") happen to be the ones with no topic
match and therefore no prefetch.

Live for ~19 hours, from the 05:15 UTC deploy to the fix. Confirmed in
`ask_bot_answers`: the 46-char canned failure on "who should i start
this week" at 09:24, 12:41 and 13:35 UTC, and on "what earnings do we
have after close" at 19:42, while every no-prefetch question in the same
window returned 300+ chars of real answer.

Why nothing caught it. The router tests cover `classify()` and
`filter_tools()`, which are pure. The loop lived inline inside
`_ask_02_call_model_with_tools`, a function no test can call without a
live Gemini client, so the one line between the router and the model was
the only unexercised link in the chain. The fix extracts
`bot._ask_prefetch_plan(route, executors)` to module level and tests it
against a real Route for every shape, asserting the args stay dicts. The
lesson worth keeping: an inline expression inside an untestable function
is untested code no matter how many tests surround it, and `set()` over
tool-call tuples is the specific trap.

The gate did not help either. `scripts/ask_fixture_run.py` mirrors the
ROUTER, not phase 2, so it reproduced the routing correctly and never
touched the crashing line.

## 2026-09-03 — Ask log review: a day with zero sourced answers, and why

Owner: "review omnicalendar and ask logs, still see some without
source". The ask log for 09-03 (23 interactions) has ZERO Sources
footers, and the Route stamp reads `LOCAL/BANTER · ungrounded` on
questions like "whats the probability according to kalshi or
polymarket", "how much market cap does $1 of NVDA represent" and
"implied move on lulu earnings". Those answers carried specific
figures (54% hike odds, 24.22B shares, ±8.1% to ±9.6%) with no tool
call and no search behind them.

Root cause, reproduced locally: the intent classifier
(`_classify_ask_needs_web`) sends `thinking_config=ThinkingConfig(
thinking_budget=0)`, and `gemini-3.5-flash-lite` answers 400
INVALID_ARGUMENT to a zero budget (256, 512, 1024, 2000 and -1 are all
accepted, so every OTHER thinking_config in bot.py is fine). The call
raised on every question, the except branch returned `(False, False)`
= LOCAL/BANTER, and every question the deterministic router did not
recognise lost Google and the FACT register. It logged at INFO, so it
never surfaced. Fix: no thinking_config on the classifier (eight output
tokens is the whole verdict; verified live: WEB FACT for the five
unsourced questions, LOCAL BANTER for "you good?" and "abe is a clown
lol"), and the failure default is now WEB/FACT at WARNING, because the
instruction itself says "when genuinely unsure, answer WEB" and a
classifier outage is the least sure state there is.

Router shapes added from the same log so these do not depend on the
classifier at all: sourced-figure questions (probability/odds according
to X, shares outstanding, market cap, "why didn't you tell us there was
a ... event today") route NEWS_EVENT with Google on; "implied move on
X" routes to the options chain, except in the past tense ("what WAS the
implied move"), where the chain prices the next expiry and the answer
lives in chat or on the web. Bare "float" is a verb in this room and
is not a gate word.

Calendar: the 09-04 sheet rendered one row, KNOP ($390M) with a dash.
Finnhub listed six micro-caps for that Friday, Yahoo answered for all
of them and none priced a straddle, so the wholesale fallback fired on
"0 of 1 priced" and its `is not False` test re-admitted a name that
the floor rule excludes. The fallback now keeps a name only when chain
presence is UNKNOWN (Yahoo down) or the name is $5B+ with a chain. A
micro-cap day renders empty, which is the truth for an options trader.
The test fixture now prices every name by default and has a
`chain_unknown` switch for the Yahoo-down scenario, so the wholesale
test states its premise instead of relying on the old conflation.

## 2026-09-03 — "who should I start" answered with ten arrows and no words

Owner screenshot: the bot answered "who should I start this week and
how's my matchup looking" with the ten current starters and their
projected points, then one sentence on the matchup. No start/sit call.

Not a guard stripping prose (`guards: —`, no raw-output block); the
model wrote the list. The cause is the payload: `topic=projections`
with a member returned ONLY `roster.starters`, so the injected
"authoritative" block was the lineup already set on Sleeper and there
was nothing to compare it against. The model did the only honest thing
with that data and recited it.

The member branch now returns starters AND bench, each row tagged
`slot: starter|bench`, plus a note that a start/sit answer names the
swaps where a bench player projects higher at an eligible slot and
says so when the lineup is already optimal. League-wide rows (no
member) are unchanged. The sleeper smoke pins the shape.

## 2026-09-03 — League questions get the whole week, then the model analyses

Owner: "if I'm in the fantasy channel and ask about league related
questions, the bot should be able to do whatever bespoke analysis ...
and be able to determine the right data to call to form a useful
response." The router had been guessing one narrow Sleeper topic per
question and injecting it as authoritative; the model then had to
notice the gap and call again, and usually did not (the ten-arrows
answer was projections with no bench; a matchup read had no opponent
lineup to argue against).

New topic `situation` (report/sleeper_data.py): one manager's whole
week in one payload. Roster with projections and `slot` tags (starter
or bench), this week's opponent with their lineup and projected total,
the manager's record and points, the standings table, and a note that
says what analysis each part supports. Verified against the live
league for BK: 3.8 KB, 15 roster rows, 135.1 projected vs Cemini+SV
136.3, the same numbers the bot had quoted.

Router: any fantasy-shaped question from a known manager prefetches
`situation` for the asker first. Draft, transactions, trending and
league settings are not in it, so those topics ride alongside when the
question names them. A non-manager keeps the narrow-topic prefetch.
The injected lead now says to answer the question that was asked with
the analysis it calls for (a start/sit is a call with the swap and the
projection gap; a matchup read is lineup against lineup; an outlook
names the stakes), not a recital of the rows.

Cost: one Sleeper composite (~4 KB) instead of one narrow slice
(~0.7 KB) per league question, inside the prompt budget, and each
follow-up tool call it replaces was a full model round.

## 2026-09-03 — Figure provenance: a number with no source loses its line

Owner, after the LULU answers: "do it then as long as it doesn't break
again." The grounding backstop only fires on shapes it recognises;
this check is shape-blind. Every figure in a FACT answer must appear
in the evidence the turn saw (injected blocks, function_response
payloads, sandbox output, the question, chat context, prior answers)
or the bullet or sentence carrying it is removed.

`discord_bot/figure_provenance.py` is pure: no I/O, no model. It
tolerates rounding (21.4 vs 21.36), percent forms (8.1% vs 0.081) and
scale (2.46B vs 2460000000 vs "$2.46 billion"), and ignores weeks,
ordinals, times, dates, years, index names and option symbols. The
ladder calls it LAST, after the grounded retries, only on FACT answers
with no web grounding (a grounded answer's sources are the footer and
the SDK's chunks carry no snippet text to check against).

"Doesn't break again" is built in three ways. `check()` is total and
returns the answer untouched on any internal error, and the ladder
call sits in its own try/except. When every line carries an unsourced
figure the guard does not strip (that would ship nothing); it appends
the existing unverified hedge and stamps `figure-provenance:all-
unsourced` so QC sees it. And the tests run the real code on the real
answers that motivated it: bulch's LULU answer keeps its three sourced
lines and loses the invented "10.2% historical" line; SansDE's answer,
unsourced on every line, is left whole with the hedge.

`bot._ask_evidence_text` is module-level and tested on real SDK Part
objects, the same discipline as `_ask_prefetch_plan` after the
2026-09-03 incident: the line between the model and the reply is not
allowed to be an inline expression no test can reach.

Two exemptions from the blast-radius sweep over the day's 23 real
answers. A turn that carried an image is skipped: figures read off an
attached chart have no text to match. And the WHO'S TALKING dossiers
are excluded from the evidence: their scores and message counts made a
Kalshi "54%" look sourced. Chat context, the bot's prior answers and
the question stay in.

## 2026-09-03 — Shakedown day 2 ran on stale cards; three guards added

Owner: "what happened to our pilot". Day 2 ran end to end and produced
a graded, legitimate-looking result from the WRONG input.

What happened. `pilot/cards/` holds exactly one directory, 2026-09-02,
with 9 files. The readers produced nothing for 09-03 and left no ops
record. The editor ran anyway: its window is `--date $DATE --days 1`,
which reaches back a day so a late reader still counts, and with 09-03
empty it packed 09-02's 9 documents and 338 cards while 25 fresh source
files sat unread. The meta recorded that plainly
(`unread_source_files_at_edit: 25`, against 0 on day 1) and the run
continued. The graders then scored a pulse arguing yesterday's
documents against today's sources, and shadow fidelity fell 97% -> 70%.
That 70% is the staleness, not the writer.

Three faults, three guards:

1. The editor had no freshness check, only `cards != '0'`, which stale
   cards satisfy. `pilot_editor_pack.has_own_day_cards()` now requires
   cards for the edit date ITSELF; the CLI exits 2 and writes no pack,
   and the workflow turns that exit into a red run with an error naming
   the unread count. `--allow-stale` exists for deliberate backfill and
   a scheduled run must never pass it.
2. The readers' cards AND ops commits both sat behind
   `unread.count != '0'`, so a day where they produced nothing left no
   trace to diagnose. A quiet run now writes and commits its ops entry
   with `quiet: true`.
3. The scoreboard scored the stale day. `is_void()` is now one rule used
   by both `day_row` and the header count: any day with a nonzero
   unread-at-edit is VOID, shown in the table, excluded from the counted
   days and from every metric, and listed under the table with the
   reason.

Clock: still "Day 1: NOT SET, counted days 0 of 10". Two shakedown days,
neither clean (day 1 needed the reader turn-limit and topic fixes, day 2
was stale), so `pilot/DAY1` is NOT committed tomorrow. The next clean
pair starts the count.

## 2026-09-03 — Code review of the pilot: seven findings, all fixed

A review of the whole pilot subsystem (cc5a228d~1..HEAD, 3,088 lines
across 13 scripts and 4 workflows). Three of the seven end in the pilot
producing nothing while looking healthy, which is what day 2 looked
like.

1. **readers: one bad document discarded the whole run.** The
   per-document loop runs under the Actions default `bash -e` and only
   `pilot_finalize_read.py` was guarded. `wc -c`, both verify_cards
   calls and the `NEEDS=` read were bare, so any one failing aborted
   the step; the ops record is written at the end of that same step and
   the commit is gated on it succeeding, so every card already read
   vanished with no trace. That is exactly the 09-03 signature (cards
   absent AND ops absent). Each of those now warns, counts a failure
   and continues to the next document.
2. **editor: a missing `## _LEANS` block silently discarded the pulse.**
   Pass 1 was wrapped in `set +e`; pass 2 was not, and
   `pilot_verify_citations` returned 1 on a missing leans block
   regardless of `--final`, aborting the step and skipping the commit.
   The verifier now blocks only on pass 1, and pass 2 is wrapped too.
   This contradicted the comment sitting directly above it.
3. **graders: my own 12fd5e0a refusal blocked re-grading.** The graders
   rebuild the pack only to resolve citations in an ALREADY written
   pulse, so the freshness refusal protects nothing there and made
   re-grading 09-03 impossible. They now pass `--allow-stale`.
4. **finalize_grade: an empty sentence list scored 0% fidelity.**
   `faithful_rate = ... if n else 0.0` with counts 0+0+0 == 0 produced
   a real-looking zero marked `failed: False`. A grader hitting its
   60-turn cap therefore read as the WRITER failing metric 2, whose
   regression is a frozen KILL criterion. It is now a validation
   problem, and the scoreboard's new `usable()` treats any
   `failed: True` grade as a missing agent rather than scoring it.
5. **scoreboard: the metric-2 agreement tolerance was 0.14.** Two
   graders could differ by more than the shadow-vs-production gap being
   measured and still be averaged into one number, so a real
   disagreement never reached the owner tiebreak the module docstring
   promises. Now 0.05, matching metric 1's discipline. Safe to change
   because the clock has not started.
6. **`verify.reasked` was always 0.** The re-ask rewrites the cards file
   through finalize_read, which emitted a fresh document with no
   `verify` block, so the counter reset every time. finalize_read now
   carries it across.
7. **pilot_list_unread hardcoded `source-text` and `cards`** while
   pilot_config claims to be the single source of those paths. Both now
   come from `SOURCE_TEXT_SUBDIR` / `CARDS_SUBDIR`; a relocation would
   have left the scanner globbing the old layout and reporting zero
   unread forever, which is a silent pilot.

326 unit tests, 157 fast smokes, every workflow shell step passes
`bash -n`. Production untouched.

## 2026-09-03 — The pilot readers were dying on the job timeout, not a code path

Owner pushed back that I DO have GitHub access. I do: the credential
manager holds a token and the Actions API answers. That changed the
diagnosis, and my morning code-review finding was wrong about the cause.

What the API says. Every `pilot-readers` run on 09-03 is "cancelled",
20 of them, zero successes. They are not cancelled by concurrency
(`cancel-in-progress: false`). Their durations are 2715, 2716, 2717,
2718, 2719 seconds: GitHub reports a job killed by `timeout-minutes: 45`
as cancelled. The 01:00 run's log is unambiguous: 37 unread, nine
documents read, 273 cards verified, zero failures, then the clock ran
out mid-loop and the commit step was skipped. Nothing persisted, so the
next run started again from 37 and read the same nine. A death spiral
where every run does real work and throws all of it away.

That, not the unguarded in-loop command I flagged this morning, is why
09-03 had no cards and no ops record. The unguarded-command finding is
still real and is still fixed, but it is not what happened: the job is
killed from OUTSIDE the shell, so no in-loop error handling could have
saved it. Worth remembering as a diagnosis lesson: "no artifact and no
log entry" pointed at an in-process abort, and the run history said
otherwise in one API call.

Fix: persist after EVERY document. A `persist()` helper commits cards
plus ops and pushes with a rebase-retry, called after each document and
once at the end. A kill now costs the document in flight, not the run,
and a 45-minute run retires nine documents so the next starts from 28.

The ops record moved out of an inline YAML heredoc into
`scripts/pilot_ops_record.py`, upserting ONE entry per run id so nine
per-document calls collapse into one record. Same reason as
`_ask_prefetch_plan` and `_ask_evidence_text`: logic no test can reach
is logic that breaks silently. It has unit tests for the upsert, the
pulse-window flag, and corrupt input.

## 2026-09-03 — daily-qc lost a push race and discarded a full grading

The GitHub daily-qc run failed today at step 12 of 12. Auth passed,
grading passed, the pulse review passed; the bare `git push origin
pulse-data` was rejected because the bridge had pushed while it worked.
The graded output existed and was thrown away, which is why
`ask-qc/2026-09-02.claude.md` never appeared. It also failed twice on
09-01 the same way, so it is an intermittent race, not a new break.
Every pilot workflow already does `git pull --rebase` before pushing;
this one did not. It now rebases and retries three times, and errors
loudly if all three fail.

## 2026-09-04 — The tone dial is decided in code now

Owner: "why is the bot default to lowercase and so mean?"

Lowercase is deliberate and correctly scoped: ask_prompt line 225 says
capitalization loosens IN BANTER. It read as the default because until
09-03 21:14 UTC it WAS the default: the intent classifier failed on
every call and fell back to LOCAL/BANTER, so sincere questions got the
loose profane register too. Today's log is the first clean sample:
three BANTER replies in room voice, one WEB/FACT answer properly
capitalized with structured bullets.

The meanness is a separate defect, and the rule to prevent it already
existed. The dial section has said since 08-20 that it rests at zero,
that only what the asker brought THIS exchange raises it, that praise
is not provocation, that size is part of the match, and never to open
profile material the exchange did not open. All three 09-04 replies
broke every one of those:

  "Gg Abe"                     -> a paragraph about their straddle
  "Hey buddy straddle hit 11x" -> their MSTR and Micron history
  "Puts pls."                  -> "peak casino brain", Palantir bags,
                                  shoplifting plans with Kyle

All three are also P&L jabs, which the owner has twice called lame and
repetitive. A rule the model is asked to infer is not a rule, so
`discord_bot/tone_dial.py` computes the LEVEL (0-3) from the asker's
own words and injects it as an instruction. It reads only the asker's
message, never the quoted alert the reply-to machinery prepends, or
every reply-to would look like provocation. Intensity only: the
reply-to trigger is untouched (the owner reversed a stand-down once).

Tests are the real 09-04 messages: Gg Abe and Puts pls. are 0, the
straddle boast is 1, "you are useless" is 2, "fuck you bot" is 3,
"roast me" is 0 because invitations are handled elsewhere.

Also fixed: the figure-provenance guard skipped on
`_grounding_has_sources`, which counts every grounding chunk, while the
Sources footer renders only chunks carrying a `web` block. Today's one
factual answer was stamped `ungrounded` with no footer and still
skipped as "grounded". It had no figures so nothing was lost, but the
guard must use the signal the reader gets: `_grounding_web_source_count`.

## 2026-09-04 — Dry was never the design; the dial was steering into it

Owner: "why dry voice? why can't it be more encouraging?"

Nothing requires it. The prompt BANS it, twice. Line 225: "Not stiff,
not dry, not above-it-all." And a whole binding section, NO DRY /
DEADPAN / PASSIVE-AGGRESSIVE, which names sardonic detachment as "the
bot's most common voice failure — sounding ABOVE the room" and
prescribes DIRECT with energy in its place.

Two places contradicted that, and one of them is mine from yesterday.

`tone_dial.LEVELS` asked for "at most ONE dry line of seasoning" at
level 1, and phrased level 0 as "no jab, not one seasoning clause",
which reads as no energy rather than no heat. A directive injected into
every single turn, steering the model straight into the register the
prompt calls its worst failure. Level 0 now says to answer straight and
with energy and states outright that warmth and enthusiasm are always
on-register, because the dial limits heat aimed AT someone, never how
pleased it can be for them. Level 1 says direct and in-register, never
deadpan.

The prompt's own survivor was line 287, praise gets "one dry line,
done". Now "take it in one line, warm or amused, never dry". Adds 19
chars; the prompt is 53,296 against a 63,590 ceiling.

Worth keeping as a pattern: the dial is a HEAT limiter and heat is not
the same axis as warmth. Conflating them made the bot flat at level 0,
which is the most common case by far (every plain question scores 0).

## 2026-09-04 — Every Sources footer has been missing since the phase split

Owner: "check the recent user asks, I didn't see a source cited in the
answers." Correct, and it was not the classifier this time.

The count of `Sources:` footers in the ask log, per day: 08-28: 7,
08-31: 1, 09-01: 13, then 09-02: 0, 09-03: 0, 09-04: 0. The split of
`_answer_with_gemini` into eleven phases landed on 09-01. From the next
day, no answer carried a footer, including answers whose route stamp
said the grounded retry had succeeded.

Cause. `_ask_09_rank_and_regen_guards` did not take `grounding_metadata`
as a parameter. It set `grounding_metadata = None` at the top, assigned
it only inside its regeneration branches, and returned it. The caller
unpacked that return into the real variable, so on every turn where
phase 9 regenerated nothing, which is nearly all of them, the grounding
that phases 7 and 8 had built was replaced with None before phase 10
rendered the footer and stamped `grounded`. The split docstring's claim
that None-initialised outputs "were never read on the others" was false
for this one variable.

Three visible consequences, all from this one line: no Sources footer
on any answer for three days; `ungrounded` stamped on answers that had
grounded (today's S&P-inclusion answers with $22.7B and the 50% float
rule WERE grounded by Google; the reader never saw the citations); and
my figure-provenance guard, which runs in phase 7, correctly saw the
grounding and skipped, while phase 10 then shipped the same answer with
no sources. That contradiction was visible in yesterday's log and I
misread it as a chunk-shape problem in the guard's skip test.

Fix: phase 9 takes `grounding_metadata` as a pass-through parameter and
the caller passes it; the None-init is gone. Tests run the REAL phase 8
and phase 9 with a sentinel grounding object and a client that fails
the test if any guard calls the model on a benign answer, and assert
the sentinel comes back. A generic test enforces the structural rule
across all eleven phases: a phase may return grounding_metadata only if
it received it or builds it from `response`.

Yesterday's classifier repair and this fix are independent halves of
the same symptom. The classifier decided whether Google was even
offered; this decided whether the reader saw what Google returned.

## 2026-09-04 — The room's book: lookup_room_positions

Owner: "is the bot able to tell when most of the room are piled into
the same plays?" The ledger could answer it (1,714 real trades, 57
members) and nothing pointed the bot at it. "what's the room piled
into" matched the CHAT shape's "what's the room" alternative and would
have searched chat text for ticker mentions instead of counting logged
positions; most other phrasings fell to UNKNOWN.

New tool `lookup_room_positions` (db.get_room_positions), new router
shape ROOM_CROWDING tested before the chat shape, prefetch of 14 days
(3 for "right now / this week"), Google off, chat search off.

Two decisions worth keeping. Membership is ENTRY-based: a member is "in"
a ticker if they logged an open or add in the window. The first cut
counted any row, and production returned PLTR "9 members" when two had
entered and eight were CLOSING, which inverts what crowded means. Exits
are now a separate count so "everyone is getting out of X" is visible
as its own fact. And the payload carries the entry-bias caveat every
time: people screenshot entries far more than exits, so
members_entered_not_exited is an UPPER bound on who still holds it.

The two query_data traps are baked into the SQL so the model never
re-derives this by hand: is_trade=1 only (raw rows overstate ~37x) and
distinct author_id, never display name (renames split one person).

Bare "we ... in" is deliberately not a trigger: "are we in a recession"
is a macro question. "we all in", "everyone in", "same trade/boat",
"piled into", "most crowded" and "who's in $X" are.

Also this session: the pilot reader's failure branch now prints the head
of the reader's STDOUT (the CLI reports usage limits, auth and max-turns
there, not on stderr), so the next "not parseable JSON" is diagnosable.
And the apparent run overlap was not real: the concurrency group queued
the 17:00 run until the 16:50 run finished (job started 17:06:55), and
with per-document persistence a queued duplicate finds nothing unread
and exits in a minute. No stagger added.

Diagnosis note for future me: a heredoc through the Bash tool turned
five `\b` anchors into backspace bytes again. Byte-repair
(0x08 -> 0x5C 0x62) fixed it; regexes go in through Write/Edit.

## 2026-09-04 — I locked the production database with a diagnostic probe

While verifying the crowding tool end to end I ran `railway ssh` probes
that did `import db` and called the executor. A second process opening
the live DB runs `_ensure_schema` (executescript, write lock) on its
first connection. The first probe hung on that lock for ~30 minutes,
and in that window the WORKER logged `database is locked` 16 times: a
Dropbox poll failed, the synthesizer's open-reads block was skipped, the
bridge's pulse_state snapshot failed. Ingestion resumed once the probe
process died. Nothing was lost that a later poll does not pick up, but
this was a self-inflicted production incident, and the tool it was
meant to verify was never the problem.

Two facts that make the in-process path safe and the probe unsafe:
`_schema_ready` is a process-global flag set at boot, so a new thread
inside the worker gets its connection without the schema script; a
separate process has the flag False and runs the script every time.

Rule, now in CLAUDE.md under "Accessing production state": probe
production data read-only (`file:/data/reports.db?mode=ro`, uri=True)
and write the query by hand. Never `import db` in a second process
against the live volume.

The read-only run gave the real 3-day crowding picture, and it
validates the entry-based redesign: SPX 5 entered / 1 exited, AVGO 4/4,
MU 4/4, QQQ 4/4, LULU 3/1, MSTR 3/2. PLTR, which the first cut reported
as "9 members", does not appear at all: those nine were eight closers
and one entry.

## 2026-09-05 — Pilot review against the plan; five blockers before the clock can start

Owner asked for a review of the pilot traced back to the spec and plan,
then said go. Spec steps 1-3 (driver, anchors, blocking adversarial
gate) are built. The pilot (step 4) is built and has three shakedown
days, none clean: 09-02 reader turn limit, 09-03 every reader killed by
the 45-minute timeout with nothing saved (VOID), 09-04 five documents
failing on every run and the editor running with 8 unread (VOID).

Findings the scoreboard did not show:

1. The same five documents (three 132 KB volatility pieces, a 75 KB
   GOAL note, a 64 KB JPM wrap) failed at 12 turns on all fourteen
   reader runs. That is the whole 65% failure rate, ~100 wasted reader
   calls, and 630 runner minutes in one day (ten runs killed at 45 min)
   against a plan estimate of 60-90 minutes per day for the pilot.
   Fix: turn budget steps at 50/100/300 KB, and `pilot_read_failure.py`
   records each failed attempt under `read-failures/<date>/<id>.json`;
   after MAX_READ_ATTEMPTS (3) `pilot_list_unread` stops listing the
   document. The editor meta records `given_up_at_edit`, so the void
   rule counts readable-unread only and the coverage gap stays visible.
2. GitHub's cron fallback fired the editor at 17:11-17:19 on 09-03 and
   09-04 and overwrote the 13:55 shadow pulse with one written from a
   different card set, after production published; the graders (also
   double-fired) graded that version. Plan 3.5's shared information
   window was broken on both days. The editor and graders lost their
   `schedule:` blocks; the readers keep theirs.
3. Seven grader disagreements pending across three days, none broken,
   so `m2 pass` could not compute. The spec lets the owner delegate:
   the graders workflow now runs a third fresh agent on the same frozen
   prompt and input for every stem `pilot_tiebreaks_needed.py` prints
   (the scoreboard's own agreement rule), saved as `<stem>-tiebreak`.
   `<stem>-owner.json` overrides it; an unusable tiebreak is ignored.
4. Metric 1 fails the frozen rubric on the only full-corpus day: 24-26%
   fragmented mass against the 10% cap, and both graders found
   theme-changing mis-merges. The graders' diagnosis is the reader
   `topic` field: single notes chopped into 10-22 one-card labels, and
   the same cross-bank data point relabelled per pass. The reader
   prompt's topic rule was rewritten (subjects first, pulse-theme
   grain, one-card labels are a warning) while it is still free to
   edit; after DAY1 it would cost the single allowed iteration.
5. Plan 4.3 stress datapoint, ledger half, over the merged 09-02..09-04
   card set: 65 documents, 2,193 cards, 1,020 topic labels, 652 of them
   holding a single card (64% of labels, 30% of card mass), 61 labels
   with two or more banks, 17 with three or more (top: fed rate hike
   odds 5 banks; september seasonality, long end treasury yields,
   hedge fund positioning, gold price 4 each). Median 13 labels per
   document, max 42, 39 of 65 documents over 10. Editor pack 486K
   chars. The editor half of 4.3 is still to run. Subscription headroom
   (plan section 5) cannot be read by a script; the owner records it.

Early signal, uncounted: shadow fidelity 87-100% on fresh-card days
against production 33-60%; shadow mechanism preserved every day,
production 1 of 6 grader calls; 2a zero material distortions per grader
b every day, one per grader a on 09-04 (tiebreak), non-material on
80-100% of audited briefs against the 20% soft ceiling. The production
arm is graded against the HIGH+MEDIUM source tree only, which biases
"unsupported" against production, in the shadow's favour.

Clock: DAY1 not set. Earliest clean pair is Mon 09-08 and Tue 09-09,
DAY1 Wed 09-10, ten counted days ending around 09-23.

First run on the new readers workflow (run 33939842122, 02:42 UTC):
the three 136 KB volatility pieces read at 30 turns, 44/42/54 cards,
every anchor verified, all three committed. The 69 KB JPM metrics wrap
(5,495 lines) and the 75 KB GOAL locator (2,944 lines) still failed at
20 turns and now carry attempts=1 in read-failures/. The reader pages
the file by lines, so the budget now keys on lines as well as bytes
(30 turns over 50 KB or 1,500 lines, 40 over 150 KB or 3,500 lines).
Two more failures and they leave the unread list. Backlog otherwise
clear: 68 of 70 source documents have cards.

## 2026-09-07 — Three GitHub failures, three different causes

Owner: "failed GitHub again?". Three red runs since Friday, none of
them the same bug.

1. **daily-qc, 18:55 UTC, Labor Day.** The pulse-wait step treats "a
   weekday with no pulse artifact by 8:20 Pacific" as a page-worthy
   incident, and knows only about weekends. Market holidays are
   weekdays with no pulse, so it fails every Labor Day, Thanksgiving
   and observed July 4th. It now reads `world_context.is_us_market_holiday`,
   the same calendar the synthesis routine's own holiday gate uses, and
   skips quietly. `smoke_market_holiday_gate` pins Labor Day for both
   covered years.

   The same run also exposed a regression from yesterday's fix. Making
   the pulse phase independent of the ask phase with `!cancelled()` was
   right, but its other condition was `steps.pulse.outputs.skip != 'true'`
   — a NEGATIVE test that reads identically whether the wait step
   skipped, succeeded, or FAILED. So with no pulse at all, the pulse
   judge was dispatched anyway and failed a second time. The wait step
   now emits an explicit `ready=true` only when it has the artifact in
   hand, and the judge runs on that.

2. **pilot-readers 09:00, red for working correctly.** Its single
   unread document was the one already on its third attempt; the
   give-up rule retired it, which is the designed outcome, and then
   `every reader failed this run` turned the run red. Retirements are
   now counted separately: an all-retired run warns, an all-failed run
   still errors. A red run for correct behaviour trains the owner to
   ignore red runs.

3. **The 12-turn floor is too low for the current reader prompt.**
   Six documents of 9-33 KB died on it over the weekend while 37, 44
   and 45 KB documents read fine, and two of the six succeeded on a
   later attempt — the signature of a marginal budget, not a poisoned
   document. 12 was set against the older prompt; the topic rule added
   2026-09-05 asks the reader to survey the document's subjects before
   writing cards, which costs turns. Floor raised to 20. Unused turns
   cost nothing.

Two 45-minute reader timeouts (09-06 22:48, 09-07 01:00) are NOT in
this list: both persisted every document they finished before the kill,
which is what the per-document commit was built for. They show as
cancelled and cost nothing.

A fourth instance of the same root class turned up while verifying the
fixes: `test_refresh_edits_in_place_when_lineup_changed` failed today
and only today. `_calendar_refresh_job` asks the real calendar whether
TODAY is a closure, so every refresh assertion in that file passed only
on days the market was open. The holiday answer is a stub now, with a
separate test asserting the gate itself. Three separate places assumed
"weekday" meant "trading day"; that is the lesson worth keeping, not
any one of the three fixes.

## 2026-09-07 — "Thought myself in circles": an empty answer had no retry

Owner: "check recent bot asks it's thinking itself in circles". Four of
today's six asks shipped the fallback wrapper. The pattern in the log is
exact:

| | route | grounded | outcome |
|---|---|---|---|
| "how much of an ass whooping did Georgia give TCU" | WEB/FACT | no | fallback |
| "what was the Georgia v TCU score" | WEB/FACT | yes, 3 sources | answered |
| "was the tcu vs michigan game one of the best ever" | WEB/FACT | no | fallback |
| "was the tcu vs michigan playoff game in 2022 one of the best" | WEB/FACT | yes, 4 sources | answered |
| "did michigan win the natty the next year" | WEB/FACT | no | fallback |
| "hardest teams TCU played in 2022-2023" | WEB/FACT | no | fallback |

Every failure is ungrounded, every success is grounded, and the asker
recovered twice by rewording — which is what the fallback tells them to
do, and a bad trade for the room.

Rate by day: 1/14 (09-02), 1/38 (09-03), 0/11 (09-04), 4/6 today. The
first two were LOCAL/BANTER; the WEB/FACT cluster is new.

Cause: `_ask_09` handles a textless response with a four-tier ladder for
the safety filter and NOTHING for a spent budget — `finish_reason in
("MAX_TOKENS", "OTHER", None)` assigned the wrapper and stopped. Two
things land there and are indistinguishable from the outside: reasoning
running out the 5000-token ceiling, and the SDK's automatic function
calling looping to its 10-call limit and returning a textless final
turn ("AFC is enabled with max remote calls: 10" is in every boot log).

Fix: one retry with the thinking budget cut 2000 -> 512 and the FUNCTION
tools withdrawn, keeping google_search (it resolves server-side and
returns text rather than a function_call — the same reason the filter
ladder keeps it). Both causes are addressed by exactly those two
changes. Grounding from the retry is passed back, so a retried answer
that searched is stamped grounded and keeps its Sources footer.

Also: the audit stamp now carries `empty: <reason>` and `empty-retry:
<result>`. The reason was already gone from Railway's hour-long tail by
the time this was investigated, and an entry that shipped a wrapper
looked identical to one that answered badly.

Not fixed, and worth watching: WHY these four never searched. All four
are vague or comparative ("one of the best ever", "how much of an ass
whooping", "hardest teams"); the two that searched were precise. If the
retry does not clear them the next step is forcing a search call on the
WEB route rather than leaving it to the model.

## 2026-09-07 (later) — The circling answers were a blocked prompt, not a spent budget

Owner asked me to check the real question: why those four never
searched. Replayed all six of the day's logged prompts verbatim against
the live ask model (`gemini-3.5-flash-lite`) in three conditions —
search-only/think 2000, every tool/think 2000, search-only/think 512.

Result, identical in all three conditions, deterministic:

```
Q: how much of an ass whooping did Georgia give TCU
   A search-only, think 2000: finish=None text=0 sources=0 thoughts=0 out=0
   B every tool,   think 2000: finish=None text=0 sources=0 thoughts=0 out=0
   C search-only, think  512: finish=None text=0 sources=0 thoughts=0 out=0
```

**Zero thinking tokens and zero output tokens.** Nothing was generated,
so nothing ran out of room. The fix I shipped an hour earlier (cut
thinking to 512, withdraw function tools) is condition C, and it fails
identically. It was aimed at the wrong cause.

Second probe named the real one: `prompt_feedback.block_reason =
PROHIBITED_CONTENT` on all four. Gemini's unconfigurable filter blocked
the PROMPT. Isolating question from context:

| | full prompt | minus **Voice.** | question only | question, no system |
|---|---|---|---|---|
| ass whooping | BLOCKED | ok | malformed | ok |
| best playoff game | BLOCKED | ok | ok | ok |
| michigan next year | BLOCKED | BLOCKED | ok | ok |
| hardest teams TCU | BLOCKED | BLOCKED | ok | ok |

The question is never the problem. The profiles are: these askers'
**Voice.** sections and chat quotes are the densest slur containers in
the prompt, which is the exact failure the filter ladder was built for
in June. Stripping Voice clears two, question-only clears all four.

So why didn't the ladder fire? It gates on `safety_ratings[].blocked` or
`prompt_feedback.block_reason`, and production's response carried
neither (the replay's did). Rather than guess at the object shape, the
gate now also fires on what is true in every observed case and cannot be
faked by a budget failure: **the model generated zero tokens.** A block
generates nothing; a spent budget generates plenty. `_ask_meta["empty"]`
records which, so the ask log now says `empty: PROHIBITED_CONTENT` or
`empty: nothing-generated` instead of nothing at all.

The short-thinking retry from earlier today stays, narrowed to what it
actually addresses: a real MAX_TOKENS with output tokens spent.

Lesson worth keeping: the wrapper text ("Thought myself in circles and
ran out of room") was itself the misdiagnosis. It asserted a cause the
code had never checked, and both the owner's read and my first fix
followed it. Wrapper copy that names a cause is a claim, and it needs
the same evidence as any other claim.

## 2026-09-08 — The provenance guard was deleting Abe's book

Owner: "check recent bot asks". Four asks since the deploy, no circling,
but the first three are one incident:

```
00:17:37 dovahjo   "what are Abe's current holdings"
         A: "Current book:"            guards: figure-provenance:stripped:1
00:17:58 spockbones "Nice book"        (the book, intact, on the BANTER route)
00:18:03 dovahjo   "You didn't show anything"
         A: the book + "Couldn't verify these specifics against a live source"
                                       guards: figure-provenance:all-unsourced
```

The model wrote all eight positions. The guard deleted them and shipped
two words. The asker said so.

Reproduced locally in one call, no production access needed: the answer
writes a strike as `350(P)` and the trade log writes it as `350P`.
`_NUM_RE` ends in a negative lookahead that refuses a number with a
letter stuck to it, so on the EVIDENCE side `350P` yielded no figure at
all. Every strike in the book therefore read as invented, while the
prices (`@3.53`) matched fine. It hit the room's most common local-data
question, and it would have hit any answer quoting a contract.

Fix: `evidence_values` also reads strikes out of contract notation
(`_OPT_STRIKE_RE`, digits followed by a single C or P, not preceded by
a letter, so `NDXP29900C` stays one token). Widening the evidence pool
can only make FEWER figures unsourced, never more, so it cannot cause a
strip that was not already happening.

Found while writing the regression test, unrelated and NOT fixed:
`_close()` has a flat 0.06 floor, and a percentage is also compared as
its /100 form, so any two percentages within SIX POINTS of each other
match. An answer saying 10.2% is "sourced" by evidence saying 6.1%.
That is most of the range this guard is supposed to police. Pinned in
`test_percent_tolerance_is_loose_enough_to_pass_a_nearby_number` with
the arithmetic, and added to the TODO rather than changed at speed:
the floor exists for real rounding cases and needs a unit-aware
comparison, not a smaller constant.

## 2026-09-08 — The provenance guard's tolerance was six percentage points wide

Owner: "yeah def take another look at the 0.06 thing".

`_close()` allowed `max(0.005 * |a|, 0.06)`. The flat floor was the
problem, and it was worse than it looks, because a percentage is
compared in its /100 form as well as its face form. Two percentages six
points apart have /100 forms 0.06 apart, so they matched. An answer
claiming a 10.2% historical move was "sourced" by evidence saying 6.1%,
or anything from roughly 4% to 16%. The guard exists to catch invented
figures and it was accepting most of the plausible range for one. The
same arithmetic covered every small decimal the sandbox produces.

The first thing I tried was deleting the floor and going purely
relative. That closes the hole and breaks honest answers: half a
percent of 8.2 is 0.041, while an answer that writes 8.2 has only
claimed its source was in [8.15, 8.25). Real rounding is 0.05 there, so
lines with correctly rounded figures would start disappearing. A
smaller constant has the same shape of problem, one hole moved.

Precision is a property of the figure, so it is now read off the
figure. `_half_unit()` takes half the last digit the answer wrote
(8.2 -> 0.05, 21.36 -> 0.005, a bare integer -> 0.5), `_variants()`
carries the multiplier that produced each scaled form, and the
tolerance for a comparison is `max(half a percent of either side,
half_unit * that multiplier)`. So 8.1% is allowed a tenth of a point on
its face and a ten-thousandth on its 0.081 form, which is exactly what
writing "8.1%" claims.

Verified against all three real incidents: the bulch LULU answer still
strips its invented 10.2% history, the SansDE answer is still entirely
unsourced, and Abe's book still survives intact. 380 unit tests, 157
smokes.

## 2026-09-08 — daily-qc's pulse judge hit its turn cap; the ask phase landed

Owner: "github fail?". One red run out of twenty-two today. The whole
pilot chain (eleven reader runs, editor, graders) was green.

daily-qc got further than it has all week. Yesterday's two fixes both
held: the ask judge's bolded result line was accepted (step 9 passed,
`ask-qc/2026-09-07.claude.md` is committed) and the pulse-wait step
found today's artifact and set `ready`. The pulse judge then ran for
12.8 minutes and died on `--max-turns 80`.

`claude -p` emits nothing until it finishes, so the log carries exactly
one line, "Error: Reached max turns (80)", and the entire review is
gone. Same structural shape as the pilot readers on 09-03: a fixed cap
kills a long job and every bit of its work is discarded.

What made today long is not the pulse, which is 11,975 bytes against
12,181 on 09-04. It is that this was the first pulse after a four-day
gap (Thursday to Tuesday over Labor Day), so the judge had more claims
to spot-check and a longer gate trail to walk. Runs that finish take
5-6 minutes. That shape recurs after every long weekend.

Cap raised to 120. The failure message also now distinguishes a judge
that ran out of turns from a judge that forgot its result line: both
printed the same "finished without its result line" error, and only one
of them is a formatting problem.

The ask judge's own verdict for 09-07, now committed: 6 interactions,
2 clean, 4 INFRA, all four of them the empty-answer bug diagnosed and
fixed the same evening. It reached that on its own from the log.

Also noticed, not chased: `pulse-output/qc-headless/2026-09-07T14-06-56Z.md`
exists on pulse-data for a day with no pulse in the archive, committed
by the 09-07 19:20 run whose commit message carries an empty pulse
timestamp ("+pulse "). Almost certainly a file written by the Labor Day
run that failed before the ready-gate fix, then swept up by the next
run's `always()` commit. Harmless, one stale file, worth deleting if
the pilot graders ever read that directory.

## 2026-09-08 — A duplicate econ row, and three tiebreak gradings thrown away

Owner: "check omnicalendar I saw a duplicate econ event", plus the
pilot scoreboard after today's grading.

**The duplicate is real and reproducible.** Rebuilding the 09-09 sheet
locally:

```
 8:15  ADP Weekly Employment Change
 8:16  ADP Weekly Employment Change
13:01  10-y Bond Auction
16:30  API Weekly Statistical Bulletin
```

The feed carries the same print twice, one minute apart.
`build_calendar_day` has deduped EARNINGS since the sheet shipped (the
`_seen` set, for a name under two sessions) and has never deduped econ
at all. An exact (name, time) key would not have caught this, because
the times differ; the key is the name with a five-minute window, so a
name that genuinely recurs later in the day (a second auction, a second
speaker) keeps its own row. If the duplicate is the one flagged
important, the surviving row takes the higher impact with it, so a
collapse can never quietly downgrade a print. Sheet for 09-09 now
renders 3 rows instead of 4.

**The tiebreak delegation shipped 09-05 has never once worked.** The
graders workflow did exactly what it was built to do today: it found
the three dimensions where the two graders disagreed (grouping,
fidelity-shadow, fidelity-production), ran a third fresh agent on each,
and then `pilot_finalize_grade.py` rejected all three with
`argument --agent: invalid choice: 'tiebreak' (choose from a, b)`. The
scoreboard has read `tiebreak` and `owner` since the same commit; the
argparse choices list was never widened to match. Three gradings paid
for and discarded, and the day still shows its disagreements. The
`|| true` on the finalize call is deliberate (one bad grade must not
kill the run) and it is what let this sit for three days. A test now
pins the two lists against each other.

**Today's grades, for the record** (all three await the tiebreak that
will now actually run):

| dim | a | b |
|---|---|---|
| grouping fragmented mass | 41% | 60% |
| fidelity shadow | 93% | 80% |
| fidelity production | 27% | 53% |

Fragmentation is WORSE than the 24% of 09-04, on both graders, after
the 09-05 topic-label rewrite. Two caveats before reading anything into
it: the graders are 19 points apart, which is why it needs a tiebreak,
and today's corpus was 8 documents and 574 cards against a normal 20+.
But nothing here suggests the rewrite helped, and metric 1 is the one
frozen metric that has never passed. Worth a hard look before DAY1
rather than spending the single allowed post-DAY1 iteration on it.

Mechanism preservation was 1.00 for BOTH arms today, the first time
production has matched the shadow.

## 2026-09-09 — The filter blocks are marginal, and `empty: unknown` was useless

Two more things about the empty-answer path, both found by replaying
real logged prompts.

**The block is a threshold flicker, not a content verdict.** Stripping
the `**Voice.**` heading from the 09-07 football prompts removed 44
characters and flipped PROHIBITED_CONTENT to a clean answer. The FSA
prompt from 09-08 behaves the same way, and its Voice section was
already empty ("- (not needed for this question)") because the LEAN
profile-depth path had correctly dropped the quotes. Forty-four
characters decide it. These prompts sit on the filter's threshold, and
what tips them over is the surrounding profile bulk, not any one
passage. That is the shape tier 0 of the ladder was built for on
2026-08-04 (resend the identical prompt, which passed 5/5), so routing
these to the ladder should recover most of them on the first rung
rather than the fourth.

Worth noting for anyone reading the earlier entry: "the Voice sections
are the trigger" was too strong. Voice is the densest slur container
and stripping it helps, but on the FSA prompt there was nothing left in
Voice to strip and the block still cleared. The **Retarded takes.**
sections carry quoted slurs too, and the honest statement is that the
prompt is near the line and the profile block as a whole is what puts
it there.

**`empty: unknown` cost a live-API replay.** The FSA ask stamped a bare
`empty: unknown`, meaning: no prompt_feedback, no safety rating, and
not zero generated tokens. Three different situations produce that and
the ask log could not tell them apart, so the only way to learn
anything was to send the prompt to the model again. The stamp now names
each one: `no-response` (nothing came back at all), `no-usage` (a
response with no usage block), or `no-text-genN` with the token count,
so a thinking-only turn reads as such. A missing response also takes
the ladder now, since resending is the right move for it and calling it
a spent budget is not.

**Live check while writing this:** five asks in the fantasy channel in
the last hour, all clean. The channel-aware fantasy route fired on
both screenshot asks (`shape=fantasy -> LOCAL/FACT`, prefetching the
Sleeper roster), "rate this parlay" and a reply both routed on their
own, and one em-dash was caught by voice cleanup. No empty-answer
warnings in the window at all.

## 2026-09-09 — "you didn't read the screenshot": the Sleeper payload outranked the picture

Owner, while I was working: "you didn't read the screenshot a few
times". Three fantasy asks in twelve minutes, each with a roster
screenshot of a league that is NOT Omnibeta:

| time | question | players named back |
|---|---|---|
| 02:12 | rate my team in a half PPR league | Mahomes, Etienne, Stevenson |
| 02:14 | **using the screenshot attached**, rate my team in a half PPR league | Allen, Chase, Collins |
| 02:23 | **using the screenshot attached**, rate my team in a full PPR league | Mahomes, Etienne, Stevenson |

Two different screenshots of two different leagues came back with the
identical three players, and the asker had to add "using the screenshot
attached" to get a different answer once. That is the tell: the bot was
reading the asker's OMNIBETA Sleeper roster, not the picture.

The route is working exactly as designed and that is the problem. A
fantasy question in the football channel prefetches the asker's Sleeper
roster and injects it with "LEAGUE STATE ... Authoritative over chat and
SQL for that topic." Nothing in that block knows an image exists, so
when the screenshot is a different league the payload wins.

Fix: `inject_text` takes `has_images`, and for the fantasy tool only it
appends a line saying an image is attached, that the payload is the
Omnibeta league, and that a roster in the image is a DIFFERENT team and
is the one being asked about. Relabelled rather than suppressed,
because an asker can legitimately attach a picture and still be asking
about Omnibeta.

Phase 2 never receives the `images` list, so the flag is derived from
`initial_parts`, where the image parts already are. My first version
read `images` directly and `smoke_pyflakes_undefined` caught it as a
guaranteed NameError on the first fantasy ask with a prefetch, before
it could reach anyone.

Not affected: yesterday's 03:08 draft-screenshot ask, which prefetched
`topic=situation` and read the image correctly, and today's "rate this
parlay", which pulled the screenshot out of the REPLIED-TO message and
quoted its exact numbers ($18.98 to win $3,199.16, 13 legs). The
reply-to-image path and the look-back path are both fine; only the
fantasy roster injection was overriding what the model could see.

### Same day, owner's correction: screenshots in general, and no numbers

Two things wrong with the fix above.

**It was roster-shaped.** Owner: "not all screenshots are lineups, just
consider screenshots in the context in general." Right, and the flaw is
not specific to fantasy either. EVERY prefetch is injected as
authoritative and NONE of them were fetched by looking at the picture,
so a chart screenshot loses to LIVE PRICES, a fill loses to the ledger,
an article loses to the calendar, exactly as an outside-league roster
lost to the Sleeper payload. The note is now on every tool and says the
image is part of the question whatever it holds and is the subject
where the two disagree; only fantasy keeps the extra line about
Omnibeta, because only fantasy has a league of its own to be confused
with.

**The football answers carry no numbers.** "Elite volume", "high-floor
engine", "league-winning ceiling" — three rated rosters, not one
projected point. The data was there the whole time:
`lookup_fantasy_league topic=projections` returns Sleeper's weekly
`pts_ppr` for EVERY NFL player, not just the ones on a roster in this
league, so a screenshot from an outside league can be rated on real
figures. Nothing told the model that, and the tool doc did not say the
topic reached beyond the league. Both now do, and the injected block
carries the rule: every player rated carries a projection, a rank or a
record, and adjectives without a figure are not analysis.

Google was never the constraint here. `GOOGLE_POLICY[FANTASY]` has been
True since 09-03 precisely so injuries and player news can be searched.
The model had search and league-wide projections available and used
neither, because nothing asked it to.

### The `stats` topic: what players actually scored

Owner asked whether users can ask for stats, injuries and projections
and whether Python would work out the API calls. Two corrections worth
recording, because the premise shapes what to build next.

Sleeper access is a FIXED MENU of topics, not the API. And the Python
sandbox has NO NETWORK (the prompt says so: pull live numbers with the
tools first, then compute). Python cannot reach an endpoint the tools do
not already cover. On the 02:14 screenshot ask the model DID run code
three times — `executable_code` and `code_execution_result` are in the
production log — and still answered in adjectives, because with an
outside-league screenshot there were no numbers in context to compute
on. The constraint was data, not Python, the same way the football
questions last week had search available and never used it.

The gap that was real: `fetch_weekly_stats` had existed since the module
was written and NO topic called it. Projections were reachable, results
were not, so "what did he actually put up" had no answer. Now
`topic=stats` returns actual pts_ppr with each player's projection
beside it. With `member` it is that manager's starters and bench (which
is what "who should I have started" needs); without one it is the week's
top scorers across the NFL, which is what a screenshot from an outside
league needs. Unavailable endpoint returns status=empty and says so
rather than inventing points, matching the projections branch.

Owner declined the second half (a mandatory projections prefetch when an
image is attached in the football channel); it was built and reverted
unmerged. The model reaches for the topic itself, now that the injected
block and the tool doc both tell it to.

## 2026-09-09 — Why metric 1 keeps failing: readers never agree on a label

Measured, not inferred. For every pilot day, how many topic labels are
used by MORE THAN ONE document, and how much card mass sits under one:

| day | docs | cards | labels | shared labels | mass under a shared label | cards/label |
|---|---|---|---|---|---|---|
| 09-02 | 23 | 536 | 235 | 3% | 6% | 2.3 |
| 09-03 | 23 | 911 | 270 | 11% | 36% | 3.4 |
| 09-04 | 24 | 693 | 299 | 7% | 18% | 2.3 |
| 09-06 | 18 | 589 | 114 | 1% | 5% | 5.2 |
| 09-07 | 6 | 237 | 63 | 3% | 6% | 3.8 |
| 09-08 | 16 | 591 | 124 | 2% | 7% | 4.8 |
| 09-09 | 37 | 1304 | 304 | 8% | 22% | 4.3 |

Between 64% and 95% of card mass every day sits under a label no other
document ever used. The soft grouping key does almost no grouping.

That is structural, not a prompt defect, and it follows from the design:
each document is read in its own session and the reader invents a
free-text `topic` blind to every label every other reader chose. Two
readers handed the same Fed-hiking story write "fed rate hike risk" and
"fed rate hike pricing" and the ledger has no way to know they are one
subject. Tracing the graders' own fragmentation sets through the card
files shows both mechanisms: Hormuz and JPM EM were ONE document split
three ways (a prompt problem), while Fed, ECB and AI-capex financing
were three or four documents each coining their own (an architecture
problem).

The 09-05 topic rewrite did work on the half it addressed. On
comparable corpora, labels per day fell (299 on 09-04 with 693 cards ->
124 on 09-08 with 591) and cards per label roughly doubled (2.3 -> 4.8).
Within-document consolidation improved; cross-document agreement did
not move, because no per-document prompt can move it.

Two honest responses, both owner calls, both still free because DAY1 is
not set:

1. **A shared vocabulary at read time.** A committed list of ~40 market
   subjects every reader picks from, plus a required `other:<free text>`
   escape, with the ledger reporting how much mass lands in `other:` so
   a bad vocabulary shows up immediately. This is NOT the post-hoc
   normalization the spec deletes (no embeddings, no cosine, no merge
   map) — it makes labels agree at creation instead of merging them
   afterwards. It does change the reader contract the spec describes.
2. **Reconsider what metric 1 gates.** Spec section 6 already decides
   that topic labels are SOFT and only warn, while instruments and
   figures are hard keys that block, precisely because labels "will
   fragment sometimes". Section 8 then makes label fragmentation a
   pass/fail criterion with a 10% cap. Those two sections disagree, and
   they have disagreed since the spec was written. Worth resolving
   deliberately rather than discovering it at day 10.

Not resolved here. Changing a frozen metric to fit a result is the
wrong move and I am not proposing it as a fix; the tension is real and
predates any measurement, which is why it goes to the owner before the
clock starts rather than after.

## 2026-09-09 — /ask code review: ten plumbing defects fixed

A code-review pass over the /ask pipeline (eleven phases, router, tool
layer, provenance guard) found 23 confirmed defects. Ten shipped as
fixes today; every one was a value carried by hand between phases and
dropped on the way, a class the 09-01 split created when implicit
shared locals became explicit return tuples.

1. Phase 8 None-initialised `response`, never received it, returned it.
   Phase 9 read None on every non-TA turn, so finish_reason and the
   block flags were unknowable, the stamp was always `no-response` and
   the MAX_TOKENS short-thinking retry was unreachable. This was the
   common cause of the 09-04, 09-07 and 09-09 empty-answer incidents;
   the three fixes shipped in that window each tuned phase 9's reading
   of a response phase 8 had already discarded. `response` is now a
   pass-through parameter, and
   tests/test_ask_grounding_passthrough.py carries an AST check that no
   phase returns a name it only assigns on some paths.
2. `_tally_retry_usage` did `nonlocal` on phase 3's parameter copy of
   the token total, which phase 3 never returned; all 19 retry tallies
   were dropped and record_actual under-counted. It is a `_RetryTally`
   object now and the caller adds `.total` at record time. The ask log
   shows `retry-calls: N` and the turn latency.
3. `_execute_market_price` never set `status`; the phase-7 price
   backstop gated on status == "ok" and had never fired.
4. Prefetch trace entries carried `prefetch:<status>`, which no net or
   validator recognised, and a timed-out prefetch counted as a source.
   Statuses are raw now with `via: prefetch`; `_trace_has_source`
   ignores failed and timed-out entries; `timeout` joins the failed set.
5. The grounding hedge's arrow flipped figure_provenance into bullet
   mode and stripped prose bodies whole. One `_UNVERIFIED_HEDGE`
   constant, detached before the check and reattached after; bullet
   mode now needs a leading arrow or two of them.
6. `_ask_evidence_text` admitted the full prompt turn (dossier numbers
   back in) and the model's own earlier text. Both excluded.
7. The plumbing regen replaced `answer` but not `response`.
8. The repetition retry hand-listed tools, ignoring TOOL_POLICY and
   dropping code_execution; it derives from the routed config now.
9. The transient 5xx re-entry dropped `out_meta`, so a book published
   after a retry was never recorded.
10. Eleven synchronous db calls in async phases and three executors
    moved onto asyncio.to_thread. Doing so exposed a latent deadlock in
    db.get_connection: the first schema init on a worker thread
    re-entered the non-reentrant schema lock via
    backfill_orphan_exit_links. RLock, and the thread-local connection
    is published before the schema runs.

Confirmed but not fixed today (structural, need their own pass): a
turn-state object replacing the return tuples; one `_regen` helper for
the twelve hand-rolled retry sites; classifying the raw response once
at the call site; a single Evidence object; the fixture harness calling
classify() without the production kwargs; nine `_ask_meta` keys written
and never rendered; the ticker-extraction duplicate (CLAUDE.md TODO);
prompt text that duplicates code validators (ask_prompt.py, policy rule
1). The transient retry still re-runs phases 0-2 instead of the one
failed call.

## 2026-09-10 — Metric 1: the grain was the defect, not the wording

Decomposing grader A's 09-09 fragmentation sets against the raw labels:
of 39% fragmented mass, 4 points were wording variants of one subject
and 36 points were the grader merging several distinct-word families
into one subject at note or theme grain ("one GS research note", 29
labels including shelter disinflation into "Fed September hike odds").
Readers emitted a median of 8 labels per document (max 21) against a
prompt that said "rarely more than five"; 47 of 62 documents exceeded
five. Duplicate cards: 0%. The two frozen documents (reader.md,
graders/grouping.md) were using different grains and metric 1 measured
the disagreement. The two graders themselves landed at 41% and 60% on
09-08 for the same reason.

The fixed ~40-word vocabulary proposed on 09-09 was withdrawn: it fixes
the 4 points, not the 36, and half the day's subjects (Rothera, TSMC
packaging, the consumer-inertia basket, Communacopia) are single-note
stories no list anticipates.

Shipped instead (owner approved 2026-09-10; spec §4 amendment):
1. One grain in both prompts: a label is a pulse theme, the unit that
   would be one BRIEF. At most five labels per document, enforced by
   pilot_verify_cards.py through the existing in-session re-ask
   (`label_contract` in the re-ask file).
2. KNOWN_LABELS: scripts/pilot_known_labels.py hands each reader the
   ledger window's labels so far, rebuilt per document inside the
   sequential reader loop, with a reuse-or-coin rule.
3. `macro_key`: a closed-set hard key on cards with no instrument
   (calendar whitelist + RECAP snapshot + POSITIONING/CREDIT/FISCAL/
   GEOPOLITICS + OTHER; scripts/pilot_config.MACRO_KEYS). The ledger
   groups them as `_MACRO:<KEY>`; the verifier coerces an invalid key
   to OTHER on the final pass and counts it.

Threshold and window unchanged. Prompt edits restart the pilot clock;
DAY1 was never set, so nothing is lost. Next: one shadow day, read the
new fragmentation number and the OTHER share, then commit DAY1.

## 2026-09-10 — Industry events on the omni-calendar, from the corpus

Spec 2026-09-09 (conference sessions from the corpus) approved and
wired end to end the same day. The deep-analysis schema gains
`conference_sessions`; `ai_analysis/conference_sessions.py` keeps a
slot only when its schedule line is a normalized substring of the
extracted text (enforcing, unlike the warn-only key_data_points check),
its month and day are printed in the text (year from the analysis
date, rolled forward past 30 days stale), and it names a US-listed
ticker. `db.conference_sessions_for_date` reads the latest analysis per
PDF over the last 7 days and dedupes across documents.

Sheet: `build_conference_rows` admits NASDAQ-100 names only
(`NDX_TICKERS`, hand-refreshed, as of the 2025-12-22 reconstitution),
one row per conference per day, tickers in market-cap order via the
same cap cache the earnings ranking uses, ET start of the first
admitted slot, day-level when the printed zone is unknown. Rendered as
a two-column events section (Economic left, Industry Events right,
wrapping) only when a row exists; the lineup signature includes the
rows so the 7:30 refresh sees a morning-note agenda. On the real 15746
text: 13 proposed slots, 12 verified (the opening-remarks slot names no
ticker), 9 admitted slots, one row starting 14:50 ET with MSFT first.

Shadow phase skipped on the owner's call; the anchor and printed-date
rules are the safety. First live test is the next conference day the
corpus writes up.

## 2026-09-11 — Pilot job timeouts raised: readers 45 to 90, graders 60 to 150

Three consecutive hourly reader runs (11:00, 12:00, 13:00 UTC) were
killed at 45:00 working through the 35-document Communacopia
takeaways backlog at 6-7 minutes per document; each persisted what it
finished, so no cards were lost, but reads ran past the 13:55 editor.
The 09:00 failure was one document at the 20-turn cap, retried and
read on the next run. The new label contract is not the cause: every
document read today is at five labels or fewer, one re-ask, zero
macro_key coercions.

The graders run for 2026-09-10 (the first clean full day: 69
documents, 2381 cards, zero unread at edit) was killed at 60:00 with
no grade written. Timeout raised to 150 and the day re-dispatched with
`date=2026-09-10`.

## 2026-09-11 — /ask reported July's CPI as August's print; FRED actuals fixed

At 08:31 ET, one minute after the August CPI release, /ask answered
"what was the economic print" with +0.07% m/m, 3.52% y/y, core +0.22%
and 2.67%. BLS printed +0.4%, 3.4%, +0.3%, 2.4%. Both defects were in
report/fred_data.py, the layer that fills `actual` on ForexFactory rows
(FF carries no actuals):

1. FRED had not yet posted August. The staleness guard was "within 75
   days", so July's observation (72 days) rode on the August row as
   its actual, with `actual_period: 2026-07` that the model ignored.
   The figure-provenance guard passed the answer because every number
   WAS in the tool payload. Fix: for a monthly series the observation
   must be the release's reference month (the calendar month before
   the row date), else actual stays None and the row reads
   past_no_data. The econ injection now tells the model what
   past_no_data means.
2. FRED has no October 2025 CPI observation (the shutdown month), and
   the year-over-year transform took "12 observations back", which was
   13 months back: 3.69% for August against the printed 3.4%, and the
   same for every y/y the feed has produced since November 2025.
   Fix: comparison months are looked up by calendar date; a missing
   month yields None.

tests/test_fred_actuals.py replays the 08:31 scenario and the missing
month against the real FRED window fetched that day.

## 2026-09-11 — Economic prints posted at release (report/print_watch.py)

Owner ask after the CPI incident: post prints as soon as they exist,
the way the reminder job posts calendar events. The agencies publish at
the release second and the BLS public API had the August CPI index
within a minute of 8:30 while FRED lagged by hours, so the watch reads
the agency, not an aggregator.

Two scheduler jobs (8:29 and 13:59 ET, weekdays) arm when the
ForexFactory calendar lists a supported US release that day, poll the
agency from the release second until the REFERENCE month appears (CPI
on the 11th reports August; July is not the print), then post one
embed per release with actual / consensus / prior per line and record
it under /data/print-alerts so a redeploy cannot repost. Supported:
CPI and the Employment Situation (BLS, seven series in one request),
FOMC target range and vote (Federal Reserve press feed, statement
parsed). PCE and GDP wait on a BEA key; ISM has no free source and is
out by owner decision. Channel: PRINT_ALERT_CHANNEL_ID, falling back to
REMINDER_CHANNEL_ID; off when both are empty. BLS_API_KEY optional
(25 requests/day unregistered, so the watch polls every 30 s without
it, 10 s with it).

The same fetch is now the FIRST source of `actual` for the econ
calendar rows (`enrich_rows_with_agency_actuals`, ahead of FRED), so
/ask and the pulse context carry the print as soon as the agency has
it. Fixtures: the real BLS payload and FOMC statement fetched today.

## 2026-09-11 — X auto-post of the omni-calendar (dry run until keys land)

Owner ask. `report/x_client.py` signs OAuth 1.0a with the stdlib (no
new pin; the signature is tested against X's published worked example)
and uses API v2 for the media upload and the post, with the legacy
upload host as a 404 fallback. The nightly calendar job posts the same
PNG to X after the Discord post is recorded, with a caption from
`report/calendar_caption.py` trimmed to 280 chars from the least
important end (unimportant econ rows, then extra tickers, then the
industry line). `X_POST_ENABLED=false` logs the caption as a dry run;
one post per date via `/data/x-posts`. The four keys come from the
owner's X developer portal; `scripts/x_post_test.py --check` proves
them read-only, `--post` makes the one owner-approved public test.

## 2026-09-12 — First day under the label contract: fragmentation 9-16%, mis-merges are the new failure

2026-09-11 was the first day read entirely under the pulse-theme grain,
the five-label cap and KNOWN_LABELS. Labels: 66 for 35 documents (476
for 62 on 09-09), 26 shared across documents, "fed september hike odds"
used by 12 documents. Grader fragmentation: 16% / 10.8% / 8.9%
(tiebreak) against 30-60% on every earlier day. Every document read
was at five labels or fewer with one re-ask and no macro_key coercion.
The day itself is VOID (11 unread at edit, the reader timeouts fixed
the same afternoon).

The new failure is the cap's side effect: readers at five subjects
folded leftovers into grab-bags ("ai narrative" = a product launch, a
data-center investment, a stock rebound and a researcher's
resignation; "european equity buy ideas" = Germany macro, Greek banks,
Inditex). All three graders flagged them as theme-changing mis-merges.
One grader call goes the other way: opposing stances on one subject
("ai infrastructure demand") are one subject, and `direction` carries
the stance.

Fix, before DAY1: reader.md gains "a label names ONE subject" with a
declared overflow label `misc` that does not count toward the cap;
pilot_verify_cards exempts it and records `misc_cards`; the grader
rubric counts `misc` as ungrouped mass (never a mis-merge) and states
that stances are not subjects. Next: one clean shadow day under the
90-minute reader timeout, read fragmentation, mis-merges and the misc
share, then commit `pilot/DAY1`.

## 2026-09-14 — Omni-calendar: second source on report dates, names wrap

QC of the Monday 9/14 sheet (posted Friday 3 PM ET). Cracker Barrel
rendered before the open with a ±5.9% move; the company had announced
fiscal Q4 for Wednesday 9/23 on 9/9, and Finnhub still carried the old
date. The move was wrong for the same reason: priced on the 9/18
weekly, which the real report falls after, so it was an ordinary
week's range. Dave & Buster's (after close, ±17.5%) was correct;
Kestra ($1.3B, no listed options) and Radiant ($390M, no priced
straddle) were correctly dropped by the floor rules.

Fix: `news_data.fetch_nasdaq_earnings_symbols` reads Nasdaq's earnings
calendar for the date, and `build_calendar_day` drops a Finnhub row
Nasdaq does not list for that date, recording it in
`CalendarDay.date_unconfirmed`. Nasdaq often omits the session, so it
confirms the date only. When Nasdaq is unavailable the sheet keeps
Finnhub alone. Over 2026-09-01..17, 10 of 175 Finnhub confirmed-session
names were absent from Nasdaq's list, almost all micro-caps.

Also: earnings names wrap to two lines instead of truncating ("Cracker
Barrel Old Co…"); a name longer than two lines truncates on line two.
Monday's posted sheet was rebuilt with the fixed code and the image
replaced in place (owner approved). The /ask earnings slate
(`ask_tools._execute_earnings_slate`) reads the same Finnhub feed and
does not yet apply the check.

Follow-up the same morning. Rebuilding the 9/14 sheet with the new
code surfaced a false industry-event row: "GS Annual Global Consumer &
Retail Conference" with MSFT. The stored session's anchor was a
week-ahead line carrying two events, "GS Hosts: AVAV + TEL + MSFT GS
Annual Global Consumer & Retail Conference - CHWY", and the extractor
attached all four tickers to the conference. The anchor check passed
because the line is verbatim; it never checked which tickers belong to
the named conference. `conference_sessions.tickers_for_conference`
now drops a ticker written before the conference name on its line
(slot lines that never name the conference, and tickers resolved from
a company name like "Walmart", are unaffected). It runs at extraction
and again in `build_conference_rows`, so sessions already stored with
the extra tickers are filtered too. The corrected 9/14 sheet was
posted with the conference band suppressed and the Cracker Barrel row
removed; the image on the original message was replaced in place.

## 2026-09-16 — Weekly Claude usage limit outage (9/15 16:00 ET to 9/16 17:00 ET)

The subscription behind the pulse routine, the pilot workflows and the
nightly QC hit its seven-day limit on 2026-09-15 and reset 2026-09-16
21:00 UTC. The worker (Gemini /ask, ingestion, calendar sheets, print
watch) was unaffected: calendar sheets posted every day and the print
watch posted the 9/16 FOMC hike at 14:00:30 ET, 30 seconds after the
statement (verified: +25bp to 3.75-4.00%, 12-0).

Lost: no pulse on 9/15 or 9/16 (both routine runs rejected by the
seven-day limit within a second; the routine stays enabled and its
next run is 9/17 10:03 ET, whose window is since-last-daily, about 71h,
inside the 96h ceiling). 19 of 28 GitHub runs failed: every pilot
reader at the auth check, the 9/16 editor (correctly refused to write
from stale cards), and the 9/16 ask-qc. Pilot 9/15 and 9/16 are void.

Remedies the same evening:
- Cancelled the reader run that started at the reset, which was
  spending the fresh budget on 9/15 documents no editor window reads.
- Retired the 88 unread 9/15 documents with given-up read-failure
  markers, so readers go straight to 9/16 and 9/17 and the 9/17 editor
  can have a complete window.
- The four workflow auth checks now say "usage limit reached, the
  token is fine" when the CLI reports a limit; each had told the owner
  to re-mint a working token.
- smoke_fred_calendar's core m/m test built its observation as "now -
  15 days", which only matched the release's reference month in the
  first half of a month; it began failing on 9/16 with no code change.

Budget, measured: the limit reset 2026-09-09 21:00 UTC and ran out
2026-09-15 around 20:00 UTC, about six days, on a week that included
the heavy 9/14 Monday. At its current size the pilot does not fit in
one weekly limit alongside the production pulse. This is the plan
section 5 headroom reading, and it needs an owner decision before DAY1.

## 2026-09-16 — Pilot readers move to Sonnet (owner call)

Both reader tiers now run claude-sonnet-5. Opus read the top-bank tier
(19-29 documents a day, 20-50 turns each) and that was most of the
pilot's draw on the shared seven-day limit that ran out on 9/15. The
grades never showed a fidelity gap between the Opus and Sonnet tiers.
The editor stays on Opus (one write a day) and the graders on Sonnet.
The tier label is kept so the ledger's tier split and the graders'
per-tier numbers keep working. Model change, so the pilot clock
restarts; DAY1 was never set. Next lever if the week still does not
fit: one grader per dimension with a tiebreak only on disagreement.

## 2026-09-16 (evening): review of the outage week, five fixes

Root causes sorted (owner asked): seven of eleven failures were process
(a known step deferred, a refactor left half done, an assumption never
measured); two were code that computed the wrong answer (CPI, both
fixed 9/14); one was an external format change with no deterministic
guard. The guards that share the Claude subscription (ask-qc, daily-qc,
graders) all went dark with the thing they guard; the deterministic
checks kept working.

1. /ask citation markers. Gemini's grounded answers carry "[cite:
   1.2.8]" / "[1.0.1]" in the model text; four shipped 9/14-16.
   `_strip_citation_markers` runs in phase 10 before the sources footer.
2. /ask Fed chair. An answer named a "Powell press conference" (9/15).
   world_context held the fact but /ask never saw it: the econ tool's
   docs and error string spelled the predecessor three times. Tool
   strings read `world_context.FED_CHAIR`; the runtime header carries a
   FED CHAIR line; `tests/test_fed_chair_single_source.py` fails any
   source line in discord_bot/report/github_bridge/scheduler that names
   the predecessor without the chair.
3. /ask price backstop fetched 'ATM' as a ticker (COST implied-move
   answer, 9/14). `_answer_price_tickers` now reads through
   `ask_router.extract_tickers`; the duplicate regex and its stopword
   set are gone (the CLAUDE.md TODO).
4. Calendar: the Nasdaq date rule only removed rows. Finnhub parks
   names on estimated dates (FedEx 9/16, General Mills 9/15, both
   unannounced or announced elsewhere); Nasdaq had GIS on the announced
   9/23 and nothing ever added it. `fetch_nasdaq_earnings_rows` returns
   session and cap; a Nasdaq-only name at or above MIN_CAP_ALWAYS_SHOW
   is added with Nasdaq's session (`CalendarDay.nasdaq_only_added`).
   Checked before changing anything: the 9/16 and 9/17 sheets were
   right. FedEx has not reported (zero post-market volume 9/16, no IR
   event), so dropping Finnhub's 9/16 row was correct.
5. Pilot machinery (shakedown, no prompt or model change): the editor
   waits up to 40 minutes for `unread == 0` before packing (five of
   thirteen days were VOID from editing over unread cards); the worker
   skips a dispatch when the workflow already has a queued run (GitHub
   cancels the older queued run in a concurrency group, which showed as
   two "cancelled" reader runs after the 9/16 reset).

Suite: 475 unit, 157/157 fast smokes. pytest had vanished from the
3.12 interpreter (reinstalled).

## 2026-09-17: plan 4.3 stress datapoint, editor half (uncounted)

Run 35168496885, dispatched 00:55 UTC with the editor workflow's new
`days=3 stress=true` inputs over the same 09-02..09-04 window the ledger
half used on 9/5 (now 70 documents, 2,478 cards, 550,864-char pack; the
five extra documents were read after 9/5). Written to
`pilot/stress/2026-09-04-3d.md`, never `shadow/`.

- Opus wrote it in 4.5 minutes wall, one pass, no re-ask, no structural
  problems. The pack was not too big for the model.
- Output did not scale with load: 1,855 words, one main event (389
  words) and eight briefs (about 183 each), inside the range of normal
  days (7-9 briefs, 1,431-1,943 words). Three days of cards produced one
  day's pulse, so at 3x load two thirds of the corpus never reaches the
  reader. That is the editor.md format doing its job, and it is the
  number the verdict's scope limit (plan 6) should quote.
- Citation reach fell: 116 distinct cards cited, 4.7% of the pack, against
  5.9% (9/14, 1,820 cards) and 12.5% (9/04, 942 cards). Quintiles
  [29, 29, 29, 20, 9]: the last fifth of the pack, the second half of
  09-04's documents, got 9 citations against 29 for each of the first
  three. Position bias at load, below the metric-4 flag (edge share
  0.328 against 0.70).
- The committed meta shows 20 citation failures. 18 were the verifier,
  not the editor: "40k" in a card against "40,000" in the pulse, "536
  basis points" against "5.36%", "3.5-3.75%" against "3.5% to 3.75%",
  "Y143.1tn" against "143.1 trillion", "14.5x" against "14.5 times", and
  "September 16 meeting" read as the figure "16m". `_numbers` now folds
  magnitude suffixes and words into the value, treats bp and % as one
  figure, gives the first bound of a "a-b%" range its own %, and stops a
  suffix inside a following word. Re-verified locally against the same
  pack: 2 failures, both real (a 29% token-price figure cited to
  Broadcom guidance cards; a repeated 162,000 with no citation).
  Earlier shadow metas were written under the old matcher (9/14: 8
  failures, likely the same class) and are not rewritten.
- `leans: 0` in the meta is the production parser's contract, not a
  defect: parse_lean_block keeps long/short lines with a cashtag, and
  the editor's nine leans were neutral cashtags or long/short on named
  instruments. The pilot's leans count is a weak signal; read the block.

Plan 4.3 is complete (ledger half 9/5, editor half 9/17).

## 2026-09-17: code review of the day's five commits, ten findings fixed

High-effort review pass (eight finder angles, one verifier per
candidate) over 526aa49d..002a5054. Ten findings, all fixed in one
commit:

1. `has_queued_run` filtered `status=queued`; a run held by a
   concurrency group is `pending`. Now reads the newest five runs and
   matches any waiting state.
2. A Nasdaq-only calendar name kept only symbol and hour; `_resolve_caps`
   still made a Finnhub profile call for it and a failed call (cached as
   0) dropped an unconfirmed-session name after it was logged as added,
   while "+N more" counted it. Nasdaq's cap and name now stand in when
   Finnhub gives nothing. The dead `fetch_nasdaq_earnings_symbols`
   wrapper is gone.
3. `days` was honoured without `stress`, so a merged pack could land in
   `shadow/`; `stress` was an exact lowercase compare in two places.
   Inputs are normalised in one step; a graded edit is forced to one
   day.
4. `git add shadow/ stress/` fails outright when either directory is
   absent (git validates every pathspec first), which would have
   discarded the day's shadow after any pilot-data reset. Only the
   directory the run wrote is added.
5. The editor waited on the global unread count, so a PDF published
   after 13:55 made it wait for the 14:00 readers and pack cards
   production never saw. It waits for the set that was unread when the
   job started, and stops sleeping on the last check.
6. The price backstop's cashtag-first extraction dropped a bare ticker
   in the same sentence as a cashtag; `extract_tickers(all_tiers=True)`
   keeps both, and index names go to the tool in Yahoo's caret form.
7. The router's stopword set lacked 31 acronyms the retired set had
   (USD, a real ETF, was becoming a quote for "1.08 USD"). Merged; the
   clapback extractor reads the same set, so the ATM defect is closed
   there too and bot.py no longer carries its own literal.
8. The citation strip missed comma lists and undotted `[cite: 3]`, and
   its `\s*` ate the paragraph break when a marker opened a line. It now
   covers `[1]`, `[1, 2]`, dotted paths and `cite:` forms with horizontal
   whitespace only, and the prompt's `[1]/[2]` clause is deleted (95
   chars), which pays for the FED CHAIR header line (104 chars): net +9,
   recorded in the ask_prompt.py ledger. `test_size_ceiling` measures
   the built instruction, header included.
9. Verifier: the range regex minted "2026%" from "by 2026 to 3.5%"; the
   bp/% alias was symmetric (a card's 3.5% passed a pulse 350bp); a
   failure named the alias form. Aliases are card-side and bp->% only,
   years are not range bounds, the pulse side keeps the sentence's own
   form, and multiples are bare numbers. The stress pack still verifies
   at the same 2 real failures.
10. Conventions: the header addition had no paying deletion (see 8).

Suite: 490 unit, 158/158 full smoke sweep.

## 2026-09-17: print watch posts to a channel list; PCE added

`PRINT_ALERT_CHANNEL_ID` is a comma-separated list (set to stonks-yapping
and test-channel on the worker); one embed per channel, sent together,
one ledger entry, a failing channel logged and the rest still post.

PCE (owner ask): BEA NIPA table T20804, monthly, series DPCERG (PCE price
index) and DPCCRG (excluding food and energy); four lines like CPI (m/m,
y/y, core m/m, core y/y), consensus only on core m/m because that is the
one line the calendar feed carries. Lines carry a `source` (bls | bea),
`fetch_observations` picks the agency per line, and the feed enrichment
does the same, so the econ tool gets PCE actuals ahead of FRED. BEA has
no unregistered tier: without `BEA_API_KEY` the release is never armed
(logged) and the feed skips it. Series codes are from BEA's published
table layout and not yet confirmed against a live response; `parse_bea`
logs what the table carries when a requested code is missing, so the
first live run tells us. Next PCE print: Friday 2026-09-25, 8:30 ET
(August personal income and outlays).

## 2026-09-17: a tool payload is a source for the grounding net

The 9/15 LEN answer (date, consensus EPS and revenue all from
lookup_earnings_date) shipped with the "Couldn't verify" hedge because
the net asked only whether Google had sources. `_tool_sourced` runs the
figure-provenance matcher over the turn's evidence (tool payloads,
injected blocks, context) when a data tool returned data; if every
figure in the answer is there, the answer ships without the hedge,
labelled `in-voice:tool-sourced`. Checked ahead of both hedge branches
(local-skip, context-dep-skip). Same matcher the phase-9 provenance
guard uses, so one definition of "sourced".

## 2026-09-17: the quiet calendar is correct; two rules tightened

Owner asked whether the empty earnings bands are right. They are:
9/14-18 is the trough between seasons. Nasdaq's $5B-plus list is
empty on 9/18, and the next real slate is 9/22 AZO/KBH, 9/23
CTAS/PAYX/GIS, 9/24 COST/DRI/SNX, 9/29 CCL/KMX, 9/30 MU/JBL/FDS, 10/1
ACN/NKE. Finnhub still parks several of these on estimated dates or
with no session (FDX 9/16, GIS 9/15, NKE 9/28, DRI no hour, ACN after
the close); Nasdaq has the announced dates and sessions.

1. Nasdaq's session now fills a blank Finnhub hour and wins a conflict
   (`CalendarDay.session_from_nasdaq` records which rows). DRI and GIS
   become confirmed before-open rows instead of flagged after-close
   ones; ACN moves to the right band.
2. An empty band prints "no names at scale confirmed"
   (`calendar_render.EMPTY_BAND_TEXT`); the feed-down case keeps its
   own "unavailable tonight" line.

## 2026-09-18: the readers' cron is gone; the worker is the only clock

Owner saw GitHub failure mail and asked. Every workflow failure in the
repo is from the 9/15-16 usage-limit outage (last ones: daily-qc 18:34
and pilot-readers 19:54 UTC on 9/16); nothing has failed since, and
9/17 and 9/18 are clean. The only non-green event after the fixes was
the 01:00 UTC reader run on 9/18 hitting `timeout-minutes: 90` on the
overnight backlog, which GitHub reports as cancelled and does not mail.
Cards persist per document inside that step, so the run lost nothing
and the 05:00 run took the rest.

`pilot-readers.yml` kept its own `schedule:` block alongside the
worker's dispatch, which is why runs appeared at 13:22, 20:04, 23:26
and 05:40 off the declared slots, and why four runs were cancelled on
9/16: cron and dispatch fire the same minute and the concurrency group
cancels whichever arrives second. The block is deleted. The worker
already dispatches every slot the cron declared (hourly 09-14 UTC and
13:15 on weekdays, 01/05/17/21 daily), and a test now pins that no
pilot workflow declares a cron and that the readers' slots are all
covered. The cron was never a real fallback: a worker that is down
publishes no source text for a reader to read.

Open, unchanged: the 90-minute reader cap on a heavy overnight backlog.
The fix is parallel readers (proposed, not built).

## 2026-09-18: the grader was wrong twice; leans leave the editor contract

Verified the 9/17 shadow's three adverse grades against the raw source
text before amending anything. Two of the three were grader errors.

1. "Fabricated detail: no source mentions Wingstop, Darden, or
   long-only selling." The shadow wrote "The desk saw long-only selling
   in Wingstop and Darden with nobody stepping in [c53]", card c53
   carries it, and source 16549 says verbatim: "We've seen LO supply in
   the space (WING, DRI) with zero defense from consumer dedicated
   players." WING is Wingstop, DRI is Darden. The reader expanded
   tickers to names correctly; the grader searched for the names,
   found tickers, and called it invention.
2. "Invented statistics" on exploit timing. Source 16560 says verbatim:
   "Mean time to exploit is now negative 7 days... Historical
   compression: 63 days (2018) to 32 days (2021) to 5 days (2023) to
   negative 7 days (2025)" and "132 new CVEs per day but remediate only
   16% of known". Every figure is there. The PDF extraction wraps the
   phrase across lines ("63" / "days (2018)"), so a literal search for
   "63 days" returns nothing.
3. The narrative gloss ("the bond market took the projections at face
   value") is a real defect. No card supports it.

Corrected, 9/17 shadow is 14 of 15 faithful (93%) with one genuine
miss, not 80% with three. Production's failures spot-check as real: the
BofA S&P target of 7,800 has zero hits in the corpus and the $9.9bn
dealer position none either, so the gap between the arms is wider than
the scoreboard shows.

Changes, all made before DAY1 while prompts are still editable:

- **graders/fidelity.md and brief_fidelity.md** gain a binding "Reading
  the source text" section: search a single distinctive token rather
  than a phrase because lines wrap; a ticker and its company are one
  entity and expanding desk shorthand is faithful; figures arrive in
  different spellings. Any verdict other than faithful now requires two
  distinct failed searches, recorded in a `searched` array on the
  sentence, so a grader error is auditable instead of invisible.
- **editor.md** drops the `## _LEANS` block and gains rule 5: a
  sentence that characterizes a market, a participant or a reaction is
  a claim and needs a card. That rule targets the one real 9/17 defect.
- **Leans are gone from the pilot** (owner call). The block fed no
  reader-facing output: the TRADE BOARD stopped rendering leans on
  2026-08-20, the only remaining output is a settlement sentence, and
  settlement prose appears in 3 of the last 12 published pulses at
  most. No pilot metric read it, and the production parser could read
  only 4 of the 9 lines the editor wrote on 9/17. `pilot_finalize_edit`
  no longer requires it, `pilot_verify_citations` no longer exits 1
  without it and no longer reports a leans count, and the body split on
  `## _LEANS` stays so artifacts written before today still grade their
  prose only. Production keeps its leans machinery untouched; if the
  pilot passes, DRAFT retires and it goes with it.
- **Not built:** the planned code check that every company named in the
  pulse must appear in a cited card. The fabrication it was designed to
  catch was not a fabrication, and the check would have flagged a
  correct sentence. `ai_analysis/prompts.py:981` still tells DRAFT the
  TRADE BOARD reads `_LEANS`, which has been false since 2026-08-20 and
  is worth correcting on the production side separately.

Next: re-run the grader separation gate against the amended prompts,
re-grade 9/17 and 9/18, then two clean days and DAY1. The metric-2
zero-unsupported clause still needs an owner call: even corrected, 9/17
carries one unsupported sentence.

## 2026-09-18 (evening): the re-grade, and a stale-grade defect it exposed

Separation gate re-run against the amended grader prompts: all four
dimensions still separate (bad artifact fails, clean passes), so the
fix did not buy accuracy with leniency. Verdict at
`pilot/grader-gate/2026-09-18T17-14-53Z/`.

Re-graded 9/17 and 9/18 with the new prompts. Today's scheduled 17:00
run was cancelled first: it had checked out 50b03e9f2, nine minutes
before the prompt fix landed, so it was grading 9/18 with the old text
and its output would have been overwritten.

**The prompt fix works, on one agent of two.** On 9/17 agent a now
grades 15 of 15 faithful and its reasoning quotes the new rules
directly: "LO=long-only, WING=Wingstop, DRI=Darden; ticker expansion is
faithful" and "all four figures match exactly" on the wrapped-line
case. Agent b still called the Wingstop sentence unsupported after
searching WING and DRI, which its own `searched` array now records;
the file was in its window, so that is agent sloppiness, and it is
what the second agent and the tiebreak exist to catch. The two agents
also genuinely disagree on the narrative gloss (a: faithful because
rates moved as the dots implied; b: unsupported because no source uses
that framing). The gloss is the sentence editor rule 5 now forbids.

**Defect found: re-grading a date did not clear the previous run's
grade files.** Both 9/17 fidelity tiebreaks still carried prompt_sha
c53af520ab4c while both fresh agents carried 224632dfdf6a. A stale
tiebreak is not just read, it is self-perpetuating: `_apply_tiebreaks`
replaces both agent grades with it, `pilot_tiebreaks_needed` then sees
no disagreement, no new tiebreak runs, and the old prompt's verdict
stands over two fresh agents that contradict it. That is how the 9/17
row kept reporting 80%. The graders workflow now deletes the day's
non-owner grades before writing (owner grades are hand-written and
survive), and the two stale files were removed from pilot-data with
the scoreboard recomputed (db7c255f2).

**Corrected rows, both shakedown:**

| day | m1 frag | m2 shadow | m2 prod | m3 shadow | m3 prod | unread |
|---|---|---|---|---|---|---|
| 09-17 | 4% | disagree 1.00 vs 0.87 | disagree 0.33 vs 0.40 | 1.00 | 0.00 | 0 |
| 09-18 | 4% | disagree 0.73 vs 0.67 | 0.67 | 1.00 | 1.00 | 0 |

Metric 1 passes both days at 4% against the 10% cap with no mis-merges,
the first back-to-back pass of the pilot. Metric 3 preserved on both.

**9/18 is the first day production graded better than the shadow** and
the shadow's four flagged sentences look real, each with its searched
list recorded: two narrative glosses of the kind rule 5 now forbids, a
trade attributed to JPMorgan when the source is The Market Ear (which
tags its JPM-sourced paragraphs separately), and a UBS call on the Bank
of England presented as a call on the Fed. The last two are genuine
misattributions and neither is addressed by anything shipped today.
That result stands as the honest counterweight to 9/17 and should not
be explained away: the shadow editor is better on grouping and
mechanism every day so far, and is not uniformly better on fidelity.

Both shadow pulses were written before the editor.md amendment, so
neither reflects rule 5.

---

## 2026-09-19 — /ask: measure the guard tax, then fix the inputs it was patching

The owner's framing after the 9/18 bot review: "broken habits with
multiple layers of bolted on guards seem not great." So the guards got
measured before anything else was written.

**Measurement.** 187 turns over the 14 days to 9/19, parsed from the
published ask-logs on `pulse-data` (read-only, no prod DB contact).
63% of turns fire at least one guard; 22% pay at least one extra model
call. Median latency 5.3s clean against 8.7s with a rewrite. The money
is a rounding error — roughly seven cents of Gemini for the whole
fortnight — so latency and correctness are the only real costs, and the
earlier framing of this as a spend problem was wrong.

| guard | n | /day | cost |
|---|---|---|---|
| repetition | 14 | 1.0 | model call |
| clapback-fidelity | 13 | 0.9 | model call |
| validate:* -> regen | 12 | 0.9 | model call |
| figure-provenance:* | 72 | 5.1 | free |

**`repetition` was a false positive, not an input problem.** 12 of its
14 firings were list-shaped answers — the top-10 ranking, the econ
calendar, the CPI print rows. Gate 1 flags a content word appearing 3x
in the last 15 tokens; Gate 2 flags a content bigram appearing twice in
the tail. Four rows of "vs X consensus (prior Y)" trip both by
construction. The retry came back 96-99% identical and four turns
shipped the original anyway. `_repetition_runs` now splits an answer at
its list markers and the gates run on the last run carrying text, which
keeps the detector's documented end-of-generation scope. Replayed over
all 187 turns it silences exactly the false positives and fires on
nothing new; the three loops recorded in the detector's own comments
still trip, as does a loop in the final bullet or in trailing prose.
Two intermediate designs were measured and rejected: scanning every run
added 11 false positives by looking mid-answer, and taking the literal
last run made the scan vacuous whenever Gemini closed with a ``` fence.

**The roast habit was an input problem.** All 13 `clapback-fidelity`
firings were reply-to-bot clapbacks and nearly all reached for P&L
caricature. The cause: the WHO'S TALKING dossier carried both scoring
rationales on every call and no trading record at all. `racism_rationale`
is written in trust-and-safety register, `trader_rationale` as a
character sketch ("high-octane options degen who full-ports into deep
OTM index contracts") — injected every call, that became the bot's
default vocabulary for a person, and it is where both the preachy tone
and the repeated cached line came from. Neither is injected now;
`lookup_user_profile` and the leaderboard helpers still serve them when
a question is actually about a score. `db.member_ledger_summary` +
`format_member_ledger_line` put the documented record in instead —
"documented 21d: 17W/3L · avg +136% on closes · traded: QQQ, SOXL".
Net 780 fewer characters per member, measured against the 59-profile
snapshot, and the ticker list is what would have stopped the MSTR
misattribution. `_member_ledger_stats` reads the same summary, so the
record the writer sees and the record the check grades against cannot
drift. `trim_rationale` and its test are deleted: it trimmed the
injected rationale at render time, and the rationale is no longer
injected.

**Two gaps closed in yesterday's ledger guard.** The rewrite acceptance
re-judged only the original subjects' ledgers, so the model's most
likely move — being told a position is not X's and handing it to Y —
passed unseen, because Y had no `_stats` entry and `judge_candidates`
skips an unevidenced name; it now fetches whoever the rewrite newly
named before grading. And a protected asker had the findings dropped
entirely, which published a claim the ledger had just disproved about
the one person the rule protects; the protected path now rewrites
subtractively (drop the claim, add nothing) instead of skipping.

Known limitation, not fixed: `_POSITION_RE` needs a holding verb, so a
possessive attribution ("those MSTR puts are Monsoon's problem") is not
detected.

555 unit tests, 158/158 smokes.

**Tone dial read other members' words (2026-09-20).** DarkMark: "Why
are you always putting all of us down for no reason?" The provoking
answer went to 2Pale, who had replied to someone else's ASCII art with
no text at all and got two invented personal insults. `asker_message`
stripped the replied-to block but not the VERBATIM RECENT MESSAGES block,
which is appended after the "message to you" marker, so Sam's quoted
"You fukn idiot" scored the dial a 2. Across 86 reply/mention turns, 6
ran hotter than the asker's own words earned, and in all 6 the asker
had written nothing. The block is now stripped before scoring.

Open: the model still jabs at dial 0 sometimes (Monsoon's "Shut up" on
9/18 scored 0 and got a jab), and a relayed self-harm mention on 9/19
was answered with mockery. Neither is fixed.

## 2026-09-21 — X keys live; test posts run inside the worker

The four X keys are on the worker and `--check` authenticates as
@omnibetatrades. The first attempt put the OAuth 2.0 Client ID and
Client Secret in the two access slots; the console shows OAuth 1.0 and
OAuth 2.0 keys side by side, and the bot signs OAuth 1.0a. The consumer
key was regenerated after a photo exposed the first one.

`scripts/x_post_test.py --post` built the sheet from a second process,
and `build_calendar_day` writes the market-cap and logo caches to the
live DB, which CLAUDE.md forbids since the 2026-09-04 lock. A test post
is now a flag file (`/data/x-requests/post-calendar`) that the worker
polls each minute, clears before posting, and answers in
`post-calendar.result`. `post_image(force=True)` replaces the script's
global `settings.x_post_enabled = True`, which inside the worker would
have left posting switched on until restart.

First live test post, 2026-09-21: keys and media upload worked, the post
came back HTTP 403, "Posts are limited to a maximum of one cashtag". The
caption carried five. It now emits plain tickers, and a test pins zero
cashtags. Hashtags are unaffected.

X caption redesign (owner, 2026-09-21: "this is X marketing, easy to
read, clear the calendar is for tomorrow"). Heading names the covered
day and says "Tomorrow's" only when the covered date is the next
calendar day, since the Friday post covers Monday and a pre-holiday
post skips the closed day. One line per session with an icon, every
ticker a hashtag (owner pick over one-cashtag and bold-names-only
options; X's guidance is two hashtags per post and it was shown the
tradeoff), 12-hour ET times, emoji counted as two toward 280.
`x_client.enforce_cashtag_limit` strips any second cashtag at post time.

X caption, same day, owner: no hashtags, cashtags on the bold names
only. X's one-cashtag cap means one bold name gets it: the bold earnings
row with the largest market cap, else the first bold conference name;
all other tickers plain, comma-separated. A day with nothing bold has no
cashtag. The all-hashtags version ran for one post (9/22 test).
