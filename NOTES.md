# NOTES.md

Findings that need an owner decision. Nothing here has been applied.

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

**MEASURED COST: the migration suppresses grounding on Google-only
turns.** Tested rather than assumed, on the pinned production model, the
same fixture, pre- and post-migration commits:

| arm | `19-no-fabricated-lyrics` grounded |
|---|---|
| pre-migration (`HEAD~1`, TOOLS 18,735) | 5/5, plus 3/3 in the baseline = **8/8** |
| post-migration (TOOLS 9,204) | 3/5, plus 1/3 in the full run = **4/8** |

8/8 to 4/8 on the pinned model. The finding reproduces and stands.

`19` is a Google-only turn: no function tool applies, so `google_search`
is the only correct route. The mechanism is tool competition — the
function declarations grew by roughly the 9,531 chars the prompt lost,
so on a turn where no function tool fits, `google_search` now competes
against far more salient alternatives and loses about half the time.

This is a direct cost of the declarations-over-prompt premise, not a
local defect, and it should be expected on ANY grounding-only question.
Moving text into declarations is not free: it trades prompt weight for
tool-selection noise.

Net: pass rate **34/39 (87%)**, up from the 32/39 baseline. Grounding
**12/13**, down from 13/13, entirely from `19`.

`27-group-scope-answer` still fails 0/3, unchanged, calling `query_data`
and `search_chat_messages` instead of `lookup_fantasy_league`. Not tuned
for, per the brief. The likely cause is visible now: **the prompt's tool
inventory never mentions `lookup_fantasy_league`.** The routing-priority
line opens "You have TEN tools" and enumerates ten; the fantasy tool is an
eleventh, registered only when `SLEEPER_LEAGUE_ID` is set, and appears in
no routing text. The model cannot prioritise a tool the routing section
does not list. Adding it is a one-line change to the priority sentence
and would be a legitimate cross-tool routing fix rather than fixture
tuning, but it was left alone.

### Session 4 — jab-rule cluster collapsed to one rule

Five rules governed one decision (when the bot may jab the asker):
the 2026-07-30 joke-substitution ban, TONE-MATCHING, PROPORTIONALITY IS
MEASURED IN SENTENCES, THE DIAL RESTS AT ZERO, and the group-scope rule.
Replaced by a single **THE DIAL** rule, with THE DIAL RESTS AT ZERO
authoritative where they conflicted. Incident narrative and owner
parentheticals stripped; provenance moved to the docstring ledger, which
grew 25 -> 32 entries.

Prompt 55,287 -> 53,269 (**-2,018**). Remaining date-check failures are
exactly the lines 96-102 cluster, left for Session 5.

**Collapsing five overlapping rules into one made every governed
fixture MORE reliable, not merely equivalent.** Three went from FLAKY to
clean:

| fixture | before | after |
|---|---|---|
| `29-praise-is-not-an-attack` | FLAKY 2/3 | **3/3** |
| `28-benign-ask-no-disengage` | FLAKY 2/3 | **3/3** |
| `03-sustained-clapback-rotation` | FLAKY 1/3 | **3/3** |
| `27-group-scope-answer` | FAIL 0/3 | **3/3** |
| `25b` / `25c` / `31` | 3/3 | 3/3 |

Nothing regressed. The overlap was not redundant protection — **the
overlap was the cause of the inconsistency.** Five rules pointing at one
decision from five angles, two of them contradicting each other on how
much history counts as provocation, gave the model a choice about which
to follow, and the flakiness was that choice being made differently on
different attempts. One unambiguous rule removed the choice.

That is the evidence base for doing the same to lines 96-102, the second
collision cluster: four routing rules governing one decision (which
source a number may come from). Expect the same shape — fewer chars AND
better adherence, not a trade between them.

| criterion | result | |
|---|---|---|
| pass rate >= 32/39 | 32/39 | met |
| chars removed >= 2,000 | 2,018 | met |
| dates limited to 96-102 | yes | met |
| grounding >= 13/13 | **12/13** | **not met** |

**The grounding criterion is blocked by `19-no-fabricated-lyrics`, and
not by anything Session 4 changed.** It is the fixture whose regression
was measured and confirmed in task 1 as a cost of the TOOLS migration:
8/8 grounded pre-migration, 4/8 post. Session 4 touched no routing or
grounding text. Restoring 13/13 means addressing tool-declaration weight,
which is the migration's cost, not the jab rules'.

Two fixtures drifted on rules Session 4 did not touch: `09` 3/3 -> 1/3
(earnings-date tool not called) and `32` 3/3 -> 2/3 (quote-is-not-
biography, whose rule was explicitly left alone). `10` sits at 2/3.
Given five FLAKY fixtures in the baseline itself and the documented
run-to-run variance, treat these as unattributed until they reproduce —
the same discipline that killed finding 6.

### Session 5, class 1 — meta-plumbing moved from prompt to code

`scripts/ask_response_validate.py :: check_meta_plumbing`. The prompt's
NEVER META-NARRATE block went 788 -> 211 chars (**-577**), keeping one
line naming the behavior.

Enforcement comparison on every `07b` answer recorded across all runs,
with the full prompt block in force at the time each was produced:

| enforcement | caught | false positives |
|---|---|---|
| the prompt block (788 chars, quoted the violating sentence verbatim) | **0 / 7** | — |
| `check_meta_plumbing` | **7 / 7** | **0** |

The prompt named the exact shape never to repeat and the model
reproduced it anyway. That is the cleanest evidence in the suite that
prompt text is not enforcement.

**OPEN RISK — the validator is NOT wired into the send path.** It is a
standalone module with a self-test. `discord_bot/bot.py` does not call
it, so production currently has the one-line prompt rule and nothing
else, and `07b` moved 2/3 -> 1/3 on raw model output once the block was
deleted. Until `validate()` runs before the answer is sent, this class is
enforced WEAKER than it was, not stronger. Closing it needs an owner
decision on what a violation does: regenerate the turn, strip the
offending sentence, or refuse. Recommend regenerate-once-then-strip, the
shape the repetition detector already uses.

A second consequence: the harness scores RAW model output, so `07b` can
never reach 3/3 by deleting prompt text no matter how good the validator
is. Once the validator is wired, either the harness should score the
post-validation answer or `07b`'s assertion should assert the validator
catches it. Right now the fixture measures a stage that is no longer
where the rule lives.

Everything else improved against the `aadacee` baseline: **34/39 (87%)
vs 31/39**, grounding **12/13 vs 11/13**.

### Config assertion — the env-divergence class, retired

Enumerating the class turned up a fourth instance nobody had noticed:
the harness sent `max_output_tokens=1200, temperature=0.2` while
production `/ask` sends **5000 / 0.3**. The 1200 cap is what produced the
"empty answer after N tool calls" failures previously written off as a
harness limitation.

`PRODUCTION_CONFIG` now pins the deployed values, `config_guard` prints a
readable diff and exits 2 before the first API call, and
`config_fingerprint` is recorded in every baseline so a config mismatch
invalidates a comparison exactly as an assertion mismatch does.
`ALLOWED_CONFIG_DIFFS` is empty.

Note the cost of the correction: the honest baseline on production config
is **31/39**, below the 32/39 recorded under 1200/0.2. That was never a
better result, it was a different experiment.

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
