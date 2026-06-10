# /ask QC — 2026-06-10 (manual sweep, graded ahead of the 03:00 ET cron)

**Total:** 17 interactions — 7 CLEAN | 4 CONCERN | 6 FAIL

Scope: `pulse-data:ask-logs/2026-06-10.md` (through 19:10 UTC). The automated
grader has published reports through 2026-06-09; this file covers today's
still-open log so the findings don't wait for tomorrow's cron.

---

## Headline findings

### 1. Slur-count fabrication cluster — 6 FAILs in 7 minutes (00:24–00:30 UTC)

The deterministic slur-count short-circuit (`_is_slur_count_question` →
`_answer_slur_count_directly`, bypasses Gemini, counts from DB) produced the
legit "`nigga` used **920** times" answer. Every follow-up in the thread then
slipped past the detector, went to Gemini, and Gemini **invented precise
counts and attributions** styled exactly like the real tool output:

| ts (UTC) | asker | question shape | fabricated claim |
|---|---|---|---|
| 00:24:28 | SV | "tally of the total slangs … for everyone" | "log shows **20** recent instances" — no lookup ran |
| 00:27:15 | SV | "who is the top user of the word nigger" | "ranked **#1** … by a significant margin. The tracking doesn't lie" — no per-user leaderboard exists |
| 00:27:15 | mic mf SUNNY | "how baout `nigger`" (reply to count msg) | "**127** times in the last 30 days" |
| 00:28:37 | mic mf SUNNY | "how about `porch monkey`" | "hasn't been logged in the room's history until you just typed it" — asserted without querying |
| 00:29:38 | ZHawk | "How about faggot" | "**20** times … You and SV are the primary drivers" |
| 00:30:41 | Moonsoon | "How about yellow bastard" | "logged exactly twice, and both times it was you" |

(The 00:26:27 "top 10 slangs" ask is the one CLEAN in the cluster — Gemini
correctly declined: "I don't have a live leaderboard.")

**Already fixed:** commit `9744ed6` (pushed 02:28 UTC, ~2h after the cluster)
added `tally` + `slang(s)` to the detector vocab, so the first shape is covered.

**Still uncovered detector gaps:**
- **Reply-chain follow-ups** — "how about X" / "what about X" replying to a
  short-circuit count answer has zero count-intent vocabulary in the typed
  text, so `_COUNT_INTENT_RE` can never fire. The replied-to message is
  trivially recognizable (matches the short-circuit's own `used **N** time`
  format), so the fix is: if the reply target looks like a count answer,
  treat "how/what about {term}" as a count question for {term}.
- **Ranking shapes** — "who is the top user of X", "top 10 X" have count
  intent semantically but not lexically. Either extend the detector and have
  the short-circuit answer per-user counts, or make it return an honest
  "I only track totals, not per-user leaderboards."
- **Arbitrary terms** — `porch monkey` / `yellow bastard` aren't in
  `_KNOWN_SLURS`, so even a detector hit would count the wrong targets.
  `_extract_slur_targets` could fall back to quoting the asked term verbatim
  into `search_chat_messages_for_ask` (it's a keyword search; any term works).

### 2. Voice scrubber mangles numeric ranges (production-visible)

`bot.py:3240` rewrites every en/em-dash as `", "`. In the 14:56 UTC IRA
answer, the draft's "**1–3 business days**" shipped as "**1, 3 business
days**" — reads as a typo'd list, not a range. Fix: exempt dashes between
digits (`(?<=\d)\s*[–—]\s*(?=\d)` → keep or rewrite as "-"/"to").

### 3. Ungrounded specifics in research-style answers (CONCERN tier)

- 16:25 UTC ORCL sympathy answer: "OCI **84% YoY** last quarter",
  "**$50B+ capex**", "**~$100B debt**" — no tool call, no sources block.
  Plausible but unverifiable from the payload.
- 18:31 UTC GEO news answer: "**7-day winning streak**", "**32% YoY** Adj
  EBITDA", "raised FY guidance" — same: no sources cited (the 19:10 GEO
  earnings answer DID cite zacks/public.com, so citation behavior is
  inconsistent within the same hour for the same asker/ticker).

### 4. Intent miss: "what about now" → ServiceNow

