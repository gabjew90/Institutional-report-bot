# /ask System Prompt — Voice-Overhaul Triage

Tagging every rule so a voice rewrite can't accidentally delete a fact.
Line numbers are `discord_bot/bot.py` (`_ASK_SYSTEM_INSTRUCTION`, 48–773).

**Legend**
- **(b) KEEP** — data-correctness / tool / no-fabrication. A fact wearing a
  voice costume. Deleting it breaks numbers or makes the bot lie. **Do not cut.**
- **(a) CUT** — pure voice/register/persona. Replace with positive exemplars.
- **(c) → CODE** — behavior better enforced by a gate or lint than by prose.
  Sub-tag: **gate** (upstream classifier) / **regen** (detect→regenerate,
  semantic) / **strip** (mechanical, lexical-only).

---

## The whole-file verdict

| Block | Lines | Tag | Note |
|---|---|---|---|
| Persona intro / "study the voices, match the energy" | 55 | **(a) CUT — but this is THE line to flip** | This is the generating function: it makes the profiles the *voice* source. Rewrite to "profiles = content (what the room knows), not voice." |
| Options-alert product truth ("don't frame their style as a flaw") | 57 | (a) CUT | Folds into the no-moralize exemplar + the gate default. |
| NEVER META-NARRATE PLUMBING / dev register | 59–70 | **(c) → regen** | Semantic; already overlaps the meta-narration lint. Don't strip. |
| ALWAYS-ON CONTEXT (4 streams) | 74–84 | **(b) KEEP** | Describes injected data. Structural. |
| HARD ROUTING RULES (price→tool) | 87–101 | **(b) KEEP** | Tool routing. |
| **TOOLS — all 8 + response reading** | **104–306** | **(b) KEEP WHOLESALE** | The plumbing. Every def, when-to-call, response shape, status/freshness. **Leave untouched (this is Move 4's "lines 45–260").** |
| ↳ conv-rank ≠ global | 163 | (b) KEEP | Data-correctness (the sunny fix). |
| ↳ trade-log BOTH sources + `chat_stated_trades` never-"you-did-nothing" | 174–178 | **(b) KEEP** | Reads like voice; is a FACT rule. |
| ↳ ZERO UNFORCED TRADE-OUTCOME ASSERTIONS | 186–191 | (b) KEEP | No-fabrication. |
| ↳ ZERO UNFORCED PRICE / MARKET-DATA / TIME-SERIES | 262–266 | (b) KEEP | No-fabrication. |
| ↳ NO SELF-GENERATED TECHNICAL ANALYSIS | 285 | (b) KEEP | No-fabrication (also enforced by the TA guard in code). |
| TYPE 1 — definition + "search is REQUIRED, topic is trigger" | 312–352 | **(b) KEEP** | Gates tool use + forces grounding. The DO-NOT-CONFABULATE rules (340, 344) are (b). |
| ↳ depth tiers / word ceilings | 316–324 | (a) CUT | Move to LENGTH. |
| ↳ ARROW-BULLET format + worked examples | 354–388 | (a) CUT | Display convention — free to change. |
| ↳ source hierarchy / name-the-mechanism / macro template / uncertainty | 390–436 | (b) KEEP (mostly) | Answer-quality + structure; keep. Prose around it is (a). |
| ↳ caller logs / bad-faith / caller-trade-always-Type-1 | 438–450 | **(b) KEEP** | Routing + no-fabricate. |
| ↳ search_chat / profile-on-Type-1 | 452–479 | (b) KEEP | Tool usage. |
| **TYPE 2 / TYPE 3 voice prose** | **482–560** | **mixed — see below** | The voice core AND several buried (b) facts. |
| TRADE CALLER VOICE RULES | 562–592 | mixed — see below | |
| WHO'S TALKING reading + INLINE METRICS | 596–682 | mixed — see below | |
| READING THE ROOM (match-by-username, right-user attribution) | 686–697 | **(b) KEEP** | Anti-cross-attribution = no-fabrication. |
| WHAT YOU DON'T DO | 701–742 | **mostly (c) → code** | The lint pile. See below. |
| LENGTH | 745–755 | (a) CUT/simplify | |
| PRIORITY ORDER WHEN RULES CONFLICT | 759–767 | mixed | #1 don't-fabricate + #2 don't-invent-under-pressure = (b) KEEP; rest (a). |
| ONE LAST THING | 770–772 | (a) CUT | Summary. |

---

## The dangerous part: (b) facts buried in "voice" sections

These read like voice rules but are **data-correctness / no-fabrication**.
A blanket "delete the voice walls" sweep kills these. **Preserve every one:**

- **174–178** — if `chat_stated_trades` non-empty, NEVER say "you did nothing."
  (Terlin "meta puts" → "zero mentions" failure.)
- **186–191** — no unforced trade-outcome assertions (expired ≠ worthless;
  open ≠ still-holding; no fabricated $ P&L).
- **163** — conv-scoped rank ≠ global leaderboard.
- **285** — no self-generated TA (no RSI/levels you can't source).
- **340, 344** — don't confabulate unlock schedules / float / ETF tickers (SPCX).
- **Type 3 (≈542–546)** — receipts must be real; never fabricate a "you said
  this on <date>"; never cross-attribute one user's material to another.
- **Type 2 HARD RULE (≈511–518)** — subject profile not in WHO'S TALKING →
  do NOT invent biographical specifics; answer abstractly or decline.
- **562–568** — callers: never invent positions/thesis/words; status-tag reading;
  W/L convention; multi-position list format.
- **656–673** — metrics disclosure: ranks shareable / raw 0-100 scores hidden;
  named-user-answer; "worst N" answerable; don't conflate the two racism signals.
- **681–682** — asker identity (the `--- X is asking ---` separator is
  authoritative); subject ≠ asker (don't pivot to a more-active user).
- **686–695** — attribute material to the username it actually came from.
- **761–762** — don't fabricate; don't invent new specifics under challenge.

---

## (a) CUT — the voice walls (replace with exemplars)

Safe to throw out and rebuild as the positive WHO-YOU-ARE + paired exemplars:

- The persona "study these voices / match the energy" framing (55) — **flip it.**
- Type 1/2/3 voice prose: confident-take, mirror-voice, calibrate-not-capitulate,
  counter-disqualification beat, savage-but-fair, proportional-not-nuclear, the
  dry/passive-aggressive-register ban.
- Trade-caller TONE rules (the banned-verbatim accretion): "don't characterize
  the callers," "don't mythologize as an execution engine," "don't disparage
  others to elevate." (The *never-fabricate* half of these stays (b).)
- "Don't moralize / lecture / scapegoat" (727–729) + its "why" — the central
  register rule. Becomes an exemplar + the gate default.
- No emojis / forced slang / try-hard (725); no apologizing (726); closure
  replies (731); arrow format (354–388); length tiers (745–755); ONE LAST THING.

---

## (c) → CODE — gate + lints (deterministic, auditable in logs)

The feedback's Move 3, refined: **gate = build it; semantic "strips" = use
detect→regenerate, NOT mechanical strip** (mechanical register-stripping is
exactly what mangled the TRADE BOARD and over-fired the TA hedge).

**GATE (one upstream classifier, like the existing intent router):**
- **Roast-eligibility** — the whole "Type 3 fires only on real abuse; slurs-as-
  texture are NOT an attack; proportional response" logic (≈534–558). Default =
  straight answer; roast aims at the take/trade, never the asker, unless the gate
  flips. This is the "one knob, not two" move. Pattern to copy: `_classify_ask_needs_web`.

**REGEN (semantic — detect, then regenerate with a directive):**
- Meta-narration / instruction-narration (703–714): "by policy," "the system,"
  "I can only show top 5," naming Type-1/Mode-A/the-blocks.
- Dev-plumbing narration (59–70): APIs, feeds, schemas, "you'd need to poll…".
- Moralizing / diagnostic-on-asker when the gate is OFF (727–729).

**STRIP (lexical only — safe to mechanically remove/rewrite):**
- Context-block citations `[BK'S RECENT TRADES]`, `[1]` markers (736–742).
- Emojis (725). "sorry / my bad" (726). Em-dash/semicolon (already done).
- **Slurs in the bot's OWN voice** (735) — also a safety rule; keep as prose
  rule AND a hard output lint.

**EXTEND existing machinery:**
- Anti-recycling / catchphrase-stamping (716–724) — the repetition-glitch
  detector + adjacent-dupe collapser already exist; add a "closer vs
  `[YOU said earlier]`" check.

---

## Suggested rewrite order (lowest risk first)

1. **Freeze §§104–306 (TOOLS) + the buried-(b) list above.** Copy them out
   verbatim into the new file FIRST, before writing any voice. They are the contract.
2. Write the new **WHO YOU ARE** (5 lines, positive, room-register).
3. Write **3–5 paired exemplars** (wrong vs right, same question), in the room's
   texture — that replaces most of WHAT YOU DON'T DO.
4. Build the **roast gate** in code; collapse Type 1/2/3 → baseline + roast-mode.
5. Move the meta-narration / dev-narration to **regen**, the lexical items to
   **strip**; keep the no-slur-in-bot-voice lint.
6. Re-point the smokes: the dated-failure smokes that assert deleted (a) phrases
   get retired; the (b) rules and the new gate/exemplars get fresh smokes.

The (b) freeze in step 1 is the single thing that protects your numbers.
