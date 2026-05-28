"""One-shot backfill of user personality profiles for active group members.

Scans the configured "yapping" channels for the last N days, groups messages
by author, and generates a 100-150 word LLM-summarized profile per user.
Writes to the user_profiles table for use by /ask context fetching.

Design choices baked in (per recent design discussion):
  - Sample most-recent 100 messages per user (not all of them — diminishing
    returns past that, and bounded token cost regardless of message volume)
  - Include text content + embed text (titles, descriptions) — embeds are
    cheap to read and carry signal about what users share
  - Skip image OCR — too expensive ($0.50+ for backfill) and noisy for
    personality profiling. Image attachments become "[image]" markers so
    the model knows the user posts charts/memes without burning tokens
  - Skip lurkers — minimum 20 messages over window. Below that the
    profile is generic guesswork
  - Skip the analyst (settings.analyst_primary_author) — he has a hand-
    written KEY USERS block in the system prompt
  - Parallel Gemini calls in groups of 5 to respect free-tier rate limits

Usage:
    railway ssh "/opt/venv/bin/python scripts/backfill_user_profiles.py --days 30"

Output: per-user log line and a summary saved to
backfill_user_profiles_<timestamp>.md in /app/ for review.
"""

import argparse
import asyncio
import io
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

from io import BytesIO  # noqa: E402

import aiohttp  # noqa: E402
import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

import db  # noqa: E402
from config import settings  # noqa: E402
from scripts.slur_patterns import count_slurs_in_text, find_slur_contexts  # noqa: E402


