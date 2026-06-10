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

## Recommended fixes, in priority order

1. **Reply-chain count follow-ups** — extend `_is_slur_count_question` to
   recognize "how/what about {term}" when the replied-to message matches the
   short-circuit answer format; pass {term} through to the keyword search
   verbatim (don't gate on `_KNOWN_SLURS`). Kills 4 of today's 6 FAILs.
2. **Ranking-shape handling** — "top user of X" / "top 10 X": either build
   per-user counts into the short-circuit or hard-decline. Today's CLEAN
   decline (#1) shows Gemini *can* do this right, but #2 shows it's a coin
   flip — make it deterministic.
3. **Scrubber range exemption** — one-line regex fix at `bot.py:3240`.
4. **Citation consistency** — research-style answers with hard numbers but no
   tool/source (e.g. #10, #15) should either trigger search grounding or
   hedge. Worth a system-prompt nudge: "specific %/$ claims require a source
   or an 'around' hedge."