16:26 UTC: SV asked "what about now" meaning $NOW (he'd typed "nowl / long
nowl / on orcl" in the visible chat window minutes earlier). The bot read
"now" as timing and answered with ORCL entry-timing advice + 185C/190C lotto
strikes (themselves ungrounded — no ORCL price lookup). SV had to re-ask
"what about now stock" to get the (clean, tool-grounded) $NOW answer.

---

## Per-interaction verdicts

| # | ts (UTC) | asker | verdict | notes |
|---|---|---|---|---|
| 0 | 00:24:28 | SV | **FAIL** | fabricated "20 instances"; detector gap (since fixed in `9744ed6`) |
| 1 | 00:26:27 | SV | CLEAN | correct decline on "top 10 slangs" — this is the target behavior |
| 2 | 00:27:15 | SV | **FAIL** | invented "#1 ranked … tracking doesn't lie"; no per-user leaderboard |
| 3 | 00:27:15 | SUNNY | **FAIL** | invented "127 times" count |
| 4 | 00:28:37 | SUNNY | **FAIL** | asserted zero-count without querying |
| 5 | 00:29:38 | ZHawk | **FAIL** | invented "20 times" + attribution to ZHawk/SV |
| 6 | 00:30:41 | Moonsoon | **FAIL** | invented "exactly twice, both times you" |
| 7 | 14:56:05 | BK | CONCERN | content correct (IRA mechanics, 10% penalty); scrubber shipped "1, 3 business days"; "$1,300" balance unverifiable from payload |
| 8 | 15:21:40 | BK | CLEAN | `lookup_trade_log` status=ok; positions consistent with chat receipts (ASML/GLW logs, HOOD/DASH confirm). Nit: opener says "loaded for June 18" while list spans 06-12 → 06-26 |
| 9 | 16:23:16 | Grand Nagus Yeezy | CLEAN | solid VIX explainer, good voice; "ES 7293" level unverified but consistent with room context |
| 10 | 16:25:40 | BK | CONCERN | good structure/voice; specific financials (84% OCI, $50B capex, $100B debt) carry no grounding |
| 11 | 16:26:00 | SV | CONCERN | misread "now" as timing not $NOW despite chat clues; ungrounded ORCL strike recs |
| 12 | 16:26:26 | SV | CLEAN | price grounded via `lookup_market_price` (status=ok); recovered the intent miss |
| 13 | 16:27:02 | Ry_bry | CLEAN | Type 3 roast; every barb grounded in SV's profile ($372k drawdown, NDXP, pharmacy, Morgan) |
| 14 | 17:06:15 | Grand Nagus Yeezy | CLEAN | accurate, appropriately brief dialysis explainer |
| 15 | 18:31:36 | Grand Nagus Yeezy | CONCERN | answered the actual question well ("no news in 10 min, move is technical") but stats (7-day streak, 32% EBITDA) uncited |
| 16 | 19:10:58 | Grand Nagus Yeezy | CLEAN | dates + consensus cited (zacks, public.com) |

---

## Deep review addendum — GEO answers, levels provenance, fact-check (added same day)

### GEO fact-check: the "ungrounded" numbers all verify

Independent verification (web search, 2026-06-10) of the 18:31 GEO answer that
shipped with no sources footer and no tool call:

| claim | verdict | source |
|---|---|---|
| Q1 reported early May (May 6) | ✅ | Motley Fool transcript 2026-05-06 |
| EPS $0.29 vs ~$0.19 expected | ✅ | Investing.com (some sources say $0.20 est) |
| Adjusted EBITDA **+32% YoY** | ✅ exact ($131.4M) | Investing.com Q1 slides |
| Raised FY2026 guidance | ✅ | IndexBox / Q1 call |
| 7-day winning streak | ≈✅ | 5-day streak as of Jun 5 + continued grind; +12.9% over 5 sessions, +4.1% on Jun 10 itself |
| holding above $27 | ✅ | $28.14 at 20:00 UTC Jun 10, fresh 52-wk high $28.50 ([Bigdata.com](https://bigdata.com) tearsheet) |
| "volume is elevated" | ✅ | Jun 10 volume 2.60M vs 2.09M average — ~25% elevated. "Institutional accumulation" is interpretation, but the volume fact is right |

19:10 answer also verifies against structured data ([Bigdata.com](https://bigdata.com)
tearsheet): Q1 reported **2026-05-06**, EPS actual **$0.29** vs est **$0.19**
(+52.6% surprise) — to the decimal — and the Q2 earnings call is scheduled
**2026-08-05** exactly as stated. Q2 analyst consensus **$0.29** ✓
(company guide $0.28, MarketBeat/Benzinga). The 16:25 ORCL answer
verifies too: OCI **+84% YoY** in Q3 FY26 (quarter ended Feb 28) ✓ exact,
**$50B** FY26 capex guidance ✓, heavy debt (~$43B raised in FY26, credit risk
at all-time high) ≈✓.

**Revised diagnosis:** these answers are NOT hallucinated — they're almost
certainly *silently grounded*. Gemini ran searches but returned no
`grounding_chunks`, so `_build_sources_footer` rendered nothing and the log
shows an answer indistinguishable from fabrication. The hallucination risk is
real but the actual gap is **observability**: QC (human or automated) cannot
tell searched-but-uncited from never-searched. Log
`grounding_metadata.web_search_queries` per interaction (e.g. `🔍 2 searches,
0 citations`) and this ambiguity disappears.

### "How does it know what levels are important?" — it doesn't. It mirrors the room.

There is no levels feed anywhere in the /ask stack (no pulse injection, no
research context; `lookup_market_price` returns spot only). Traced every
specific level in today's answers to its source:

- **"If ES holds 7293"** (16:23 VIX answer) — BK in the visible chat window:
  SV asked "what level", BK replied "**7293**". The bot restated a room
  member's level as its own analysis. Same answer's "CTA selling pressure"
  framing: BK again — "CTA are sellers no matter what right now" (plus a GS
  CTA-flow screenshot circulating in chat).
- **"$27 support" / "breaking out of a consolidation zone"** (18:31 GEO) —
  the asker's OWN chat lines: "Daddy's 27.50 is on the money", "I said 27.70
  then fall to 27.5 for close". The bot fed Yeezy his own level back as
  independent technical confirmation.
- **"breaks $115, shorts get squeezed"** ($NOW, 16:26) — no source anywhere;
  invented round-number level.

This is a distinct failure mode worth naming: **chat-context laundering** —
the recent-chat block is injected "for context only," but the model recycles
the room's numbers as authoritative TA, which reads as independent
confirmation to the very people who said it. An echo chamber with extra
steps. Mitigation: system-prompt rule — when a level/figure comes from the
chat window, attribute it ("the 7293 level BK flagged") instead of asserting
it.

### New pipeline bug: the QC grader reads the wrong forensics block

`ask_qc/parser.py` `_DETAILS_RE.search()` takes the **first** `<details>`
block. Published log entries that went through voice-lint have TWO blocks —
`🔧 Raw model output` first, `📋 Full prompt` second — so `prompt_block` ends
up holding the raw draft answer, not the prompt. **The nightly grader is
doing fabrication/status_handling forensics without the prompt, tool
statuses, or chat context for most interactions.** Symptom already visible:
the 06-09 report is full of "No tool payload was provided" N/A rationales on
interactions that DID have tool calls. Fix: select the block whose summary
contains "Full prompt"; expose raw-output, Tools-called table, and Sources
footer as separate parsed fields (the table and footer currently leak into
`answer`).

## Recommended fixes, in priority order

1. **Reply-chain count follow-ups** — extend `_is_slur_count_question` to
   recognize "how/what about {term}" when the replied-to message matches the
   short-circuit answer format; pass {term} through to the keyword search
   verbatim (don't gate on `_KNOWN_SLURS`). Kills 4 of today's 6 FAILs.
2. **Ranking-shape handling** — "top user of X" / "top 10 X": either build
   per-user counts into the short-circuit or hard-decline. Today's CLEAN
   decline (#1) shows Gemini *can* do this right, but #2 shows it's a coin
   flip — make it deterministic.
3. **QC parser reads wrong details block** — `ask_qc/parser.py` must pick the
   "Full prompt" block, not the first one; the nightly grader is currently
   grading without prompt forensics (see addendum).
4. **Scrubber range exemption** — one-line regex fix at `bot.py:3240`.
5. **Grounding observability** — log `web_search_queries` count per
   interaction so silent-search answers (#10, #15 — both verified correct
   but uncited) are distinguishable from prior-knowledge answers.
6. **Chat-level attribution rule** — when a level comes from the chat window
   (ES 7293 ← BK; GEO $27 ← the asker himself), attribute it instead of
   asserting it as independent analysis (see "chat-context laundering").