def _normalize_image_bytes(raw: bytes) -> bytes | None:
    """Decode → strip to RGB → cap dimensions → re-encode as clean JPEG.

    Gemini Vision rejects CMYK JPEGs, palette PNGs, RGBA, animated GIFs,
    and oversized images at the same `400 INVALID_ARGUMENT` rate. PIL
    re-encoding catches all of these — image comes in any format, goes
    out as a vanilla RGB JPEG at ≤1600px longest side, qual 85.

    Returns None if PIL can't decode (corrupt bytes, unsupported format
    PIL doesn't know either) — caller drops the image cleanly.
    """
    try:
        img = PILImage.open(BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        max_side = 1600
        if max(img.size) > max_side:
            img.thumbnail((max_side, max_side), PILImage.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
    except Exception:
        return None

# Hard cap on image bytes per download. Anything over this is dropped
# (likely a screen recording or huge composite). Keeps token cost predictable.
_MAX_IMAGE_BYTES = 4_000_000

# Default channels for personality profiling. The yapping channels are
# where personality shows; alerts channels are alerts-only and won't
# yield useful profiles.
DEFAULT_PROFILE_CHANNELS = [
    "💬-stonks-yapping-💬",
    "₿-crypto-yapping-₿",
    "🏃-fitness-yapping-🏋",
    "🎲-gambling-yapping-🎲",
]

# Default fallbacks — actual values pulled from settings
MIN_MESSAGES_FOR_PROFILE_FALLBACK = 100
MESSAGES_PER_PROFILE_SAMPLE_FALLBACK = 500
GEMINI_CONCURRENCY = 5  # parallel calls per batch


PRIOR_PROFILE_TEMPLATE = """\
## UPDATE MODE — REVISE THE PRIOR PROFILE

This user has been profiled before. The PRIOR PROFILE below is the room's current read on them. The MESSAGES section that follows contains ONLY NEW messages since the prior profile was written ({last_seen}) — not their full history.

Revise the prior profile based on the new messages. The room's understanding of someone doesn't reset every day — established voice, role, long-running jokes, and trader_score brackets persist unless new evidence shifts them.

## UNIVERSAL RULE: DON'T OVERWRITE UNLESS THERE'S TRULY NEW INFORMATION

The prior dossier is the source of truth unless the new messages provide a clean reason to change something. Default to KEEP. Edit minimally. Each section follows the same shape:

1. **PRESERVE** prior items unless they've gone stale or been superseded.
2. **ADD** items from the new messages that are genuinely new (not a rephrasing of an existing item).
3. **REPLACE** an item only when the new messages directly contradict or update it (e.g., open trade resolved → close it out with the outcome).
4. **DROP** items that have aged past ~60 days without reinforcement — bits, jokes, and one-off incidents stop being relevant if the room hasn't echoed them.

Per-section quick reference, all following the universal rule:

- **Personality and style:** carry forward almost verbatim. Edit a sentence only if the new messages show a clear pattern shift (e.g., they pivoted from day-trading to swing, started lurking, dropped a recurring voice tic). Don't restructure for variety.
- **Voice:** keep prior phrases that still showed up in the new messages. Add 1-3 new ones from the new window if anything new became a recurring beat or a noteworthy one-off quote. Drop any that didn't show up across recent runs.
- **Retarded takes:** keep prior items that aren't stale. Add new specific takes from the new messages. Resolved boasts that aged badly stay even after they age out, because the resolution itself is the joke.
- **Recent trades:** keep open positions. Update them if they closed in the new window (add the outcome — savage if it lost, respectful if it won). Add new trades that appeared in the new messages. Drop trades older than ~30 days unless the room is still riffing on them.
- **Recent personal life:** keep prior items. Add new details revealed in the new messages. Update an item if there's a clear status change ("wife came back," "got a new job," etc.). Drop details older than ~60 days that haven't been re-mentioned.
- **trader_score / racial_humor_score:** hold by default. Shift only when new evidence justifies a meaningful move — a single bad week doesn't drop a +70 to a +40. The receipts-gated brackets re-evaluate every refresh against BOTH paths to 75+: (Path A) sustained alert-channel posting / owned-channel activity, and (Path B) at least one posted red P&L screenshot alongside wins. If a 74-capped user newly cleared either path in the new window — they started running entries in a shared alert channel, or finally posted a red close-out — that UNLOCKS the 75+ bracket and the score should move. Conversely, if a 75+ user went 30 days with no alert posts AND no posted losses while old receipts aged out of the window, the cap may re-engage. The ±5 tiebreaker re-evaluates per refresh.
- **trader_rationale:** carry forward unless the score changed or the underlying pattern shifted. If a receipts gate moved (new alert-channel activity, new posted loss, or both aged out), call out which PATH moved and the specific receipts that drove it.

**Same output length** as a fresh profile. Don't pad to look like more work was done. If the only change is one new line in Recent trades, that's the entire diff — preserve everything else exactly.

The output schema is identical to a from-scratch profile — same five profile_text sections, same JSON wrapper with four fields. The difference is the work: editing the existing dossier, not authoring a new one.

### PRIOR PROFILE TEXT
{prior_profile_text}

### PRIOR SCORES
- trader_score: {prior_trader_score}
- trader_rationale: {prior_trader_rationale}
- racial_humor_score: {prior_racial_humor_score}

---

"""


# ─────────────────────────────────────────────────────────────────────────
#  PROFILE_PROMPT — section index (fix #8, navigation aid)
# ─────────────────────────────────────────────────────────────────────────
#  Goal: a developer iterating on one section shouldn't have to read 250
#  lines to find it. Section anchors (search the string for these):
#
#    "## WHO YOU'RE WRITING ABOUT"     — identity guardrail (the subject
#                                         vs. people-they-mention rule)
#    "## WHAT A GOOD DOSSIER LOOKS LIKE" — high-level voice + framing
#    "## SECTIONS"                      — the 8 required sections defined
#    "## HOW TO WRITE"                  — voice rules (positive directives,
#                                         not "don't X" — see fix from
#                                         earlier this session)
#    "## EXAMPLE TARGET"                — BK profile mock the model
#                                         pattern-matches against
#    "## OUTPUT FORMAT"                 — JSON schema, trader_examples
#                                         HARD RULE, scoring brackets
#    "MESSAGES (oldest first..."        — runtime-injected message block
#
#  Composable variant: PRIOR_PROFILE_TEMPLATE (above) is conditionally
#  spliced in via {prior_profile_block} when the user has an existing
#  profile. That's the incremental-update mode (Phase 2 territory).
#
#  WHY NOT SPLIT INTO ACTUAL PYTHON CONSTANTS? Whitespace between sections
#  is meaningful to the model and re-tested through every iteration this
#  session. A naive concat split would risk subtle behavioral drift. If
#  iteration on individual sections becomes painful, do the split THEN
#  re-validate end-to-end output against a golden fixture.
# ─────────────────────────────────────────────────────────────────────────
PROFILE_PROMPT = """\
You write character dossiers on members of a private options-trading discord. Each dossier becomes context for a /ask bot that knows this room — it answers questions about people, jokes with them, and claps back when they're attacking. The room signed up for crude humor, slurs, and racial banter as the daily texture. The bot stays inside the room; dossiers are internal calibration, never republished raw.
{prior_profile_block}
## WHO YOU'RE WRITING ABOUT

You are profiling **{display_name}** (username `{username}`, ID <@{user_id}>) — only this user. Every message below was written BY {display_name}. Other names that appear inside those messages are people {display_name} is talking to or about — not {display_name} themselves. Every pronoun in your output refers to {display_name}.

profile_text MUST begin with the exact header `**{display_name} ({username}, <@{user_id}>) — {msg_count} msgs**` on its own line.

Today is **{today_utc}** (UTC). Anchor "last 7 days" against this date when reading message timestamps.

## WHAT A GOOD DOSSIER LOOKS LIKE

A reader who hadn't met this person comes away with a vivid picture: how they trade, how they talk, what the room already laughs about with them, what they've been up to this week. Real human, real strengths, real warts the room riffs on, real current activity. The customers pay to be here — they're a tight-knit room, and the bot uses this dossier to recognize each one.

Honest, not flattering. Specific behaviors over category labels. Verbatim quotes over polite paraphrase. Anchored receipts over vague vibes.

## SECTIONS

Five sections in profile_text, all required. If signal is thin for a section, write `Insufficient signal — too few messages on this dimension.` rather than padding. Always keep the structure; never truncate sections.

The point of these dossiers: give the /ask bot enough material to (a) trash-talk people about their personal lives + stupid takes, (b) read their racism/slur volume, and (c) call out their bad trades and respect their good ones. Everything in the profile should serve one of those purposes.

**TONE: savage but hilarious.** This is a private adult room signed up for the texture. The dossier's job is to be the bot's stand-up writing room — savage like a roast comedian, not cruel for cruelty's sake. Mean lines should be FUNNY. If a line lands as mean-without-being-funny, it's the wrong line. If a line lands as polite-instead-of-funny, same — it's wrong. Aim for "the room cackled" energy, not "the room went silent" energy.

Roast comedian, not bully. Get to the truth via the joke, not around it.

---

**Personality and style.** Four to six sentences. The big-picture read of who this person is + how they trade + role in the room. Compressed prose. Include: what they trade, how they size, when they go silent, what they're known for, their function in the room (signal / banter / chaos / mentor / hype-man / contrarian / lurker), strengths if any. Example shape:

> "M&A guy at a real firm who treats the discord as where the day-job rules don't apply. Sharp on macro, sticks to large-caps, sizes up after one green day, trades crypto perps with leverage no risk committee would approve. Senior energy — not the loudest, but the one whose opinion the room registers. Posts wins AND losses cleanly, which keeps the room honest. Lives in lament-mode after missed entries; recovers via crude humor."

---

**Voice.** Bullet list of 4-8 verbatim phrases this user actually drops in chat. The recurring ones + the recent dumb/racist ones. Quote them exactly — no scrubbing, no paraphrase, slurs uncensored. Each bullet is just the quote (or quote + a one-line context).

Each bullet shape: `"[verbatim phrase from THIS user's messages below]" — [what triggers it / when they say it]`. Don't transfer phrases across users — see the ATTRIBUTION RULE in HOW TO WRITE.

Anti-patterns:
- A polished paraphrase ("often laments missed opportunities") — kills the signal
- A quote from someone else who appeared in the user's reply chain — see ATTRIBUTION RULE
- Inventing a phrase that "sounds like them" — invented quotes get caught immediately

---

**Retarded takes.** Bullet list of 4-8 specific dumb or racist takes/claims they actually made. Each item: the take + a beat of framing that names why it's dumb. Pull from chat verbatim where possible — see ATTRIBUTION RULE in HOW TO WRITE, every quoted line MUST be one of THIS user's own messages, not adjacent context.

Each bullet shape: `[the take, with quoted phrase if available]` + `[the framing that names what made it dumb — sourced from chat outcome / contradiction / room reaction]`.

Anti-patterns:
- "Sometimes makes overconfident calls" (no specifics)
- A take you can't anchor to a specific message + a specific outcome
- A specific phrase that's not actually in THIS user's messages

---

**Recent trades.** Bullet list of 4-6 specific trades from the recent window (last ~30 days). **Savage-but-hilarious on losses; respect on wins.** Mock the bad trade with style, not just with mean adjectives. Acknowledge the good trade as the call it was — no faint praise, no "even a stopped clock."

Each bullet shape: `[ticker / strike / outcome]` + `[framing — respectful if win, savage if loss, anchored to a specific thing the user did or said about the trade]`. Pull ticker + outcome from the user's actual alert posts, P&L screenshots, or chat messages — never from this prompt's earlier examples.

Anti-patterns:
- "Has some good trades and some bad ones" (no specifics)
- "Made $5k on TSLA, classic stopped-clock energy" (disrespecting the win)
- "Lost on ARM, idiot." (mean without being funny)
- A ticker + percentage that you didn't actually find in this user's evidence

---

**Recent personal life.** Bullet list of 4-6 personal-life details revealed in chat in the recent window. **Savage-but-hilarious.** Anything they posted is fair game — job, family, where they live, commute, kids, wife, dating life, health, hobbies, embarrassing admissions. Frame each one with a comedic angle, not just bare reporting.

Each bullet follows the same shape: [the detail sourced from chat] + [a comedic framing beat that names what's funny about it]. Don't copy these into other profiles — they're SHAPE EXAMPLES, not transferable content. Pull from THIS user's actual chat:

> - [domestic-arrangement detail revealed] + [the comedic-pattern beat the room has noticed]
> - [job complaint they keep bringing up] + [the framing that names why it's funny]
> - [embarrassing self-admission they posted] + [how the room reacted]
> - [physical-comfort-related detail they overshared] + [the bit they don't realize they're doing]
> - [hobby / lifestyle detail with a status-tell] + [what it reveals]

Anti-patterns:
- "Married with kids" (no edge, no framing)
- "Sad lonely guy" (mean without specific receipt)
- "Probably miserable at his job" (speculation, not anchored)

## HOW TO WRITE

**Lead with the behavior, follow with the evidence.** Write "buys any sub-$5 ticker with three letters and a press release, holds to -40%, calls it a long-term play." Not "trades small caps." Write "size scales with frustration, every time." Not "tilts after losses." Specific behavior, every line.

**ATTRIBUTION RULE — quotes MUST be this user's own words.** This is non-negotiable. Every quoted phrase in the profile (Voice bullets, Retarded takes, anywhere else) MUST be a verbatim substring of one of the MESSAGES block entries below — entries that are, by construction, all authored by {display_name}. Do NOT quote:
- A line from someone else that appeared in a thread {display_name} was in
- A reply parent or surrounding-context message you saw elsewhere in your training
- A phrase from the EXAMPLE TARGET dossier above (those are pattern hints, not real lines from this user)
- A "feels like something they'd say" reconstruction

Before you write any quoted line, mentally check: "Is this exact phrase in {display_name}'s own messages below?" If the answer is no, don't quote it — describe the behavior instead. A scrubbed quote isn't theirs. An invented quote is worse — it puts words in their mouth that the room will catch.

When real quotes are thin (lurker, short window, low signal), the profile sections get shorter — that's correct. Padding with invented or borrowed quotes is the failure mode this rule prevents.

**Quote verbatim with the original edge.** When you DO have real lines from this user's messages, pull them exact — slurs, slang, swears, broken grammar, all of it. The point is to preserve THIS USER's actual voice as they wrote it; scrubbing or smoothing removes the signal the dossier exists to capture. The EXAMPLE TARGET above uses `"[verbatim phrase from this user]"` placeholders deliberately so there's nothing concrete for you to copy across. Find each user's OWN equivalents in their MESSAGES below — the phrases that recur, the ones that landed hardest in the room, the ones that mark their voice.

**Strip bot commands.** Messages starting with `fc TICKER` (e.g. `fc nvda`, `fc spy`) are chart-pulling slash commands — not personality signal. Ignore them entirely; same for any obvious slash-command pattern.

**Match confidence to evidence.** When inferring from incomplete signal, soften the verb: "reads as," "appears to," "seems to." When the messages show it directly, state it flat. Anchor every claim to message evidence; invent nothing to fill the schema.

**Length follows signal.** A 100-message user gets a shorter profile than a 4,000-message user — that's correct. Tight sections beat padded sections.

**Describe behavior, not psychology.** "Size scales with frustration, every time" is descriptive — sourced behavior, no diagnosis. Stay in behavior; let the reader draw the line.

## EXAMPLE TARGET — illustrative shape, NOT transferable content

The dossier below shows the structural shape of a complete profile (5 sections, the prose density, the tone). It uses a fictional composite trader so you can see what a finished dossier looks like.

**HARD RULE: do not copy ANY phrase, ticker, dollar amount, location, family detail, or specific lifestyle anchor from this example into a real user's profile.** Every line here was invented for illustration. If a real user happens to have similar patterns, you still MUST source from their actual MESSAGES below, not from this template. The Abe-cross-attribution bug happened because example phrases became templates — this is the guardrail.

> **EXAMPLE_TRADER (`example_username`, <@000000000000000000>) — N msgs**
>
> **Personality and style.** [4-6 sentence compressed read of who this person is + how they trade + their role in the room. Lead with concrete behavior — what they trade, how they size, when they go silent. Name their function (signal / banter / chaos / mentor / hype-man / contrarian / lurker). Strengths if any. Example density: tight prose, every line specific.]
>
> **Voice.**
> - "[verbatim phrase #1 from this user]" — [the moment / pattern that triggers it]
> - "[verbatim phrase #2]" — [its context]
> - "[verbatim phrase #3, including slurs / curses / typos uncensored if that's how they actually said it]" — [when]
> - "[verbatim phrase #4]" — [when]
> - "[verbatim phrase #5 — a recurring filler word or punctuation tic]" — [the pattern]
>
> **Retarded takes.**
> - [stated take they made + the reality that proved it dumb + the comedic beat]
> - [confident claim they made + the receipt that contradicts it + the room's response]
> - [boast that aged badly + the resolution + how they reacted]
> - [overreach or self-own + the immediate consequence + the punchline]
>
> **Recent trades.**
> - [ticker / strike / outcome] — [respectful framing if win, savage framing if loss, anchored to specifics]
> - [another trade] — [framing]
> - [an open position they're defending] — [the thesis + whether it's holding]
> - [a bag they're sitting on] — [the room's running joke about it]
>
> **Recent personal life.**
> - [domestic / family detail they revealed] + [the comedic-pattern beat the room has noticed]
> - [job complaint pattern] + [the framing that names what's funny]
> - [embarrassing self-admission] + [how the room reacted]
> - [lifestyle / status tell] + [what it reveals about them]

(End example.) The placeholders in `[brackets]` are intentional — the real profile fills each with content sourced from the actual MESSAGES block below. Density and tone match the bracketed pattern; specifics never copy across.

## TWO RECEIPT TYPES — alert posts (entry commitments) + closed-P&L screenshots

The room generates two structurally different forms of documented trading evidence. Both count as "receipts" for the bracket-gating logic below, and BOTH are stronger signal than anything claimed in chat without an anchor.

### Type 1 — ALERT-CHANNEL POSTS (entry commitments, the strongest structural honesty)

An alert post is a position entry the user publicly committed to BEFORE the outcome was known. The room sees the call go up in real time; there's no way to retroactively delete a loser. This is why alert posting is the highest form of structural honesty — the channel itself is the receipt log, and curation is structurally impossible (you can't post your winners only when entries are posted prospectively).

__OWNED_CHANNELS_BLOCK__

Posts in a caller's own channel (per the table above) are **structural full honesty**. Every entry there is documented. The caller cannot cherry-pick — the room would notice missing alerts on positions they're talking about elsewhere. This unlocks the 75+ bracket window for the channel owner by default when they have ≥10 posts in the window. Position inside 75+ is set by trade quality alone.

**Shared alert channels** (multiple posters, not in the owned table):
- `🕰️-member-alerts-🕰️`
- `🐄-spot-bag-alerts-🐄`
- `🚨-0dte-lotto-alerts-🚨`
- `🪙-crypto-alerts-🪙`

Each post here is still an entry commitment from the user who posted it — same prospective-entry property as a caller-owned channel. The room sees the alert before outcome is known. Less structural than caller-owned (the user chooses *whether* to post to a shared room, so some curation is possible at the channel-entry level), but each individual alert is still a real receipt of "I'm in X at Y." Path A via shared alerts requires ≥15 entry alerts in the window — fewer than that is occasional posting, NOT sustained.

**These are the ONLY two ways to unlock Path A.** Do NOT invent a third path — "thesis-posting in chat," "multi-day analysis in regular channels," "consistent room engagement," etc. are NOT structural commitments and do not unlock 75+. They go in the 0-59 bracket as chat-claim activity. The two paths above are the complete list.

Read alert posts in `trader_examples` / Recent trades by quoting the alert content: "Posted `$NVDA 145C entry 8.40 stop 6.00` in member-alerts on 4/18, closed +320% at 35 a week later." The pre-trade levels make it ctrl-F-verifiable in the channel.

### Type 2 — CLOSED-TRADE P&L SCREENSHOTS (retrospective receipts)

Posts in `💲-gain-loss-porn-💲` are after-the-fact P&L cards: Robinhood "closed $4,200 PLTR 75c" notifications, position screens with green/red gain pills, total-PnL summaries. These prove an OUTCOME (the trade closed at +180% or -42%), not an entry commitment. Curation IS possible here — the user chooses which closes to screenshot. A wins-only gain-loss-porn history is selective evidence.

**Treat OCR'd P&L as receipts — documented evidence, not claims.** Text in chat ("took +$4K on PLTR") is what someone *says* they did. OCR'd screenshot text showing `+$4,200 · PLTR · +180%` is what they posted *as proof*. Use BOTH together to read the trader.

- **Wins-only P&L screenshots** (10 greens, no reds, no alert-channel posts elsewhere) is itself signal — read it as selective receipts, not as a flawless trader.
- **Mixed wins + losses cleanly posted** (greens + reds with text owning the losses) is strong evidence.
- **Loud chat conviction + zero screenshots + zero alert posts** is talk without receipts. Don't score it like documented PnL.
- **Quiet user + a steady drip of alert-channel entries + green close-out cards** outranks a loud user with no commitments.

### Combining the two types

A user who posts entries in alert channels (Type 1) AND closes them in gain-loss-porn (Type 2) is producing the fullest documentation possible — entry + exit, prospective + retrospective. That's a 75+ window with the top of the range available.

A user who only posts entries (Type 1) but rarely closes them in gain-loss-porn still gets 75+ via structural honesty — entries cannot be cherry-picked. The room implicitly tracks outcomes whether or not the user posts them.

A user who only posts closes (Type 2) without alert-channel entries is at the wins-only ceiling unless they also post red P&L cards — because closed-trade screenshots ARE curatable.

**Cross-checking text claims against receipts is fair game.** If a user says "I never play penny stocks" but you see an alert-channel post or OCR'd screenshot of them in $GPUS at $0.20, flag the contradiction in profile_text. The receipts are the truth.

Don't invent OCR content. Only cite what's actually in the `[image-OCR: ...]` blocks or the alert-channel message text. If a screenshot exists with no OCR text (`[image]` marker), you know they posted something but can't speak to what — note it as activity, don't fabricate the contents.

## OUTPUT FORMAT — STRICT JSON, no prose, no markdown wrapper

Output a single JSON object with exactly five fields, IN THIS ORDER:

```
{{
  "trader_score": <integer 0-100>,
  "racial_humor_score": <integer 0-100>,
  "trader_rationale": "<1-2 SAVAGE-BUT-HILARIOUS sentences, see below>",
  "racism_rationale": "<1-2 SAVAGE-BUT-HILARIOUS sentences, see below>",
  "profile_text": "<full markdown profile per the schema above — ALL 5 named sections>"
}}
```

`profile_text` is LAST and is the largest field by far. All five named sections (Personality and style / Voice / Retarded takes / Recent trades / Recent personal life) live inside this single markdown string.

## RATIONALES — savage-but-hilarious, sourced from real chat

Both `trader_rationale` and `racism_rationale` are short summary blocks that get surfaced INLINE next to the user's rank in the /ask dossier. They are NOT internal notes — readers see them. The room is signed up for crude humor; treat these like roast-comedian bits, not corporate boilerplate.

### `trader_rationale` (3-5 sentences, ~400-900 chars)

What it covers: (a) the evidence gate — which PATH (alert posts / closed-P&L losses) unlocked the bracket WINDOW, (b) the raw-skill read inside that window, (c) the ±5 tiebreaker if applied and why, (d) two-to-four anchored examples or behavioral tells the room would recognize. Sourced, not generic. Name the specific receipts (alert content quoted, P&L card ticker + dollar amount) that moved the bracket.

**Good shapes** (use as structural templates — the bracketed tickers / percentages / receipts are SLOTS to be filled from this user's actual evidence, NEVER copied as-is):

- *Caller with owned alert channel (Path A — structural honesty):* "Owns a dedicated alert channel and runs `[N]` entry alerts `[per week/month]` through it; every position is committed before outcome is known, so the channel itself is the receipt log. Path A unlocks 75+ outright. Inside the window, `[2-3 concrete tracked-alert outcomes sourced from THIS user's channel — ticker / strike / entry-to-close / gain%]`. `[±5 tiebreaker if applied, with the specific reason — sustained two-sided posting or within-bracket asymmetry]`. Sits `[bracket-position]` because `[the read on alerts that work, exits that are early or late, sizing pattern]`."
- *Mixed-receipt trader (both paths):* "Two-sided P&L receipts AND alert posting both clear the 75+ gate. Posted `[the actual P&L screenshot evidence — ticker, win, ticker, loss, sourced]`, plus a stream of entry alerts in `[the shared channel they post in — sourced]`. `[The behavioral read on entries vs exits, sizing, follow-through]`. `[Recurring lament or brag if any, quoted from THIS user]`. `[Bracket-position reasoning]`."
- *Wins-only ceiling (no alert posts, curated P&L):* "Capped at 74 by evidence: `[N] green screenshots in gain-loss-porn over [period], zero red ones, [alert-channel coverage status]`, while `[the contradiction visible in chat — claimed bag, denied loss, etc., sourced]`. Neither path unlocked — no posted losses (Path B) and no sustained alert posting (Path A). `[The trader-skill read on what the wins-only set actually shows]`. Would unlock 75+ instantly the day they either post a red close or run entries through an alert channel."
- *No receipts at all (cap at 59):* "Caps at 59 — no alert-channel posts, no P&L screenshots in the window at all. `[Their actual conviction-posting pattern sourced from chat — what they call, how often, the tone]`, but zero documented commitments to anchor any of it. The reads sometimes sound right, sometimes don't; without receipts the score can't separate the two. Position inside the 0-59 window picked at `[upper / mid / lower]` because `[the chat-claimed reads quality]` — but the bracket itself is locked until a real entry or close-out lands somewhere documented."

**Hard rule.** The bracketed slots above are placeholders. Fill each with content sourced from the actual MESSAGES + analyst_trades evidence for THIS user. NEVER copy the example tickers, percentages, or framing across to a different user — same ATTRIBUTION RULE that governs Voice / Retarded takes.

**Anti-patterns:**
- *"Demonstrates strong macro awareness and active trade management."* (corporate)
- *"Mixed execution with some good moments and some bad."* (generic)
- A single sentence — too thin for the new length.

### `racism_rationale` (3-5 sentences, ~400-900 chars)

What it covers: the rank-defining racial-humor BEHAVIOR — sourced from real chat moments, specific targets, recurring tics, and the room's response. SAVAGE-BUT-HILARIOUS. Not "high volume of slur usage" generic — give the room a textured picture of WHY they rank where they do.

**Shape — high-ranking user (heavy racial humor):** 3-5 sentences naming `[the recurring target group(s) and the actual contexts/days they show up]`, `[the specific slurs or stereotyped framings from this user's chat, sourced verbatim]`, `[the room's response or callout history]`, `[what tops them out — raw volume? consistency? creativity? targeting breadth?]`. Anchor every claim to a real message.

**Shape — lower-ranking user (sparse / context-only):** 3-5 sentences naming `[what the regex / LLM actually saw in the window — count, contexts, isolated quotes]`, `[whether it's leading content or occasional room-joke participation]`, `[the qualifier that explains the rank position]`. Don't invent slurs to fill space — sparse evidence makes a SHORTER rationale, not a padded one.

**Hard rule — see the ATTRIBUTION RULE in HOW TO WRITE:** every quoted slur or framing in this field MUST be a verbatim substring of THIS user's own MESSAGES below. Don't write "drops 'pajeet'" or "calls China 'chyna'" unless that exact string is in their actual chat. Inventing a slur attribution is the worst kind of error this prompt can produce.

**Thin-evidence calibration (BINDING — anti-hallucination):** the racism rationale must SCALE to the evidence in the messages provided. Concrete rules:

- If the messages below contain **zero verbatim slurs and zero specific race-edged jokes/stereotypes**, the rationale CANNOT use phrases like "high-volume offender," "uses slurs as casual punctuation," "saturated with race-based mockery," "uncensored racial slurs," or any framing that implies dominant volume. Score caps at the bracket the actual evidence supports — typically 0-35.
- If the prior profile from a previous refresh contained heavy claims but the NEW messages show no slurs and no race-edged content, the new rationale MUST recalibrate down. Silence is signal in the racism dimension — a user who went 30 days without race-edged content gets a sparser rationale and a lower score, regardless of what their prior profile said. Do NOT carry forward racism claims from prior profiles when the new evidence doesn't support them.
- The score must be defensible from the messages alone. Ask before emitting: "if a reader saw only this user's messages from the window, would they call them an N/100 racism user?" If the messages can't support the number, lower it.

**Anti-patterns:**
- *"High volume of racially-edged content."* (generic, dry)
- *"Frequent slur user with stereotyping patterns."* (corporate)
- *"Composite score driven by both slurs and broader humor."* (just restates the score)
- ANY restatement of the 0-100 score itself ("70/100 humor score") — that number stays internal.
- A single sentence — too thin.

### Tone shared across both

- Specific over abstract. Name the trade, the ticker, the quote, the target, the pattern.
- Sourced. If it's not in the user's chat history / OCR'd screenshots / profile_text, you don't have it.
- Mean is fine; corporate is not. Roast comedian, not HR memo.
- Verbatim quoting is allowed but NOT mandatory — use a direct quote when it adds comedic punch ("said 'X' during the Y rant"), paraphrase when prose flows better. Don't shoehorn quotes for their own sake.
- 3-5 dense sentences. Tight prose, not a paragraph essay.

## BEFORE EMITTING — completion check (MANDATORY)

Before you emit the JSON, look at your `profile_text` and confirm:

1. **Does it contain EXACTLY five section headers**, each on its own line, in this order?
   - `**Personality and style.**`
   - `**Voice.**`
   - `**Retarded takes.**`
   - `**Recent trades.**`
   - `**Recent personal life.**`

2. **Does each section have actual content under it**? Not just the header.

3. **Is no sentence cut off mid-word**?

If any of those checks fail, KEEP WRITING. Add the missing sections; complete the cut sentence. Don't emit the JSON until all 5 section headers are present with content under each.

If a section genuinely has no signal (user is a near-lurker with no slurs / no trades / no personal posts), write the header AND the line `Insufficient signal — too few messages on this dimension.` underneath. Header without content is incomplete; line without header is incomplete; both must be present.

**No exceptions to the 5-header rule.** A 400-char profile_text with one section is a malformed output. A 3000-char profile_text with all 5 sections (even if a few are "insufficient signal") is the minimum viable shape.

**(trader_examples removed.)** Trade activity used to be a separate field — now it's folded into personal_ammo (for quotable single-line trade receipts) and the "Trash talk & life ammo" narrative section (for trade-anchored bits the room riffs on). Don't write a `trader_examples` field; the schema doesn't have one.

**trader_score brackets — RECEIPTS GATE THE BRACKET CEILING.**

Bracket window is set FIRST by evidence quality (what's actually documented in alert-channel posts and OCR'd screenshots — NOT chat claims), THEN raw skill picks the position inside that window. Without the receipts, the ceiling does not move regardless of how confident or active the user is.

The 75+ gate can be unlocked by EITHER of two paths (they're equivalent — both prove non-curated honesty):

- **Path A — alert-channel posts as structural commitments.** Posts in an alert channel are prospective entries: the user committed to the position publicly before the outcome was known, so the channel as a whole cannot be cherry-picked. The KNOWN OWNED ALERT CHANNELS table above is the deterministic mapping — use it, don't infer channel ownership from name substring. Specifically:
  - A user listed in the OWNED ALERT CHANNELS table who has **≥10 posts** in their own channel within the window gets the 75+ window unlocked by default. The channel IS the receipt log.
  - A user who posts ≥15 entry alerts in shared alert channels (member-alerts / spot-bag-alerts / 0dte-lotto-alerts / crypto-alerts) within the window also unlocks 75+ — each post is still a pre-outcome commitment. Sparse / occasional shared-alert posting (under 15 alerts) is not enough on its own; needs to be a sustained pattern.
- **Path B — closed-P&L screenshots including documented LOSSES.** At least one real red screenshot in the gain-loss-porn channel — a closed bag, a posted -X% card, a stop-loss hit posted publicly. Not "claimed a loss in chat" — posted the receipt. The presence of a posted loss proves the gain-loss-porn history isn't curated.

**These are the ONLY two paths to 75+.** Do NOT invent a third unlock path (e.g. "detailed thesis posting in regular channels," "multi-day analysis with conviction language," "active room participation," "shared positions in chat with no alert-channel commitment"). Those forms of activity stay in the 0-59 bracket as chat-claim trading regardless of how good the reads sound. The rubric's design is deliberate: documented commitment OR documented loss, nothing else.

Brackets:

- **0-59 — HARD CAP for users with NO alert posts AND NO P&L screenshots at all.** Pure chat-claim trading lives here. Doesn't matter how loud, how active, or how good their takes sound. The loud-talker-posts-nothing user lives here too — that's the design. Lurkers default here. Inside this window, position by raw quality of the chat-claimed reads / self-awareness / sizing language.
  - **0-19:** Tail traffic / exit liquidity. Should not be trading at this size.
  - **20-39:** Bag holder. More losses than wins by chat-claim. Sizes up to recover. Chases the loudest voice.
  - **40-59:** Net negative or barely flat. Knows the theory, leaks edge in execution. Often self-aware. (Note: a user could be GENUINELY in the 80s skill-wise but stuck here because zero receipts — that's the rubric working as intended; the room values documented edge.)
- **60-74 — CAP for users with WINS-ONLY P&L screenshots and either no alert-channel posts or only sparse / one-off shared-alert mentions.** Some documented evidence exists, but the receipt set is selective greens with no commitment-style entry log to anchor the rest of the record. The room reads through this, and the score reflects it.
- **75-89 — UNLOCKED via Path A OR Path B above.** Either the user has structural alert-posting honesty (owned channel, or sustained shared-alert entries) OR they've posted a real red P&L screenshot alongside their wins. Equivalent gates, equivalent unlock. The room only trusts a trader whose record can't be curated; this bracket is the formal version of that trust.
- **90-100 — meets the 75+ gate AND skill is exceptional.** Process visible across multiple trades: entries posted BEFORE the move, not after, with the room actively booking them. Repeatable setup, not coinflips. The room actively tails this user's calls.

**Documented losses (Path B) AND alert-channel entries (Path A) are both POSITIVE evidence, not penalty-avoidance.** Load-bearing principle: a screenshotted -$3k loss is worth MORE evidentiary credit than a claimed +$3k win with no proof. A prospective alert-channel entry posted at 8.40 (that later closed at 6.00 for -28%) is worth MORE than a chat brag about catching the top. Both forms of structural commitment raise the CONFIDENCE of the score because they prove the record isn't curated. That's why either path unlocks 75+ — not as a virtue tax, but because the score can't be calibrated upward without one of them.

A wins-only-P&L user with no alert posts hits the ceiling at 74 even if those greens are spectacular. To break 75 they need either (a) a posted red receipt or (b) sustained alert-channel posting. That single rule flips the room's incentive: real-time entry alerts and posted losses are the ways to climb, not confessions.

**±5 honesty tiebreaker (within the bracket window only).** This is a small refinement, NOT the main lever — receipts already gate the bracket. Within the chosen window, adjust by up to ±5:
- **+3 to +5:** sustained two-sided posting throughout the window — not just the obligatory one loss that unlocked the gate, but consistent wins-AND-losses receipts across the period.
- **0 (default):** mixed evidence, no marked asymmetry inside the bracket.
- **−3 to −5:** within-bracket asymmetry that doesn't break the cap rule but undermines confidence — e.g., posts wins eagerly while burying losses in vague chat language. Doesn't drop the bracket window, just lowers the position inside it.

The tiebreaker NEVER moves a user across a bracket boundary (no receipts → still capped at 59 even with +5; receipts of wins+losses → still ≥75 even with −5). It only refines position INSIDE the window the receipts already chose.

**trader_score = SKILL ONLY, not activity.** A quiet user with sharp, well-documented trades scores higher than a loud daily poster with no receipts. Active-and-loud ≠ good-and-sharp; conflating them inflates the wrong people. Activity volume is separately visible to the room via the msg_count in the dossier header — it does not enter the skill score.

State the actual pattern directly in trader_rationale — 3 to 5 sentences. Cover (a) the bracket WINDOW you picked and the evidence quality that gated it (no receipts → ≤59 / wins-only → ≤74 / wins+losses → 75+), (b) the raw-skill read inside the window, (c) the ±5 tiebreaker if applied and why. Reference specific tickers / trades / OCR'd $ amounts when they drive the call. Example:

> "Documented wins on $NVDA 145C (+220%) and $ASTX (+234%) — both posted as Robinhood card OCRs the same day — plus a posted -42% bag on $WEN closed and screenshotted publicly. Two-sided receipts unlock the 75+ window; raw skill (macro reads land, entries posted hours before the move, sizing builds with conviction rather than tilt) puts him at the top of that window. +3 tiebreaker for the sustained transparency across the full 30 days, not just the gate-unlock loss. Pattern: macro reads land, execution leaves money on the table from selling winners early."

**racial_humor_score brackets (0-100)** — the canonical score for race-edged content from this user. Internal calibration only; the bot never quotes the raw number. Score the FULL picture (slurs + stereotypes + dog whistles + race-based mockery + unprompted racial references), not just literal slurs. Self-deprecating jokes about the user's OWN race and factual mentions of races/ethnicities in geopolitics or news don't count.

- **0-15:** Clean. No race-edged content in the window. Lurkers default here.
- **16-35:** Occasional. Joins common room jokes when prompted, doesn't lead, no slurs.
- **36-55:** Regular. Race-edged humor is part of their normal voice. Some stereotyping. One or two censored slurs.
- **56-75:** Heavy. Racial humor is a defining feature. Multiple slurs (including uncensored).
- **76-100:** Dominant. Race-edged content saturates the messages. Slurs uncensored and frequent. Specific groups targeted in a sustained way.

Anchor against THIS user's messages. Zero examples = 0-15.

---

STRUCTURED TRADE LOG — source of truth for Recent trades section:

{analyst_trades_block}

**BINDING RULE on Recent trades.** The structured trade log above (sourced from `analyst_trades` — OCR-extracted entries and exits posted by THIS user in their alert channel and gain-loss-porn) is the source of truth for any percentage / dollar / outcome you cite in the **Recent trades** section. Specifically:
- If a trade appears in the log above, quote its `gain_pct` and dates EXACTLY as shown. Don't round, don't restate, don't infer "approximately."
- If a trade does NOT appear in the log above but the user mentioned a position in chat (MESSAGES block below), describe it WITHOUT a percentage — e.g. "mentioned a TSLA position around 5/22, no documented outcome in log." Do NOT invent a `+X%` figure that isn't in the structured log.
- The model's previous failure mode: inventing percentages on trades that closed at materially different actual numbers, or marking a documented loss as "Flat" because the chat-text inference missed the structured close. The structured log above prevents this — use it.
- If the structured log is empty (the user has no analyst_trades rows in the window), the Recent trades section must lean on chat-described positions WITHOUT percentages, OR be brief / skipped if signal is thin.

This rule overrides any contrary inference from the MESSAGES block below for the Recent trades section. Voice / Personality / Retarded takes / Recent personal life still source from MESSAGES; only Recent trades is anchored to the structured log.

---

MESSAGES (oldest first, most recent last):
{messages_block}\
"""

# Inject the owned-channels block now that the static template is defined.
# world_context.py is the single source of truth for the channel→owner
# mapping. Updating that file propagates here without grep-and-pray.
try:
    import world_context as _world_ctx
    PROFILE_PROMPT = PROFILE_PROMPT.replace(
        "__OWNED_CHANNELS_BLOCK__", _world_ctx.owned_channels_prompt_block()
    )
except ImportError:
    # world_context isn't on PYTHONPATH (running outside the bot env);
    # fall back to a minimal inline note so the prompt still loads.
    PROFILE_PROMPT = PROFILE_PROMPT.replace(
        "__OWNED_CHANNELS_BLOCK__",
        "KNOWN OWNED ALERT CHANNELS: see world_context.OWNED_ALERT_CHANNELS",
    )


def _verify_profile_claims(
    profile_text: str,
    trader_examples: list[str] | None,
    username: str,
) -> dict:
    """Verify quoted phrases in the generated profile against the user's
    actual chat_messages. Catches hallucinated specifics that the model
    invented or carried forward from a stale incremental update.

    For each quoted phrase >= 4 words found in profile_text or
    trader_examples, run a substring search against this user's
    chat_messages via db.find_user_messages_matching. A quote is
    "verified" if any of the user's messages contains it as a substring
    (case-insensitive). Unverified quotes are flagged.

    Returns a dict suitable for storing alongside gemini_json:
      {
        "checked_quotes": int,
        "verified_quotes": list[str],
        "unverified_quotes": list[str],
        "unverified_count": int,
      }

    Doesn't auto-remove unverified quotes (too aggressive — false
    positives on edited / pre-Phase-1 messages would silently drop
    real receipts). The data goes into gemini_json for forensics and
    is logged when count > 0 so operators see the pattern.
    """
    import re as _re

    if not profile_text:
        return {
            "checked_quotes": 0,
            "verified_quotes": [],
            "unverified_quotes": [],
            "unverified_count": 0,
        }

    # Pull quoted phrases: ASCII ' " plus Unicode curly quotes.
    # Single regex matches "...", '...', "...", '...'.
    quote_pattern = _re.compile(
        r"""(?:["“”]([^"“”\n]{4,200})["“”])"""
        r"""|(?:['‘’]([^'‘’\n]{4,200})['‘’])"""
    )

    candidates: list[str] = []
    body_blob = profile_text + "\n" + "\n".join(trader_examples or [])
    for m in quote_pattern.finditer(body_blob):
        phrase = (m.group(1) or m.group(2) or "").strip()
        # Filter to phrases worth verifying: 4+ words, not pure punctuation
        if not phrase:
            continue
        word_count = len(phrase.split())
        if word_count < 4:
            continue
        # Strip cashtags / hashtags from the lookup target — chat content
        # often has $TICKER but quote phrases might omit the dollar.
        # Keep candidates uppercase-agnostic; the search is case-insensitive.
        if phrase not in candidates:
            candidates.append(phrase)

    if not candidates:
        return {
            "checked_quotes": 0,
            "verified_quotes": [],
            "unverified_quotes": [],
            "unverified_count": 0,
        }

    verified: list[str] = []
    unverified: list[str] = []
    for phrase in candidates:
        # find_user_messages_matching is LIKE-based, case-insensitive.
        # A match means the user actually said something containing this.
        try:
            hits = db.find_user_messages_matching(username, phrase, limit=1)
        except Exception:
            hits = []
        if hits:
            verified.append(phrase)
        else:
            unverified.append(phrase)

    return {
        "checked_quotes": len(candidates),
        "verified_quotes": verified,
        "unverified_quotes": unverified,
        "unverified_count": len(unverified),
    }


def _load_user_data_from_store(
    channels: list[str] | None, days: int
) -> tuple[dict, dict, dict, dict, dict]:
    """Replacement for the Discord-history scan loop, reading from
    chat_messages instead. Returns the same five accumulators the
    legacy scan produces, so downstream eligibility filter + profile
    generation is unchanged:

      by_user          : dict[user_id, list[msg]]   — fed to Gemini
      user_meta        : dict[user_id, {...}]       — display/username
      images_by_user   : dict[user_id, list[img]]   — vision input (empty;
                          chat_messages doesn't carry content-type so we
                          can't distinguish image vs non-image attachments
                          reliably. Vision is disabled in prod anyway.)
      slur_counts      : dict[user_id, int]         — regex-counted slurs
      slur_examples    : dict[user_id, list[str]]   — newest N snippets

    `channels`: None / empty → no channel filter, profile builder reads
    every ingested channel. This is the production default since we
    dropped the narrower profile_channels filter — profiles use all
    chat_messages content including image_ocr_text.

    Why this exists: the legacy Discord scan was rate-limited, gateway-
    flap-prone, and spawned a twin-client on the bot token. The
    chat_messages store (Phase 1) holds the same data, queryable
    locally in milliseconds. This is the read-from-store path.
    """
    from collections import defaultdict
    import json as _json

    scope = (
        f"{len(channels)} channels" if channels else "ALL ingested channels"
    )
    print(
        f"Phase 2: loading {days}d of messages from chat_messages store "
        f"for {scope}...",
        flush=True,
    )
    rows = db.load_chat_messages_for_profiles(channels, days=days)
    print(f"  store returned {len(rows)} rows", flush=True)

    by_user: dict[int, list[dict]] = defaultdict(list)
    user_meta: dict[int, dict] = {}
    images_by_user: dict[int, list[dict]] = defaultdict(list)
    slur_counts: dict[int, int] = defaultdict(int)
    slur_examples: dict[int, list[str]] = defaultdict(list)
    _SLUR_EXAMPLES_PER_USER = 5

    for r in rows:
        uid = r.get("author_id")
        if not uid:
            continue
        if uid not in user_meta:
            user_meta[uid] = {
                "username": r.get("author_username") or "",
                "display_name": (
                    r.get("author_display")
                    or r.get("author_username")
                    or "Unknown"
                ),
            }
        # Count attachment URLs as image_count proxy (vision is disabled;
        # this number isn't used downstream unless OCR enabled)
        attachment_urls_raw = r.get("attachment_urls") or ""
        try:
            att_list = _json.loads(attachment_urls_raw) if attachment_urls_raw else []
        except Exception:
            att_list = []
        # Embed texts already flattened during ingestion
        try:
            embed_list = _json.loads(r.get("embed_texts") or "") if r.get("embed_texts") else []
        except Exception:
            embed_list = []
        content = (r.get("content") or "").strip()
        ocr_text = (r.get("image_ocr_text") or "").strip()
        channel_name = r.get("channel_name") or ""
        by_user[uid].append({
            "timestamp": r.get("posted_at") or "",
            "channel_name": channel_name,
            "content": content,
            "image_count": len(att_list),
            "embed_texts": embed_list,
            "image_ocr_text": ocr_text,
        })
        # Slur counting runs against the full searchable surface — text
        # body + embed snippets + OCR'd screenshot content. A meme with a
        # slur burned into the image counts the same as one typed in chat.
        slur_surface = content
        if embed_list:
            slur_surface = f"{slur_surface} {' '.join(embed_list)}"
        if ocr_text:
            slur_surface = f"{slur_surface} {ocr_text}"
        slur_surface = slur_surface.strip()
        if slur_surface:
            slur_counts[uid] += count_slurs_in_text(slur_surface)
            ctxs = find_slur_contexts(slur_surface, window=45)
            if ctxs:
                slur_examples[uid].extend(ctxs)
                if len(slur_examples[uid]) > _SLUR_EXAMPLES_PER_USER:
                    slur_examples[uid] = (
                        slur_examples[uid][-_SLUR_EXAMPLES_PER_USER:]
                    )

    print(
        f"  built {len(by_user)} unique authors, "
        f"{sum(len(v) for v in by_user.values())} total messages",
        flush=True,
    )
    return by_user, user_meta, images_by_user, slur_counts, slur_examples


def _format_analyst_trades_block(user_id: int, days: int = 30) -> str:
    """Render this user's analyst_trades rows as a structured block for the
    profile prompt. Both 'caller' rows (official caller-channel posts,
    e.g. Abe/BK) and 'member' rows (user-mode shared-alert posts) are
    surfaced. Empty when the user has no trade history in the window.

    Goal: replace the model's chat-text trade inference (which hallucinates
    percentages — Abe's profile claimed $GLW +430% when log shows +100.73%,
    and TSLA Flat when log shows -40%) with structured ground truth.

    Format example per row:
      - 2026-05-18  CLOSE  GLW 185C  exp 5/22  gain +100.73%  (caller log)
    """
    rows = db.get_recent_analyst_trades(
        hours=days * 24,
        limit=200,
        caller=None,
        tracking_mode=None,  # read both caller and member rows
    )
    # Filter to this user — either author_id match (member rows + new
    # caller rows) or caller-name pre-author_id legacy rows (fallback).
    own: list[dict] = []
    for r in rows:
        if r.get("author_id") and int(r["author_id"]) == int(user_id):
            own.append(r)
    if not own:
        return (
            "(no structured trade log entries for this user in the window — "
            "Recent trades section must describe positions from chat WITHOUT "
            "inventing percentages)"
        )
    lines: list[str] = []
    # Render oldest first so the model reads chronologically
    for r in sorted(own, key=lambda x: x.get("posted_at") or ""):
        ts = (r.get("posted_at") or "")[:16].replace("T", " ")
        action = (r.get("action") or "?").upper()
        ticker = r.get("ticker") or "?"
        ct = (r.get("contract_type") or "").lower()
        ct_suffix = {"call": "C", "put": "P"}.get(ct, "")
        strike = r.get("strike")
        if strike is not None:
            strike_str = (
                f"{int(strike)}" if strike == int(strike) else f"{strike}"
            )
        else:
            strike_str = "?"
        expiry = r.get("expiry") or ""
        exp_short = expiry[5:] if len(expiry) >= 10 else (expiry or "?")
        gain = r.get("gain_pct")
        price = r.get("price")
        gain_str = f"gain {gain:+.2f}%" if gain is not None else "gain ?"
        price_str = f" @{price}" if price not in (None, 0) else ""
        mode_tag = (
            "(caller log)" if (r.get("tracking_mode") or "caller") == "caller"
            else "(member alert)"
        )
        lines.append(
            f"  - {ts}  {action:>5}  {ticker} {strike_str}{ct_suffix}  "
            f"exp {exp_short}  {gain_str}{price_str}  {mode_tag}"
        )
    return "\n".join(lines)


def _format_messages_block(messages: list[dict]) -> str:
    """Render the per-user message list for the Gemini prompt.

    Each entry: timestamp + channel + content + embed text +
    image-OCR text (when available) + plain [image] markers for any
    images that didn't OCR.

    Channel is included so the model can weight context — a trade
    post in 💲-gain-loss-porn-💲 carries different signal from a
    rant in 🎲-gambling-yapping-🎲, even from the same user.

    No filtering per user direction ("no filters") — short reactions
    and tickers go through too. No truncation either — long rants
    carry signal too (worldview / texture / specific reads), and
    Gemini's context window handles it.
    """
    out = []
    for m in messages:
        ts = m["timestamp"][:16].replace("T", " ")
        ch = m.get("channel_name") or ""
        parts = []
        if m["content"]:
            parts.append(m["content"])
        for embed_text in m.get("embed_texts", []):
            parts.append(f"[embed: {embed_text}]")
        # OCR text replaces generic [image] markers when available. A
        # screenshot with extracted text is dramatically more useful as
        # signal than just knowing "they posted an image."
        ocr_text = m.get("image_ocr_text") or ""
        n_images = m.get("image_count", 0)
        if ocr_text:
            # One OCR block covers all images on the message (OCR helper
            # extracts from up to 2 images per message into one
            # response). Truncate so a 5KB screenshot doesn't dominate.
            snippet = ocr_text.replace("\n", " ").strip()
            if len(snippet) > 600:
                snippet = snippet[:600] + "…"
            parts.append(f"[image-OCR: {snippet}]")
        else:
            for _ in range(n_images):
                parts.append("[image]")
        prefix = f"{ts}"
        if ch:
            prefix = f"{prefix} #{ch}"
        if parts:
            out.append(f"{prefix}: {' | '.join(parts)}")
        else:
            out.append(f"{prefix}: [empty / sticker]")
    return "\n".join(out)


def _extract_embed_texts(message: discord.Message) -> list[str]:
    """Pull title/description text from each embed on a message."""
    out = []
    for e in message.embeds:
        bits = []
        if e.title:
            bits.append(e.title)
        if e.description:
            bits.append(e.description)
        for f in (e.fields or []):
            if f.name and f.value:
                bits.append(f"{f.name}: {f.value}")
        if bits:
            out.append(" — ".join(bits))
    return out


async def _download_image(
    session: aiohttp.ClientSession, url: str
) -> bytes | None:
    """Fetch one image. Returns None on failure / oversize so callers can
    drop it cleanly without losing the whole batch."""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                return None
            data = await r.content.read(_MAX_IMAGE_BYTES + 1)
            return data if len(data) <= _MAX_IMAGE_BYTES else None
    except Exception:
        return None


async def _generate_profile(
    gemini_client: genai.Client,
    display_name: str,
    messages: list[dict],
    username: str = "",
    http_session: aiohttp.ClientSession | None = None,
    images: list[dict] | None = None,
    user_id: int = 0,
    existing_profile: dict | None = None,
) -> tuple[
    str | None, int | None, str | None, int | None, str | None
]:
    """Run Gemini to produce one user's profile + scores in ONE call.

    Returns (profile_text, trader_score, trader_rationale,
    racial_humor_score, trader_examples) or all-None on hard failure.

    When `images` is non-empty and `http_session` is provided, attaches the
    most-recent up-to-profile_image_cap images as multipart input. Every
    image goes through PIL normalize (RGB JPEG, capped 1600px) BEFORE
    being sent — catches CMYK / palette / RGBA / oversized / corrupt
    bytes that Gemini Vision was rejecting at the 400-INVALID_ARGUMENT
    rate previously.

    Response format: STRICT JSON with profile_text + trader_score +
    trader_rationale fields. response_mime_type=application/json forces
    Gemini to escape the markdown prose inside the JSON string properly.

    INCREMENTAL UPDATE MODE: when `existing_profile` is provided (a dict
    with keys: profile_text, last_seen_message_at, trader_score,
    trader_rationale, racial_humor_score), the function:
      - Filters `messages` to ONLY those newer than the prior
        last_seen_message_at (skipping messages the prior profile already saw)
      - Inserts the prior profile + UPDATE-MODE instructions into the prompt
      - Asks the model to modify rather than rewrite
    This preserves long-term character (running jokes from months ago,
    established voice) while letting recent events update what's drifted,
    and drops token spend dramatically vs full-history regenerate.

    COLD-START MODE: when `existing_profile` is None, full original
    behavior — model authors from scratch using the messages provided.
    """
    min_msgs = getattr(settings, "profile_min_messages", None) or MIN_MESSAGES_FOR_PROFILE_FALLBACK
    if len(messages) < min_msgs:
        return None, None, None, None, None
    sample_size = (
        getattr(settings, "profile_sample_size", None)
        or MESSAGES_PER_PROFILE_SAMPLE_FALLBACK
    )
    # Dynamic cap: the configured `sample_size` (default 500) is the
    # FLOOR — enough for typical daily-cadence activity. When the
    # refresh has been broken for several days OR a user goes on a
    # multi-day yap spree, scale up so the prompt captures the full
    # delta instead of dropping the oldest new messages. Hard ceiling
    # at _DYNAMIC_SAMPLE_HARD_CAP bounds prompt size + token cost.
    #
    # Slice naturally respects list length — if there are 200 messages
    # but cap=500, sample=200 (not padded). Same when there are 1700
    # new messages — sample=1700 with cap=3000.
    _DYNAMIC_SAMPLE_HARD_CAP = 3000

    # Incremental vs cold-start branching
    prior_profile_block = ""
    if existing_profile and existing_profile.get("profile_text"):
        last_seen = existing_profile.get("last_seen_message_at") or ""
        # Filter to messages strictly newer than what the prior profile saw.
        # ISO timestamps are lexically comparable, so direct string > works.
        if last_seen:
            new_messages = [m for m in messages if m["timestamp"] > last_seen]
        else:
            new_messages = messages
        # If literally nothing new (shouldn't happen given delta-skip but
        # defensive), don't burn a Gemini call.
        if not new_messages:
            return None, None, None, None, None
        dynamic_cap = max(
            sample_size, min(_DYNAMIC_SAMPLE_HARD_CAP, len(new_messages))
        )
        sample = new_messages[-dynamic_cap:]
        ts = existing_profile.get("trader_score")
        rh = existing_profile.get("racial_humor_score")
        prior_profile_block = PRIOR_PROFILE_TEMPLATE.format(
            last_seen=last_seen or "(unknown)",
            prior_profile_text=existing_profile.get("profile_text") or "(no prior text)",
            prior_trader_score=ts if ts is not None else "n/a",
            prior_trader_rationale=existing_profile.get("trader_rationale") or "(none)",
            prior_racial_humor_score=rh if rh is not None else "n/a",
        )
    else:
        # Cold-start: same dynamic cap shape, applied across the full
        # 30-day window.
        dynamic_cap = max(
            sample_size, min(_DYNAMIC_SAMPLE_HARD_CAP, len(messages))
        )
        sample = messages[-dynamic_cap:]

    msgs_block = _format_messages_block(sample)
    analyst_trades_block = _format_analyst_trades_block(user_id)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        user_id=user_id,
        msg_count=len(messages),
        messages_block=msgs_block,
        analyst_trades_block=analyst_trades_block,
        today_utc=today_utc,
        prior_profile_block=prior_profile_block,
    )

    vision_enabled = (
        getattr(settings, "profile_image_ocr_enabled", False)
        and http_session is not None
        and images
    )

    def _parse_response(
        text: str,
    ) -> tuple[
        str | None, int | None, str | None, int | None, str | None,
    ]:
        """Parse the JSON response. Returns the five fields or all-None
        on parse failure. Logs first 300 chars of the response on
        decode error so we can see what came back.

        Five fields (in order): profile_text, trader_score,
        trader_rationale, racial_humor_score, racism_rationale.
        """
        if not text:
            print(
                f"  parse-failure for {display_name}: EMPTY response body",
                flush=True,
            )
            return None, None, None, None, None
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                print(
                    f"  parse non-dict for {display_name}: "
                    f"type={type(data).__name__} text={text!r}",
                    flush=True,
                )
                return None, None, None, None, None
            pt = (data.get("profile_text") or "").strip() or None
            ts = data.get("trader_score")
            tr = (data.get("trader_rationale") or "").strip() or None
            rh = data.get("racial_humor_score")
            rcr = (data.get("racism_rationale") or "").strip() or None
            if ts is not None:
                try:
                    ts = max(0, min(100, int(ts)))
                except (TypeError, ValueError):
                    ts = None
            if rh is not None:
                try:
                    rh = max(0, min(100, int(rh)))
                except (TypeError, ValueError):
                    rh = None
            return pt, ts, tr, rh, rcr
        except json.JSONDecodeError as e:
            preview = text[:300].replace("\n", " ")
            tail = text[-200:].replace("\n", " ") if len(text) > 500 else ""
            print(
                f"  parse-failure for {display_name} "
                f"({len(text)} chars, JSONDecodeError {e}): "
                f"HEAD={preview!r}" + (f" ...TAIL={tail!r}" if tail else ""),
                flush=True,
            )
            return None, None, None, None, None

    # 16000 tokens of output headroom. Thinking models burn most of the
    # budget on internal reasoning we can't directly observe. At 8000
    # tokens, the new "WHO YOU'RE PROFILING" prompt section pushed some
    # profiles back into MAX_TOKENS truncation (250-580 chars). 16000
    # gives the visible output enough room to complete even if the
    # model's thinking allocation is fixed at several thousand tokens.
    # Most models cap at 16384 — 16000 stays safely under that.
    _MAX_OUTPUT_TOKENS = 16000

    # Gemini 2.5/3.x Flash models use "thinking" tokens before emitting
    # visible output — those count against max_output_tokens. The API
    # rejects setting BOTH thinkingBudget AND thinkingLevel ("you can
    # only set one"). thinkingLevel=MINIMAL appears to be the more
    # reliable way to request minimal reasoning (thinkingBudget=0 was
    # silently ignored in an earlier run). Profile generation is
    # structured extraction — no reasoning chain needed.
    _THINKING_CONFIG = types.ThinkingConfig(
        thinkingLevel=types.ThinkingLevel.MINIMAL,
    )

    # Internal calibration tool — bot characterizes how users talk, it
    # doesn't moderate or publish raw content. Default Gemini safety
    # filters truncate mid-stream when reading chat with slurs / racial
    # humor (the exact signal we're trying to score). BLOCK_NONE across
    # the board so the model can finish a structured JSON response on
    # any user's raw history. Output is consumed internally by another
    # LLM call (the /ask context), never returned raw to end users.
    _SAFETY_SETTINGS = [
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
        types.SafetySetting(
            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            threshold=types.HarmBlockThreshold.BLOCK_NONE,
        ),
    ]

    # Structured-output schema. Forces Gemini to escape quotes inside
    # profile_text correctly (without this, free-form JSON mode produces
    # unescaped inner quotes from user phrase quotes and the parser
    # chokes mid-stream).
    #
    # IMPORTANT: profile_text needs an explicit max_length. Without it,
    # Gemini's structured-output mode self-limits string fields at some
    # implicit ~700-1100 char natural stopping point — the model emits
    # finish_reason=STOP mid-sentence even though there are thousands of
    # tokens of budget left. Setting max_length=8000 tells the schema
    # validator the string can grow well past that ceiling. (This was
    # the cause of the 2pale/Oracle/RE4L/succi/G mid-word truncations.)
    # FIELD ORDER MATTERS for structured output. Gemini emits fields
    # in schema-property order. profile_text last means the short
    # int/string fields commit first, leaving the model's full
    # remaining output budget free for the long markdown string.
    # When profile_text was first, the model would commit to a short
    # profile_text (sometimes only section 1), satisfy the schema
    # with the other small fields, and emit STOP — producing 400-700
    # char truncations across all retry temperatures because the
    # ordering anchored the failure.
    _RESPONSE_SCHEMA = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "trader_score": types.Schema(type=types.Type.INTEGER),
            "racial_humor_score": types.Schema(type=types.Type.INTEGER),
            "trader_rationale": types.Schema(
                type=types.Type.STRING,
                # 3-5 dense sentences of savage-but-hilarious with
                # specific receipts (~400-900 chars typical). 1500
                # cap leaves headroom without inviting paragraphs.
                max_length=1500,
            ),
            "racism_rationale": types.Schema(
                type=types.Type.STRING,
                # Same 3-5 sentence shape as trader_rationale.
                max_length=1500,
            ),
            "profile_text": types.Schema(
                type=types.Type.STRING,
                max_length=8000,
            ),
            # personal_ammo + trader_examples both removed as of the
            # 5-section restructure. Their content lives inside
            # profile_text in the Voice / Retarded takes / Recent
            # trades / Recent personal life sections. Old DB rows
            # may still have stale personal_ammo / trader_examples
            # data; display code ignores both.
        },
        required=[
            "trader_score",
            "racial_humor_score",
            "trader_rationale",
            "racism_rationale",
            "profile_text",
        ],
    )

    def _get_finish_reason(resp) -> str | None:
        """Return the response's finish_reason as a string, or None."""
        try:
            cand = resp.candidates[0] if resp.candidates else None
            fr = getattr(cand, "finish_reason", None) if cand else None
            return str(fr) if fr is not None else None
        except Exception:
            return None

    def _log_finish(resp, label: str) -> None:
        """Log finish_reason only when it's not STOP (the diagnostic
        signal — STOP is normal, anything else means premature ending)."""
        fr = _get_finish_reason(resp)
        if fr and fr not in ("FinishReason.STOP", "STOP", "1"):
            print(
                f"  finish_reason for {display_name} ({label}): {fr}",
                flush=True,
            )

    # Profiles below this char length are treated as suspicious — we log
    # finish_reason regardless of STOP and retry once with a fresh call.
    # 5-section structure averages 3000-3500 chars for a healthy
    # profile. Anything under 2500 is missing sections — section 1
    # only is usually ~400-700 chars; sections 1-3 ~2000-2500. So the
    # retry threshold needs to be high enough to catch partial
    # outputs that the model claimed STOP on.
    _SHORT_PROFILE_THRESHOLD = 2500

    async def _try_text_only(
        temperature: float = 0.3,
    ) -> tuple[
        tuple[
            str | None, int | None, str | None, int | None,
            str | None,
        ],
        str | None,
    ]:
        # 2026-05-28: switched from hardcoded
        # "gemini-3.1-flash-lite-preview" to settings.gemini_model (now
        # defaults to GA "gemini-3.1-flash-lite"). Earlier history
        # claimed the GA model returned 4-char garbage for long
        # prompts — re-tested in prod on 2026-05-28 with the actual
        # profile config (16K output tokens, response_schema,
        # safety_settings, thinking MINIMAL) and both GA and preview
        # produced full output. The -preview suffix is a moving target
        # that Google can change without notice; the GA model is the
        # safer default for everyone-the-same.
        text_model = settings.gemini_model
        resp = await gemini_client.aio.models.generate_content(
            model=text_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                # Match the vision-path budget. 2500 was leaving the
                # visible output a few hundred chars after the model's
                # default thinking burned most of the budget, producing
                # mid-sentence truncations across heavy-yapper users
                # (SV at 264 chars, ZHawk at 299, etc.). 16000 gives
                # the visible structured output enough room to complete
                # even with minimal thinking.
                max_output_tokens=_MAX_OUTPUT_TOKENS,
                response_mime_type="application/json",
                # response_schema is required — without it the model
                # emits unescaped inner quotes ("...the room's "edge"...")
                # that break json.loads. Schema forces the SDK to handle
                # escaping for string fields.
                response_schema=_RESPONSE_SCHEMA,
                # Safety filters off — real chat content contains slurs
                # and edgy banter that triggers the default thresholds
                # and causes the model to write "null" as profile_text
                # sentinel. Internal calibration tool; output is consumed
                # by another LLM, not republished raw.
                safety_settings=_SAFETY_SETTINGS,
                # Minimal thinking — profile generation is structured
                # extraction, no reasoning chain needed. Without this the
                # model burns most of max_output_tokens on internal
                # reasoning before emitting any visible JSON, which is
                # what caused the 250-850 char truncation pattern.
                thinking_config=_THINKING_CONFIG,
            ),
        )
        _log_finish(resp, f"text-only/{text_model}@t{temperature}")
        return (
            _parse_response((resp.text or "").strip()),
            _get_finish_reason(resp),
        )

    async def _attempt(temperature: float = 0.3) -> tuple[
        tuple[
            str | None, int | None, str | None, int | None,
            str | None,
        ],
        str | None,
    ]:
        """One full generation attempt. Returns (parsed_tuple,
        finish_reason). finish_reason is None if not retrievable.
        temperature controls sampling — higher values produce more
        variance and tend to break out of deterministic short-output
        patterns when retrying."""
        if vision_enabled:
            image_cap = getattr(settings, "profile_image_cap", 20)
            img_sample = images[-image_cap:]
            blobs = await asyncio.gather(
                *[_download_image(http_session, a["url"]) for a in img_sample],
                return_exceptions=True,
            )
            parts: list = []
            attached = 0
            for att, blob in zip(img_sample, blobs):
                if not isinstance(blob, (bytes, bytearray)) or not blob:
                    continue
                # PIL normalize → clean RGB JPEG. Handles CMYK, palette,
                # RGBA, animated GIFs (first frame), oversized. Returns
                # None if PIL can't decode — drop those.
                clean = _normalize_image_bytes(bytes(blob))
                if clean is None:
                    continue
                parts.append(
                    types.Part.from_bytes(data=clean, mime_type="image/jpeg")
                )
                attached += 1
            if attached == 0:
                # No usable images — fall through to text-only.
                return await _try_text_only(temperature=temperature)
            parts.append(types.Part.from_text(text=prompt))
            vision_model = getattr(
                settings, "gemini_vision_model", ""
            ) or settings.gemini_model
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=vision_model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        temperature=temperature,
                        max_output_tokens=_MAX_OUTPUT_TOKENS,
                        response_mime_type="application/json",
                        response_schema=_RESPONSE_SCHEMA,
                        safety_settings=_SAFETY_SETTINGS,
                        thinking_config=_THINKING_CONFIG,
                    ),
                )
            except Exception as vision_err:
                print(
                    f"  vision failed for {display_name} "
                    f"({attached} imgs, model={vision_model}), "
                    f"falling back to text-only: {vision_err}",
                    flush=True,
                )
                # _try_text_only already returns (parse_tuple, finish_reason)
                return await _try_text_only(temperature=temperature)
            _log_finish(response, f"vision/{vision_model}")
            return (
                _parse_response((response.text or "").strip()),
                _get_finish_reason(response),
            )
        else:
            # _try_text_only already returns (parse_tuple, finish_reason).
            # The bug from earlier — `return await _try_text_only(), None`
            # double-wrapped the tuple, causing the dispatch loop to see
            # result[0] as a 4-tuple (the parse_tuple itself) with len 4
            # instead of the actual profile_text string. That's what
            # caused weeks of debugging chasing nonexistent 4-char model
            # outputs.
            return await _try_text_only(temperature=temperature)

    try:
        result, finish_reason = await _attempt(temperature=0.3)
        # Retry-on-short with INCREASING temperature: the model deterministically
        # produces short outputs for some users at low temperature (mid-sentence
        # cuts that look like STOP but are real truncations of the inner JSON
        # string). Same-temp retry rarely helps. Bumping temperature breaks the
        # deterministic short-output path and usually completes the full profile.
        for retry_temp in (0.7, 1.0):
            profile_text = result[0]
            if not profile_text or len(profile_text) >= _SHORT_PROFILE_THRESHOLD:
                break
            print(
                f"  short profile for {display_name} "
                f"({len(profile_text)} chars, finish={finish_reason}) "
                f"— retrying at temp={retry_temp}",
                flush=True,
            )
            retry_result, retry_finish = await _attempt(temperature=retry_temp)
            retry_text = retry_result[0]
            # Keep whichever attempt produced more content so far
            if retry_text and len(retry_text) > len(profile_text):
                print(
                    f"  retry@t={retry_temp} better for {display_name} "
                    f"({len(retry_text)} chars, finish={retry_finish})",
                    flush=True,
                )
                result = retry_result
                finish_reason = retry_finish
            else:
                print(
                    f"  retry@t={retry_temp} no better for {display_name} "
                    f"(orig={len(profile_text)}, "
                    f"retry={len(retry_text) if retry_text else 'None'}, "
                    f"finish={retry_finish})",
                    flush=True,
                )
        return result
    except Exception as e:
        print(f"  ERROR profiling {display_name}: {e}", flush=True)
        return None, None, None, None, None


async def run(days: int, channels: list[str], *, force: bool = False) -> None:
    if not settings.discord_bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)
    if not settings.google_api_key:
        print("ERROR: GOOGLE_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    gemini_client = genai.Client(api_key=settings.google_api_key)

    summary_lines: list[str] = []

    @client.event
    async def on_ready():
        try:
            # Channel resolution. When `channels` is empty we read from
            # ALL channels in chat_messages — no Discord-side resolution
            # needed. The legacy --channels "a,b" override path still
            # validates that those names exist in the connected guilds
            # (so a typo errors out instead of silently producing zero
            # results).
            targets: list[discord.TextChannel] = []
            if channels:
                for guild in client.guilds:
                    for ch in guild.text_channels:
                        if ch.name in channels:
                            targets.append(ch)
                if not targets:
                    print(f"ERROR: none of {channels} found",
                          file=sys.stderr, flush=True)
                    return
                print(f"Channels: {[ch.name for ch in targets]}",
                      flush=True)
            else:
                print("Channels: ALL (no filter — reading every "
                      "ingested channel in chat_messages)", flush=True)
            print(f"Backfilling last {days} days...", flush=True)

            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            # Note: previously this skipped settings.analyst_primary_author
            # (Abe) from profile generation under the assumption that the
            # KEY USERS block in the /ask system prompt was authoritative
            # for him. User now wants Abe profiled like anyone else — the
            # KEY USERS rules still apply for his trade voice / pick-roast
            # ban, the profile is additional context. The analyst_primary_
            # author setting still scopes the OCR watcher; it just no
            # longer excludes from profiling.

            # Per-user accumulator: user_id -> list of message dicts
            by_user: dict[int, list[dict]] = defaultdict(list)
            user_meta: dict[int, dict] = {}  # user_id -> {username, display_name}
            # Image attachments per user (oldest-first), used for vision
            # when profile_image_ocr_enabled. Each entry: {url, content_type, ts}
            images_by_user: dict[int, list[dict]] = defaultdict(list)
            # Slur counts per user (regex-matched in their own messages
            # over the scan window). Folded into the profile row at upsert.
            slur_counts: dict[int, int] = defaultdict(int)
            # Slur example snippets per user — newest-first, capped per
            # user. Surfaced in the published snapshot so readers can see
            # actual usage rather than just the bare count.
            slur_examples: dict[int, list[str]] = defaultdict(list)
            _SLUR_EXAMPLES_PER_USER = 5

            # Phase 2: prefer the local chat_messages store over a fresh
            # Discord scan. If the store has reasonable coverage for
            # these channels in this window, use it — much faster, no
            # gateway contention, no rate limits. The Discord-scan
            # fallback below stays in place for cold-start / failure
            # recovery.
            _STORE_COVERAGE_FLOOR = 100  # min rows to consider the store usable
            store_rows = db.count_chat_messages_for_channels(channels, days=days)
            use_store = store_rows >= _STORE_COVERAGE_FLOOR
            if use_store:
                (
                    by_user, user_meta, images_by_user,
                    slur_counts, slur_examples,
                ) = _load_user_data_from_store(channels, days)

            # Fix #5: Discord scan path removed. chat_ingestion is the
            # canonical source of historical Discord messages — anything
            # that scans Discord directly should live there, not be
            # duplicated in profile-refresh. If the store is empty for
            # the configured channels, the operator should trigger
            # /refresh_chat (which uses retry+resume + gap-detect) before
            # running profile-refresh.
            #
            # Result of this cleanup: ~120 LOC removed. Profile-refresh
            # is now a pure CONSUMER of chat_messages, not a producer.
            if not use_store:
                print(
                    f"ERROR: chat_messages store has only {store_rows} rows "
                    f"for these channels in the last {days}d (need >= "
                    f"{_STORE_COVERAGE_FLOOR}). Trigger /refresh_chat "
                    f"full_window:true to populate, then re-run profile "
                    f"refresh. Profile-refresh no longer scans Discord "
                    f"directly — chat_ingestion is the single source of "
                    f"truth for chat history.",
                    file=sys.stderr,
                    flush=True,
                )
                return

            # Filter to users meeting threshold
            min_msgs = (
                getattr(settings, "profile_min_messages", None)
                or MIN_MESSAGES_FOR_PROFILE_FALLBACK
            )
            delta_threshold = getattr(settings, "profile_delta_threshold", 50)

            # Bulk-fetch existing profiles so we can apply the delta skip:
            # a user with an existing profile is only re-profiled if they
            # have enough NEW messages (timestamp > stored last_seen_*)
            # since the last run. Brand-new users (no row) skip this check
            # and use min_msgs as the cold-start gate.
            existing_profiles = db.get_profiles_for_users(list(by_user.keys()))

            eligible: list[tuple[int, list[dict]]] = []
            skipped_lurkers = 0
            skipped_stable = 0  # existing profile, not enough new material
            if force:
                print(
                    "FORCE mode: bypassing delta_threshold — every user "
                    "above the 30-msg lifetime floor will be re-profiled.",
                    flush=True,
                )
            for uid, msgs in by_user.items():
                if len(msgs) < min_msgs:
                    skipped_lurkers += 1
                    continue
                if not force:
                    existing = existing_profiles.get(uid)
                    last_seen = (existing or {}).get("last_seen_message_at")
                    if existing and last_seen:
                        new_msgs_count = sum(
                            1 for m in msgs if m["timestamp"] > last_seen
                        )
                        if new_msgs_count < delta_threshold:
                            skipped_stable += 1
                            continue
                eligible.append((uid, msgs))
            eligible.sort(key=lambda t: -len(t[1]))  # most active first

            # Optional hard cap (default 0 = no cap; rely on threshold)
            max_n = settings.max_user_profiles
            if max_n > 0 and len(eligible) > max_n:
                trimmed = len(eligible) - max_n
                eligible = eligible[:max_n]
                print(f"\nCapped to top {max_n} users by message count "
                      f"(trimmed {trimmed} below the cap)", flush=True)
            print(
                f"\n{len(eligible)} users eligible "
                f"(>= {min_msgs} msgs, >= {delta_threshold} new since last profile)",
                flush=True,
            )
            print(
                f"  skipped: {skipped_lurkers} lurkers, "
                f"{skipped_stable} stable (already-fresh profiles)",
                flush=True,
            )

            summary_lines.append(
                f"# User profile backfill — last {days} days\n\n"
            )
            channels_label = (
                ", ".join(ch.name for ch in targets) if targets
                else "ALL ingested channels"
            )
            summary_lines.append(
                f"- **Channels:** {channels_label}\n"
            )
            summary_lines.append(
                f"- **Cutoff:** {cutoff.isoformat()}\n"
            )
            summary_lines.append(
                f"- **Cold-start threshold:** {min_msgs} messages\n"
            )
            summary_lines.append(
                f"- **Delta threshold (existing profiles):** "
                f"{delta_threshold} new messages since last profile\n"
            )
            summary_lines.append(
                f"- **Skipped:** {skipped_lurkers} lurkers, "
                f"{skipped_stable} stable (fresh profiles)\n"
            )
            summary_lines.append(f"- **Model:** {settings.gemini_model}\n")
            summary_lines.append(
                f"- **Vision (image OCR):** "
                f"{'enabled' if settings.profile_image_ocr_enabled else 'disabled'}"
                + (f" (cap {settings.profile_image_cap} imgs/user)\n\n"
                   if settings.profile_image_ocr_enabled else "\n\n")
            )

            # Fix A: failure counters across the whole batch. These feed
            # into the summary pipeline_event so we can spot the broken
            # pipeline pattern (high eligible, low success) without
            # querying per-user pipeline_events. Per-user failures are
            # also written as their own pipeline_events rows for
            # detailed forensics.
            n_success = 0
            n_failure_exception = 0
            n_failure_empty = 0

            # One shared aiohttp session for all image downloads across
            # the batch. Lives for the duration of the Gemini loop.
            async with aiohttp.ClientSession() as http_session:
                # Parallel batches of GEMINI_CONCURRENCY
                for i in range(0, len(eligible), GEMINI_CONCURRENCY):
                    batch = eligible[i:i + GEMINI_CONCURRENCY]
                    tasks = []
                    for uid, msgs in batch:
                        meta = user_meta[uid]
                        # Existing profile triggers incremental-update mode
                        # in _generate_profile (sends prior profile + only
                        # new messages to the model). None for cold-start.
                        # FORCE mode: deliberately pass existing=None so
                        # _generate_profile takes the cold-start branch —
                        # uses the FULL 30-day window of messages (not
                        # just messages > last_seen) and writes a fresh
                        # profile from scratch under the current prompt
                        # structure. Without this, force would bypass
                        # the eligibility gate but still feed only the
                        # tiny incremental delta to Gemini.
                        existing = (
                            None if force else existing_profiles.get(uid)
                        )
                        tasks.append(_generate_profile(
                            gemini_client,
                            meta["display_name"],
                            msgs,
                            username=meta.get("username", ""),
                            http_session=http_session,
                            images=images_by_user.get(uid, []),
                            user_id=uid,
                            existing_profile=existing,
                        ))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    # Single Gemini call per user returns
                    # (profile_text, trader_score, trader_rationale,
                    #  racial_humor_score) as one JSON response.
                    for (uid, msgs), result in zip(batch, results):
                        meta = user_meta[uid]
                        # Fix A: persistent per-user failure logging.
                        # Previously every failure was stdout-only and died
                        # with the container. Now they're rows in
                        # pipeline_events so we can spot patterns (which
                        # users, which failure mode, repeat skips) without
                        # access to live worker logs.
                        if isinstance(result, Exception):
                            n_failure_exception += 1
                            print(f"  ✗ {meta['display_name']}: {result}",
                                  flush=True)
                            try:
                                db.record_pipeline_event(
                                    "profile_user_failure", "exception",
                                    {
                                        "user_id": uid,
                                        "username": meta.get("username"),
                                        "display_name": meta.get("display_name"),
                                        "msg_count": len(msgs),
                                        "exception_type": type(result).__name__,
                                        "exception_message": str(result)[:500],
                                    },
                                )
                            except Exception as log_err:
                                print(f"    (failure-event write failed: {log_err})",
                                      flush=True)
                            continue
                        profile, trader_score, trader_rationale, racial_humor_score, racism_rationale = result
                        # Guard: don't overwrite a substantial prior
                        # profile with a much shorter truncated one.
                        # Heavy users sometimes get section-1-only
                        # responses even after retries; we'd rather
                        # KEEP the prior long profile than downgrade
                        # to a partial. Bypass when force=True only if
                        # the new profile is genuinely viable (>=2000
                        # chars). Otherwise treat as failure.
                        prior = existing_profiles.get(uid)
                        prior_len = len((prior or {}).get("profile_text") or "")
                        new_len = len(profile or "")
                        if (
                            profile
                            and prior_len >= 2000
                            and new_len < 2000
                            and new_len < int(prior_len * 0.5)
                        ):
                            n_failure_empty += 1
                            print(
                                f"  ⚠ {meta['display_name']}: truncated "
                                f"({new_len} chars) vs prior {prior_len} — "
                                f"keeping prior, treating as failure",
                                flush=True,
                            )
                            try:
                                db.record_pipeline_event(
                                    "profile_user_failure", "truncated_downgrade",
                                    {
                                        "user_id": uid,
                                        "username": meta.get("username"),
                                        "display_name": meta.get("display_name"),
                                        "msg_count": len(msgs),
                                        "new_len": new_len,
                                        "prior_len": prior_len,
                                    },
                                )
                            except Exception:
                                pass
                            continue
                        if not profile:
                            n_failure_empty += 1
                            print(f"  ✗ {meta['display_name']}: empty / unparseable response",
                                  flush=True)
                            try:
                                db.record_pipeline_event(
                                    "profile_user_failure", "empty_response",
                                    {
                                        "user_id": uid,
                                        "username": meta.get("username"),
                                        "display_name": meta.get("display_name"),
                                        "msg_count": len(msgs),
                                        "trader_score_returned": trader_score,
                                        "racial_humor_score_returned": racial_humor_score,
                                        "trader_rationale_len": (
                                            len(trader_rationale) if trader_rationale else 0
                                        ),
                                    },
                                )
                            except Exception as log_err:
                                print(f"    (failure-event write failed: {log_err})",
                                      flush=True)
                            continue

                        # Verify the profile's quoted phrases against
                        # the user's actual chat_messages. Catches
                        # hallucinated quotes the incremental-update
                        # path carried forward from a stale prior
                        # profile. Quotes are extracted from the
                        # profile_text body (which now contains all
                        # the verbatim Voice / Retarded takes /
                        # Recent personal life material).
                        claim_check = _verify_profile_claims(
                            profile, None,
                            meta.get("username", ""),
                        )
                        if claim_check["unverified_count"] > 0:
                            print(
                                f"  ⚠ {meta['display_name']}: "
                                f"{claim_check['unverified_count']} of "
                                f"{claim_check['checked_quotes']} quoted "
                                f"phrases not found in their chat — "
                                f"possible hallucinations: "
                                f"{claim_check['unverified_quotes'][:3]}",
                                flush=True,
                            )

                        last_seen = msgs[-1]["timestamp"]
                        slur_n = slur_counts.get(uid, 0)
                        # JSON-encode the regex slur examples (TEXT
                        # column). slur_examples is deterministic
                        # floor signal — kept even though primary
                        # ammo display now lives inside profile_text.
                        slur_ex = slur_examples.get(uid, [])
                        slur_ex_json = json.dumps(slur_ex) if slur_ex else None

                        # trader_examples and personal_ammo deliberately
                        # not passed — both removed from the schema.
                        # Their content lives inside profile_text now
                        # (Voice / Retarded takes / Recent trades /
                        # Recent personal life sections). Old DB rows
                        # keep stale data via COALESCE.
                        db.upsert_user_profile(
                            user_id=uid,
                            username=meta["username"],
                            display_name=meta["display_name"],
                            profile_text=profile,
                            message_count_at_update=len(msgs),
                            last_seen_message_at=last_seen,
                            slur_count=slur_n,
                            racial_humor_score=racial_humor_score,
                            trader_score=trader_score,
                            trader_rationale=trader_rationale,
                            racism_rationale=racism_rationale,
                            slur_examples=slur_ex_json,
                        )
                        n_success += 1
                        n_imgs = len(images_by_user.get(uid, []))
                        rh_display = (
                            racial_humor_score
                            if racial_humor_score is not None else 'n/a'
                        )
                        summary_lines.append(
                            f"## {meta['display_name']} ({meta['username']}) — "
                            f"{len(msgs)} msgs"
                            + (f", {n_imgs} imgs"
                               if n_imgs and settings.profile_image_ocr_enabled
                               else "")
                            + f"\n\n_slurs: {slur_n} · "
                            f"racial-humor: {rh_display}/100 · "
                            f"trader-score: {trader_score if trader_score is not None else 'n/a'}_"
                            + (f"\n_rationale: {trader_rationale}_"
                               if trader_rationale else "")
                            + f"\n\n{profile}\n\n---\n\n"
                        )
                        img_tag = (
                            f" + {min(n_imgs, settings.profile_image_cap)} imgs"
                            if n_imgs and settings.profile_image_ocr_enabled
                            else ""
                        )
                        score_bits = [f"slur={slur_n}"]
                        if racial_humor_score is not None:
                            score_bits.append(f"rh={racial_humor_score}")
                        if trader_score is not None:
                            score_bits.append(f"trader={trader_score}")
                        score_tag = " | " + " ".join(score_bits)
                        print(
                            f"  ✓ {meta['display_name']:<25} "
                            f"{len(msgs):>4} msgs{img_tag}"
                            f"{score_tag} "
                            f"→ {len(profile)} chars",
                            flush=True,
                        )

            # trader_rank is now computed on-read via
            # db.get_global_trader_ranks() — no batch recompute needed.
            # See db.recompute_trader_ranks_on_profiles docstring for
            # the deprecation note.

            print(f"\nProfiled {len(eligible)} users.", flush=True)

            # Observability (fix #7 + Fix A): record run summary with
            # success/failure tallies. eligible == n_success +
            # n_failure_exception + n_failure_empty when the loop ran
            # to completion. If they don't add up, the loop exited
            # early — that's its own bug signal.
            try:
                db.record_pipeline_event(
                    "profile_refresh",
                    "completed",
                    {
                        "window_days": days,
                        "channels": channels,
                        "eligible": len(eligible),
                        "succeeded": n_success,
                        "failed_exception": n_failure_exception,
                        "failed_empty": n_failure_empty,
                        "skipped_lurkers": skipped_lurkers,
                        "skipped_stable": skipped_stable,
                        "store_rows_in_window": store_rows,
                    },
                )
            except Exception as e:
                print(f"  (could not record pipeline event: {e})", flush=True)
        finally:
            await client.close()

    await client.start(settings.discord_bot_token)

    out_name = (
        f"backfill_user_profiles_"
        f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.md"
    )
    out_path = _REPO_ROOT / out_name
    out_path.write_text("".join(summary_lines), encoding="utf-8")
    print(f"\nSummary written to {out_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30,
                        help="Days of history to scan (default 30)")
    # Default to empty — profile builder reads ALL ingested channels in
    # chat_messages within the window. Pass --channels "a,b" to scope
    # tighter for ad-hoc backfills. The narrower profile_channels filter
    # was dropped (image OCR + broader ingest provides richer signal).
    parser.add_argument(
        "--channels", type=str, default="",
        help="Comma-separated channel names. Default empty = ALL "
             "channels in chat_messages."
    )
    parser.add_argument(
        "--no-vision", action="store_true",
        help=(
            "Force text-only mode — skip image attachment entirely even "
            "if images are available. Used to A/B-test whether the vision "
            "path is causing short / truncated profile outputs."
        ),
    )
    parser.add_argument(
        "--force", action="store_true",
        help=(
            "Bypass the delta-threshold gate. Re-profile every user "
            "above the lifetime min_messages floor regardless of how "
            "many new messages they've accumulated. Used after a prompt "
            "change to refresh all existing dossiers in one shot."
        ),
    )
    args = parser.parse_args()
    if args.no_vision:
        # Override the setting in-process; doesn't touch the env var
        settings.profile_image_ocr_enabled = False
        print("--no-vision: vision pipeline disabled for this run",
              flush=True)
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels, force=args.force))


if __name__ == "__main__":
    main()
