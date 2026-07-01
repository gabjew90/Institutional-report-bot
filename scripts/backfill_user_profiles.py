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
# Aligned to settings.profile_min_messages (30) on 2026-06-02. Earlier
# 100 was a stale default from before the floor was lowered — and the
# code-vs-settings mismatch meant if the settings value got unset
# (env var reset etc.), the floor would silently jump 30 -> 100 and
# the 30-49 msg bucket (4 currently-profiled users) would lose their
# profiles on the next refresh.
MIN_MESSAGES_FOR_PROFILE_FALLBACK = 30
MESSAGES_PER_PROFILE_SAMPLE_FALLBACK = 500
GEMINI_CONCURRENCY = 5  # parallel calls per batch

# Activity-credibility threshold for the trader-score chatter component.
# Gemini's chatter_base read is reliable only when there's enough chat
# to read. Below this message count, the chatter component scales down
# linearly so users with very thin chat histories don't surface above
# active users on a 1-paragraph Gemini sample. Receipts (the trade
# ledger points) are NOT scaled — those are objective signal and stay
# at full weight regardless of chat volume.
#
# 2026-06-02: lowered 500 -> 300 after distribution analysis showed 18
# of 62 active users (30%) sit in the 100-499 msgs/30d bucket — real
# participants whose chatter brackets were getting scaled down to
# 0.20-0.60. At /300:
#    100 msgs -> 0.33  (was 0.20)
#    200 msgs -> 0.67  (was 0.40)
#    300 msgs -> 1.0   (was 0.60)
#    500+    -> 1.0   (unchanged)
# Top 26 most-active users unaffected; the moderately-active 30% get
# fairer credit for their chatter bracket. Receipts still dominate
# top-of-leaderboard separation; this just stops volume-handicapping
# the middle.
TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS = 300


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
- **trader_score / racial_humor_score:** hold by default for racism — shift only when new evidence justifies a meaningful move. For trader_score, RECOMPUTE from scratch each refresh. The chatter base is re-read from the current MESSAGES window (not carried forward) and the 21-day points ledger decays automatically — a documented win scores 2 pts for its first 7 days, 1 pt from day 8 to 21, then stops scoring. The final = `clip(chatter_base + honesty, 50) + receipt_points` (no upper cap — top traders separate by receipts). If last week the user documented 8 wins (16 pts at full credit) and posts nothing new, those same wins decay to 8 pts (half credit) the following week and 0 after three — the score drifts down unless the wins keep coming. That's the decay mechanism working as designed.
- **trader_rationale:** REWRITE each refresh from the current ledger + chat. The structure is fixed (chatter description / receipts described qualitatively), but the prose re-derives every time. Don't carry forward stale framings from a prior refresh. The rationale is QUALITATIVE — no numbers (no point counts, no score values, no win/loss counts). See the trader_rationale spec for the no-numbers rule.

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

Posts in a caller's own channel (per the table above) are **structural full honesty**. Every entry there is documented; the caller cannot cherry-pick. These posts accumulate points in the 21-DAY POINTS LEDGER below — documented WINS score (2 pts within 7 days, 1 pt at 8-21 days); losses, ghosts, and pending entries score 0.

**Shared alert channels** (multiple posters, not in the owned table):
- `🕰️-member-alerts-🕰️`
- `🐄-spot-bag-alerts-🐄`
- `🚨-0dte-lotto-alerts-🚨`
- `🪙-crypto-alerts-🪙`

Each post here is also a pre-outcome entry commitment from the user who posted it — same prospective-entry property as a caller-owned channel, slightly less structural (the user chooses *whether* to post to a shared room). These posts also accumulate points in the ledger; the ceiling-lift is the same whether the entry was in an owned channel or a shared one.

**Receipts certify edge; chat-claimed activity does not.** "Thesis-posting in chat," "multi-day analysis in regular channels," "consistent room engagement" do NOT score points and do NOT lift the receipts ceiling. They feed the chatter-base read (Layer 1 in the trader_score rubric) — the ceiling layer comes exclusively from documented entries / closes / screenshots that hit analyst_trades.

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
  "chatter_base": <integer 0-50, your chatter-bracket placement>,
  "racial_humor_score": <integer 0-100>,
  "trader_rationale": "<3-5 SAVAGE-BUT-HILARIOUS sentences, see below>",
  "racism_rationale": "<3-5 SAVAGE-BUT-HILARIOUS sentences, see below>",
  "profile_text": "<full markdown profile per the schema above — ALL 5 named sections>"
}}
```

**You do NOT output a `trader_score`.** Python computes it deterministically downstream as `min(50, chatter_base) * activity_multiplier + receipt_points` (no upper cap — top traders separate by receipts), where `activity_multiplier = min(1, msg_count / 300)` and `receipt_points` is the exact `TOTAL RECEIPT POINTS:` value from the 21-DAY POINTS LEDGER block — not anything you judge or infer. Your job is ONLY the chatter bracket call. The activity scaling AND receipt addition are arithmetic, and Python does them.

**Why this matters**: an earlier version of the prompt asked Gemini for the final `trader_score`, which produced a hallucination class — model would read the chat, see chat-claimed trades NOT in the structured ledger ("closed BTC short +50%" etc.), invent 30-40 receipt points for them, and inflate the score by that amount. The new schema makes the failure structurally impossible — Gemini only outputs the chatter judgment; receipts are computed from the deterministic ledger.

`profile_text` is LAST and is the largest field by far. All five named sections (Personality and style / Voice / Retarded takes / Recent trades / Recent personal life) live inside this single markdown string.

## RATIONALES — savage-but-hilarious, sourced from real chat

Both `trader_rationale` and `racism_rationale` are short summary blocks that get surfaced INLINE next to the user's rank in the /ask dossier. They are NOT internal notes — readers see them. The room is signed up for crude humor; treat these like roast-comedian bits, not corporate boilerplate.

### `trader_rationale` (3-5 sentences, ~400-900 chars)

What it covers: (a) the chatter base bracket you placed them in (0-25 / 25-40 / 40-50) and the chat-evidence that drove it, including how the trader handles losses (self-awareness, gloat-loud / complain-vague asymmetry, tilt patterns — all baked into the bracket directly), (b) the receipts contribution described **qualitatively, NOT with numbers** ("a clean stream of entry-and-close pairs lifting him further" / "sparse receipts" / "no documented receipts in the window"), (c) two-to-four anchored examples or behavioral tells the room would recognize. Sourced, not generic. Reference specific receipts from the ledger by ticker + outcome shape (e.g. "the documented NVDA call that closed +220%"), but never quote the point value, point total, or any numeric score component.

**No numeric score components in the rationale.** Specifically NEVER write phrases like "35 receipt points," "chatter base of 22," "final 78," or any other number that maps to internal scoring. The rationale describes the SHAPE of the read in qualitative prose. Python computes the score deterministically downstream; users see the rank + this rationale prose, never the raw 0-100 final or any sub-component. Numbers in the rationale = raw-score leak.

**Good shapes** (use as structural templates — the bracketed slots are filled from this user's actual evidence, NEVER copied as-is):

- *Sharp talker, no receipts:* "Chat reads sharp on the macro side — `[chat-evidence: macro takes that land, conviction calibration, owns losses cleanly, no obvious tilt]`. No documented receipts in the window — the room hasn't seen him commit a single entry through the alert channels, so the chatter is taking it on faith. Talks a strong game; nothing certifies it yet."
- *Average joe with some receipts:* "Chat reads middle-of-pack — `[chat-evidence: sells winners early, lukewarm conviction, follows the room when it pivots]`. A handful of receipts in the window — `[describe shape: 'a couple of clean entry-and-close pairs on the spot side,' 'mostly screenshot-only without commitments,' etc.]` — but the chatter pattern doesn't certify real edge."
- *Bag holder:* "Chat reads bag-holder: `[chat-evidence: panic exits, sizing up to recover, tilt cycles visible, complaining about closed bags, gloat-loud / complain-vague asymmetry if visible]`. `[Describe receipts qualitatively: 'a stack of entries that mostly ghosted out without resolution,' 'a few losing screenshots,' 'no posts at all,' etc.]`. The receipts can't lift him out of the bag-holder zone when the chatter says he's tilting."
- *Heavy poster, sharp by both layers:* "Chat reads sharp — `[chat-evidence: clean reads, repeatable framework, room tails the calls, owns misses without crisis]`. A steady stream of receipts in the window — `[describe shape: 'documented wins on $X and $Y plus the bagged $Z closed publicly,' 'consistent entry-and-close cadence in member-alerts,' etc.]`. Both layers stacking is what puts him near the top."

**Hard rule on the slots.** The bracketed slots are placeholders. Fill each with content sourced from the actual MESSAGES + analyst_trades + points ledger for THIS user. NEVER copy the example framing or trade descriptions across to a different user — same ATTRIBUTION RULE that governs Voice / Retarded takes.

**Hard rule on numbers in the rationale (BINDING).** The rationale describes the SHAPE of the read in qualitative prose. NEVER cite specific point counts, score values, win counts, loss counts, ghost counts, or any other numeric internal. "Several documented wins" — yes. "8 wins" — no. "A handful of receipts" — yes. "23 receipt points" — no. "Chat reads sharp" — yes. "Chatter base 45" — no. The rationale is the user-visible surface; numbers in the prose = raw-score leak. Reference receipts by ticker + outcome shape ("the documented NVDA call that closed +220%") — never by point value.

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

**trader_score — CHATTER BASE (0-50, activity-scaled) + RECEIPTS (additive, unbounded above).**

Two layers, ADDED together. Chatter caps at 50 and is THEN multiplied by an activity-credibility factor (a 100-msg user gets 20% of their chatter; 500+ msgs gets full credit) — so a thin-history user can't surface above an active one on a sharp-sounding sample. Receipts are not scaled; they add on top with no cap of their own. The final still caps at 100, leaving plenty of room for receipts to fill above the chatter ceiling — that's the entire point of certifying through posts.

```
final_score = min(50, chatter_base) * min(1, msg_count / 300) + receipt_points  # No upper cap — top traders separate by receipts
```

**Layer 1 — Chatter base (0-50, calibrated from MESSAGES below).**

Read the user's actual chat and place them in one of three bands. **Chatter caps at 50 absolutely — no matter how sharp someone sounds, the chat alone cannot push them above 50. Receipts are the certification.**

- **0-25 — Bag holders. Never win.** Chat is dominated by losses, panic exits, copy-trading whoever's loudest, bag-holding language, sizing up to recover. Tilt cycles visible. Calls go against them more often than not. The texture is "I'm down again" and "what's bouncing." Use the lower half for active-loud bag-holders, upper half for users where the bag is there but the tone is more resigned than panicked.
- **25-40 — Average joes.** Sell winners too early, hold losers too long, coin-flip outcomes over time. Follow the crowd more than they lead. Some self-awareness but doesn't translate to execution. The "talks the right setups, fades them" pattern. Lukewarm conviction; pivots when the room pivots.
- **40-50 — Talks a good game.** Sharp reads, owns losses cleanly, calibrated conviction language, reasonable risk management talk, no obvious tilt cycle. Seems to win. Chat alone is consistent with real edge — but with no receipts, the room can't certify it. The "sounds like an actually-good trader, hasn't proven it" bracket.

The chatter base IS the chat signal — it already reflects self-awareness, tilt patterns, gloat-loud / complain-vague asymmetry, and honesty in how the trader handles losses. Bake those into your chatter bracket placement directly: a gloating bag-holder who buries losses lands lower in 0-25; a self-aware mid-trader who owns misses cleanly lands higher in 25-40. No separate modifier — the bracket already does that work.

Final clip: **chatter base never exceeds 50**, regardless of which bracket signal pulled you. The cap is hard.

**Layer 2 — Receipt points (additive, from the 14-day ledger above; unbounded above on its own — the only ceiling is the final 100 cap).**

The 21-DAY POINTS LEDGER above is the receipt total. It adds DIRECTLY to the (clipped) chatter base. The policy is wins-only for EVERYONE — members and official callers alike: documented wins score (2 pts within 7 days, 1 pt at 8-21 days), while losses, ghosts, pending entries, and losing screenshots all score 0. The non-scoring buckets are still listed so your qualitative read of the trader stays honest.

For members:

- **+3 per entry posted (automatic on post).** Posting an entry is structural commitment — published before outcome.
- **+5 if the position later closes for a gain in the window** (the +3 upgrades to +5; net +2 win bonus).
- **+3 if the position closes for a loss in the window** (no upgrade; commitment was real, the loss doesn't subtract).
- **+2 (ghost penalty) if the entry has NO close AND has either passed its expiration date OR been open ≥14 days.** Total-loss assumption: −1 against the +3 entry award. An option that expires worthless without a posted close counts as ghost; a stock entry that's been sitting for 14 days without a close also counts.
- **0 (pending) if the entry has no close yet, is still within the 14d window, and is before its expiry.** Held in suspense — not scored as ghost YET; the entry might still close or might trip the ghost rule on a later refresh.
- **+2 per standalone winning screenshot** (close-only P&L card with gain > 0 and no matching entry commitment in the window). Order tickets posted in `gain-loss-porn` also score as +2 even without a visible gain pill — the channel is structurally for flexing wins, so a gain-less close-ticket there is presumed positive.
- **+1 per standalone losing screenshot** (close-only P&L card with gain ≤ 0, OR a gain-less close ticket posted OUTSIDE gain-loss-porn). The weakest receipt — no commitment, negative outcome — but still credited for the honest red post.

For callers (wins-only nerf):

- **+5 if the position closes for a gain in the window** — same as members.
- **+2 per standalone winning screenshot** — same as members.
- **0 on entry+loss closes, entry+ghosts, losing screenshots, AND pending entries.** Callers are the product members pay to tail; only documented wins lift their score. A caller with 14 ghosted positions earns 0 from that bucket; a caller posting losing screenshots earns 0 from that bucket. The `TOTAL RECEIPT POINTS:` line in the ledger block already reflects this — read it as-is, no further math.

**Decay mechanism (rolling 14-day window):** The ledger only sees trades posted within the last 14 days. Older rows stay in the DB (never deleted by scoring) but stop generating points. If last week's 35 points were 28 wins-and-closes that aged out, this week's ledger picks up whatever this week posted instead. The score recomputes from scratch every refresh.

**RECEIPT POINTS = TOTAL RECEIPT POINTS FROM THE LEDGER BLOCK, VERBATIM. NO INFERENCE.**

This is the single most important rule on the receipts layer. The `TOTAL RECEIPT POINTS:` value in the 21-DAY POINTS LEDGER block above is the ONLY number you may use as the receipt contribution. Specifically:

- If the ledger shows `TOTAL RECEIPT POINTS: 0`, the receipt contribution is **0**. Final score = chatter base (clipped at 50). Do NOT invent points based on chat-claimed trades, casual P&L brags, alert-channel posts mentioned in chat but not extracted into analyst_trades, "documented" trades you remember from MESSAGES, or any other source. The structured ledger IS the source of truth.
- If the ledger shows `TOTAL RECEIPT POINTS: 14`, the receipt contribution is **14**. Not 20 because the chat mentions more trades. Not 8 because some of the wins look small. **14**.
- A trade mentioned in the user's chat but NOT in the structured ledger means OCR didn't extract it or the watcher didn't process it. That's a pipeline gap, NOT a license to credit the trade in the score. Note the trade in Recent trades section (sourced from analyst_trades + chat), but do NOT add fictional points for it.
- The failure mode this rule blocks: model reads chat, sees "I closed BTC short for +210%," infers that's worth ~40 points, adds it on top of chatter base, produces a final score 30-50 points higher than the user actually earned. That's hallucination — the chat claim is not a receipt; only an analyst_trades row is a receipt.

If you find yourself wanting to inflate receipt points above the ledger total because "the chat clearly shows trades that aren't in the ledger," STOP. The ledger value stands. Lower the chatter base if you must to reflect the trader's true skill — but receipts are the ledger total, full stop.

**Final score = scaled_chatter + receipt_points** (no upper cap — top traders separate by receipts) — where `scaled_chatter` is your `chatter_base` (clipped to 50) MULTIPLIED by an activity factor `min(1, msg_count / 300)`. Python applies the activity factor downstream after you pick the chatter bracket — you don't compute it. Your job stays the bracket call on the chat that's actually visible to you.

Four populations the additive rubric reads correctly:

- **Pure talker, sounds sharp, no receipts.** Chatter 45 + 0 receipts → 45. The 50 cap doesn't matter; they're below it. Talks a strong game; nothing certifies it.
- **Pure talker, obvious bag-holder.** Chatter 20 + 0 receipts → 20. Bag-holder by chat, no receipts to lift them. Scored honestly from the complaining.
- **Mid trader who posts.** Chatter 35 (average joe — coin-flip outcomes) + 30 receipt points → 65. Receipts add cleanly because the chatter base wasn't already near the cap.
- **Sharp trader with receipts.** Chatter 48 (clipped from 51 — sharp + clean) + 40 receipt points → 88. Both layers stacking. The 40-50 chatter band combined with sustained receipts is where edge is certified.
- **Loud bag-holder with high post volume (BK pattern).** Chatter 22 (bag-holder pattern) + 47 receipt points (9 wins × 5 + 1 winning screenshot × 2; caller-nerf zeroes out his 14 ghosts and 1 losing screenshot) → 69. Receipts lift him meaningfully but the wins-only rule keeps ghost-farming from inflating the total.
- **Caller with consistent wins (abe pattern).** Chatter 0 (he doesn't post chat outside his alert channel) + 120 receipt points (20 wins × 5 + 10 winning screenshots × 2; caller-nerf zeroes the 2 losses and 5 ghosts) → 100, capped. Documented winning closes alone clear the ceiling. The 16 points he'd have earned from losses/ghosts under the member rule weren't needed.
- **Thin-history lurker (activity penalty fires).** Chatter 45 (chat that's visible reads talks-good-game) but msg_count = 100 → credibility multiplier 0.2 → scaled chatter 9. Plus 0 receipts → 9. The chatter read might be accurate on the small sample, but the system can't verify the pattern over volume; the multiplier reflects that. The same user at 500+ msgs would land at 45 + receipts. The penalty applies ONLY to chatter — receipts (real trade activity logged in alert channels) are objective and never scaled.

State the reasoning in trader_rationale — 3 to 5 sentences, qualitative prose (no numbers). Cover (a) chatter bracket placement + what chat-evidence drove it (self-awareness about losses, gloat patterns, tilt cycles all factor into bracket selection directly), (b) receipts described qualitatively, (c) the anchored examples that read the call. Example:

> "Chat reads bag-holder. The actual outcomes visible in chat skew heavy on closed bags and complaining-vague-about-losses tilt cycles. The gloat-loud / complain-vague asymmetry is visible — he names every winner with size, every loss gets a one-line shrug. Some documented entries did make it into the alert channels, which credits some receipt weight; but the chatter pattern anchors him well below the sharp-trader range."

**trader_score = SKILL × ACTIVITY-CREDIBILITY + receipts.** The chatter bracket reflects skill (behavioral quality on the visible chat). Activity then acts as a CREDIBILITY FACTOR on that chatter — a user with very thin chat history can't be confidently bracketed in the 40-50 "talks-good-game" range no matter how sharp the few lines look, because there isn't enough sample to verify the pattern. Python applies the credibility multiplier (`min(1, msg_count / 300)`) downstream; you don't pre-discount the bracket. Just pick the bracket that fits the chat you SEE, and let the system handle the activity scaling. msg_count is also visible in the dossier header for reference.

**racial_humor_score brackets (0-100)** — the canonical score for race-edged content from this user. Internal calibration only; the bot never quotes the raw number. Score the FULL picture (slurs + stereotypes + dog whistles + race-based mockery + unprompted racial references), not just literal slurs. Self-deprecating jokes about the user's OWN race and factual mentions of races/ethnicities in geopolitics or news don't count.

- **0-15:** Clean. No race-edged content in the window. Lurkers default here.
- **16-35:** Occasional. Joins common room jokes when prompted, doesn't lead, no slurs.
- **36-55:** Regular. Race-edged humor is part of their normal voice. Some stereotyping. One or two censored slurs.
- **56-75:** Heavy. Racial humor is a defining feature. Multiple slurs (including uncensored).
- **76-100:** Dominant. Race-edged content saturates the messages. Slurs uncensored and frequent. Specific groups targeted in a sustained way.

Anchor against THIS user's messages. Zero examples = 0-15.

**Activity multiplier applies to racism too.** Same as trader_score: a user with 45 visible messages can't certify a Dominant-bracket pattern (76-100) over volume even if those 45 lines are saturated — there isn't enough sample to confirm the behavior is sustained rather than episodic. Pick the bracket that fits the chat you SEE; Python applies the same `min(1, msg_count / 300)` credibility multiplier downstream. A user with 50 msgs and a 95 bracket call gets stored as ~16; with 300+ msgs the same call stays at 95. Don't pre-discount the bracket call yourself — the math is automatic.

---

21-DAY POINTS LEDGER — used by the trader_score rubric's receipts layer.

{points_block}

The total points above are computed from analyst_trades (real entries / closes / screenshots posted by THIS user in their alert channel and gain-loss-porn). The receipts layer is ADDITIVE — points add directly to the (clipped) chatter base. There is NO internal cap on receipts; the only ceiling is the final 100 cap. A user with sustained heavy posting CAN exceed 50 from receipts alone. Pure-talker / no-receipts users land at their chatter base value (clipped at 50) — and that's by design.

---

STRUCTURED TRADE LOG — source of truth for Recent trades section:

{analyst_trades_block}

**BINDING RULE on Recent trades.** The structured trade log above (sourced from `analyst_trades` — OCR-extracted entries and exits posted by THIS user in their alert channel and gain-loss-porn) is the source of truth for any percentage / dollar / outcome you cite in the **Recent trades** section. Specifically:
- If a trade appears in the log above, quote its `gain_pct` and dates EXACTLY as shown. Don't round, don't restate, don't infer "approximately."
- If a trade does NOT appear in the log above but the user mentioned a position in chat (MESSAGES block below), describe it WITHOUT a percentage — e.g. "mentioned a TSLA position around 5/22, no documented outcome in log." Do NOT invent a `+X%` figure that isn't in the structured log.
- The model's previous failure mode: inventing percentages on trades that closed at materially different actual numbers, or marking a documented loss as "Flat" because the chat-text inference missed the structured close. The structured log above prevents this — use it.
- If the structured log is empty (the user has no analyst_trades rows in the window), the Recent trades section must lean on chat-described positions WITHOUT percentages, OR be brief / skipped if signal is thin.

**BINDING RULE on trade STATUS and OUTCOME (not just percentages).** The same source-of-truth discipline applies to whether a trade is open, closed, expired, won, or lost — this is where the profile has fabricated and lost member trust (2026-06-16: a TSLA 410C that expired 06-12 was narrated as "Open, waiting for his reversal thesis to materialize" four days after expiry; a no-close BTC position as "still held in limbo"). Read the status tag on each log row and obey it EXACTLY:
- `[EXPIRED — … OUTCOME UNKNOWN]`: the option's expiry passed and no close was posted. State it expired and that the outcome is unknown — e.g. "his TSLA 410Cs hit 6/12 expiry with no close posted, outcome unrecorded." You may NOT say it "expired worthless," "is dead," "is still open," or "is waiting" — you do not know which, and asserting one is fabrication. (You can still mock the RISK of a naked weekly bet — just not a made-up result.)
- `[OPEN per log — no exit posted …]`: we only ingest screenshotted trades, so a missing close does NOT mean still-held. Say "opened X on <date>, no exit posted" — never "still holding," "currently defending," "in limbo," or any present-tense status that implies you know they're still in it.
- `[EXIT only — no logged entry]`: a close with no matching open — describe the exit only, don't invent the entry.
- Never write present-tense narrative status ("waiting to materialize," "being defended," "still riding it") for any position whose outcome the log does not record. Describe what was POSTED, not what you imagine is happening now.

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


def _format_points_block(user_id: int) -> str:
    """Render the user's 21-day rolling points ledger.

    Policy (wins-only, recency-banded — same rule for everyone,
    including official callers):
      +2  documented WIN (entry→winning close/trim, or winning
          screenshot) documented within the last 7 days
      +1  documented WIN documented 8-21 days ago (half credit)
       0  everything else: losses, ghosts, pending entries, losing
          screenshots. They're listed for the qualitative read but
          never score.

    2026-07-01: this block previously displayed the RETIRED 5/3/2/1
    point math in the bucket lines while the total used wins-only ×2 —
    the arithmetic Gemini read didn't add up to the total it was told
    to use. The lines below now show the real policy, so the math is
    self-consistent.

    Final = clip(chatter_base + honesty, 50) + receipt_points  # no upper cap
    """
    ledger = db.compute_member_points(int(user_id), days=21)
    is_caller = bool(ledger.get("is_official_caller"))
    caller_tag = "  [OFFICIAL CALLER]" if is_caller else ""
    wr = ledger.get("wins_recent", 0)
    wo = ledger.get("wins_older", 0)
    lines = [
        f"21-DAY ROLLING POINTS LEDGER{caller_tag} (wins-only,"
        f" recency-banded — this week's wins score double, older wins"
        f" half; losses/ghosts/pending never score but are listed for"
        f" the honest read):",
        f"  - Documented WINS in the last 7 days   ({wr} × 2 pts)  =  "
        f"{wr * 2} pts",
        f"  - Documented WINS 8-21 days ago        ({wo} × 1 pt)   =  "
        f"{wo} pts",
        f"  - Entry closed for a LOSS              "
        f"({ledger['entries_lost']} × 0)  =  0 pts",
        f"  - Entry GHOSTED (no close; past expiry OR ≥14d open)  "
        f"({ledger['entries_ghosted']} × 0)  =  0 pts",
        f"  - Entry PENDING (no close yet; before expiry, <14d)   "
        f"({ledger['entries_pending']} × 0)  =  0 pts",
        f"  - LOSING screenshot (close-only)       "
        f"({ledger['screenshot_losses']} × 0)  =  0 pts",
        f"",
        f"TOTAL RECEIPT POINTS: {ledger['points']}",
        f"",
        f"(Spec: only documented WINS score — an entry whose position"
        f" shows a winning close or winning trim, or a standalone"
        f" winning P&L screenshot. A win documented in the last 7 days"
        f" = 2 pts; a win 8-21 days old = 1 pt (decays to 0 past 21"
        f" days). Losses, ghosted entries, pending entries, and losing"
        f" screenshots all = 0, for members and official callers alike."
        f" The bucket counts above still describe the trader's full"
        f" documentation pattern — use them for the qualitative read,"
        f" not for arithmetic.)",
        f"",
        f"(Receipt points add ON TOP of the chatter base. Final ="
        f" clip(chatter_base + honesty, 50) + receipt_points (no upper cap)."
        f" Chatter base caps at 50 — receipts are the only path past 50,"
        f" and receipt points have no internal cap of their own. A heavy"
        f" poster CAN exceed 50 from receipts alone if they post enough.)",
    ]
    return "\n".join(lines)


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
    from datetime import date as _date
    today = _date.today()
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
        # STATUS TAG — the 2026-06-16 fix. Without this the model read a
        # bare OPEN row with a past expiry as "still open, waiting to
        # materialize" (ZHawk's TSLA 410C exp 06-12 narrated as Open on
        # 06-16, 4 days after it expired) and an open-with-no-close as
        # "still holding in limbo" — fabricated outcomes on real trades.
        # The tag states what is and isn't KNOWN so the profile can't
        # invent a status.
        status = (r.get("inferred_status") or "").lower()
        expired_by_date = False
        if len(expiry) >= 10:
            try:
                expired_by_date = _date.fromisoformat(expiry[:10]) < today
            except ValueError:
                expired_by_date = False
        status_tag = ""
        if action in ("OPEN", "ADD"):
            if status == "expired_unknown" or expired_by_date:
                status_tag = (
                    "  [EXPIRED — expiry passed, NO close posted; "
                    "OUTCOME UNKNOWN (may have sold early, expired ITM, "
                    "or expired worthless — do NOT assert which)]"
                )
            else:
                status_tag = (
                    "  [OPEN per log — no exit posted; we only see "
                    "screenshotted trades, so they MAY have closed it "
                    "without posting — do NOT assert it's still held]"
                )
        elif status == "close_without_open":
            status_tag = "  [EXIT only — no logged entry]"
        lines.append(
            f"  - {ts}  {action:>5}  {ticker} {strike_str}{ct_suffix}  "
            f"exp {exp_short}  {gain_str}{price_str}  {mode_tag}{status_tag}"
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
    points_block = _format_points_block(user_id)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        user_id=user_id,
        msg_count=len(messages),
        messages_block=msgs_block,
        analyst_trades_block=analyst_trades_block,
        points_block=points_block,
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
        str | None,  # profile_text
        int | None,  # chatter_base (clipped 0-50)
        str | None,  # trader_rationale
        int | None,  # racial_humor_score
        str | None,  # racism_rationale
    ]:
        """Parse the JSON response. Returns the five fields or all-None
        on parse failure. Logs first 300 chars of the response on
        decode error so we can see what came back.

        Caller responsibility: compute final trader_score as
        scaled_chatter + receipt_points (no upper cap), where
        scaled_chatter = clip(chatter_base, 50) * min(1, msgs/500)
        and receipt_points is from db.compute_member_points(). The
        activity multiplier discounts chatter for thin-history
        users so they can't out-rank active ones on a 1-paragraph
        Gemini read. Receipts are NOT scaled — they're objective
        ledger signal that the activity floor doesn't apply to.
        Honesty modifier was removed — chatter brackets bake in
        self-awareness / gloat-loud-complain-vague-asymmetry /
        tilt-pattern signals directly, no separate ±10 adjustment.
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
            cb = data.get("chatter_base")
            tr = (data.get("trader_rationale") or "").strip() or None
            rh = data.get("racial_humor_score")
            rcr = (data.get("racism_rationale") or "").strip() or None
            # Backwards-compat for prompt-cache rollout: if Gemini emits
            # the legacy trader_score field or the legacy honesty_modifier
            # field (cached prompt instructing the old format), absorb
            # into chatter_base via simple clamping. trader_score legacy
            # → clamp to 0-50; honesty_modifier legacy → add to chatter
            # then clamp.
            if cb is None and "trader_score" in data:
                legacy = data.get("trader_score")
                try:
                    cb = max(0, min(50, int(legacy))) if legacy is not None else None
                except (TypeError, ValueError):
                    cb = None
            legacy_hm = data.get("honesty_modifier")
            if cb is not None and legacy_hm is not None:
                try:
                    cb = max(0, min(50, int(cb) + int(legacy_hm)))
                except (TypeError, ValueError):
                    pass
            if cb is not None:
                try:
                    cb = max(0, min(50, int(cb)))
                except (TypeError, ValueError):
                    cb = None
            if rh is not None:
                try:
                    rh = max(0, min(100, int(rh)))
                except (TypeError, ValueError):
                    rh = None
            return pt, cb, tr, rh, rcr
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
            # chatter_base replaces the old trader_score field. Gemini
            # outputs only its qualitative chat-pattern judgment
            # (0-50); Python computes the final trader_score as
            # min(50, chatter_base) + receipt_points  # no upper cap
            # downstream. The honesty modifier (a separate ±10 field
            # in a prior schema) was folded into the chatter bracket
            # placement — bag-holder / average / sharp brackets bake
            # in self-awareness, gloat patterns, and tilt cycles
            # directly. This eliminates the receipt hallucination
            # class — Gemini physically cannot output the receipt
            # component because it's not in the schema.
            "chatter_base": types.Schema(type=types.Type.INTEGER),
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
            "chatter_base",
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
            str | None, int | None,
            str | None, int | None, str | None,
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
            # FORCE-mode floor: 30 msgs (matches what we promise in the
            # message + the docstring). Default-mode floor stays at
            # min_msgs (100 by default) so cold-start gating still applies
            # to fresh users with thin history. Users who ALREADY have a
            # profile bypass both floors in force mode — they're already
            # in the ranking, so re-rank them regardless of msg count
            # (otherwise low-activity users locked in old scores forever).
            FORCE_LIFETIME_FLOOR = 30
            if force:
                print(
                    f"FORCE mode: bypassing delta_threshold — every user "
                    f"above the {FORCE_LIFETIME_FLOOR}-msg lifetime floor "
                    f"(or with an existing profile) will be re-profiled.",
                    flush=True,
                )
            for uid, msgs in by_user.items():
                has_existing_profile = uid in existing_profiles
                if force:
                    # In force mode: existing profile bypasses the floor
                    # entirely; new users still need >= FORCE_LIFETIME_FLOOR.
                    floor = 0 if has_existing_profile else FORCE_LIFETIME_FLOOR
                else:
                    floor = min_msgs
                if len(msgs) < floor:
                    skipped_lurkers += 1
                    continue
                if not force:
                    last_seen = (
                        existing_profiles.get(uid) or {}
                    ).get("last_seen_message_at")
                    if has_existing_profile and last_seen:
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
            if force:
                eligibility_label = (
                    f"existing profiles re-ranked + new users >= "
                    f"{FORCE_LIFETIME_FLOOR} msgs"
                )
            else:
                eligibility_label = (
                    f">= {min_msgs} msgs, >= {delta_threshold} new since "
                    f"last profile"
                )
            print(
                f"\n{len(eligible)} users eligible ({eligibility_label})",
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
                        profile, chatter_base, trader_rationale, racial_humor_score, racism_rationale = result
                        # Python-side deterministic final-score
                        # computation. Gemini outputs only the chatter
                        # judgment (0-50); the receipt component comes
                        # from the structured 14-day ledger (verified
                        # impossible-to-hallucinate). Honesty modifier
                        # was removed — the chatter brackets bake in
                        # self-awareness / gloat patterns / tilt cycles
                        # directly via bracket placement.
                        #
                        # Activity penalty (added 2026-05-28): chatter
                        # scales linearly until the credibility
                        # threshold so a 45-msg user can't out-rank a
                        # 1467-msg user on a thin Gemini read. Receipts
                        # stay un-scaled — they're objective trade
                        # signal that the activity floor doesn't apply
                        # to. Final score = scaled_chatter +  # no upper cap
                        # receipt_pts) with scaled_chatter =
                        # clip(chatter_base, 50) * min(1, msgs/500).
                        if chatter_base is not None:
                            try:
                                _ledger = db.compute_member_points(uid, days=21)
                                _receipt_pts = int(_ledger.get("points") or 0)
                            except Exception:
                                _receipt_pts = 0
                            _clipped_chatter = max(0, min(50, int(chatter_base)))
                            _activity_mult = min(
                                1.0,
                                len(msgs) / TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS,
                            )
                            _scaled_chatter = round(_clipped_chatter * _activity_mult)
                            # 2026-06-02 policy: no upper bound on trader_score.
                            # Removed min(100, ...) so top traders separate from
                            # each other rather than all pinning to 100. Scaled
                            # chatter caps at 50; receipts (wins * 2 per the
                            # ledger overhaul) are unbounded above.
                            trader_score = max(0, _scaled_chatter + _receipt_pts)
                        else:
                            trader_score = None
                        # Activity multiplier on racism too (added
                        # 2026-05-29): same 500-msg credibility floor.
                        # A user with 45 msgs and a 95 racial_humor
                        # bracket placement can't certify the pattern
                        # over volume; scale it down so the racism
                        # rank reflects sustained behavior, not a
                        # single sharp sample. The raw bracket call
                        # from Gemini stays valid as the read on the
                        # chat that IS visible; Python applies the
                        # credibility factor downstream. Same as
                        # trader_score's chatter-base scaling.
                        if racial_humor_score is not None:
                            _humor_mult = min(
                                1.0,
                                len(msgs) / TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS,
                            )
                            racial_humor_score = max(
                                0,
                                min(
                                    100,
                                    round(int(racial_humor_score) * _humor_mult),
                                ),
                            )
                        # Guard: don't overwrite a substantial prior
                        # profile with a much shorter truncated one.
                        # Heavy users sometimes get section-1-only
                        # responses even after retries; we'd rather
                        # KEEP the prior long profile than downgrade
                        # to a partial.
                        #
                        # BUT: this guard only makes sense for users
                        # with enough chat to PRODUCE a long profile.
                        # Thin-history users (< 500 msgs, the activity
                        # full-credit threshold) can't write 2000-char
                        # profiles because there isn't enough chat to
                        # describe — their correct output IS short.
                        # The guard was permanently locking them at
                        # whatever score the OLD (pre-activity-ramp)
                        # Gemini grading produced, which is exactly
                        # the Tied / KsFs / Soysoo / Quackers stuck-
                        # at-#3 bug. Skip the guard for thin users so
                        # the activity ramp can actually reach them.
                        prior = existing_profiles.get(uid)
                        prior_len = len((prior or {}).get("profile_text") or "")
                        new_len = len(profile or "")
                        thin_user = len(msgs) < TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS
                        if (
                            profile
                            and not thin_user
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
                            # Thin-user rescore fallback. When Gemini
                            # returns empty (often the case for thin-
                            # history users — the model can't produce
                            # a meaningful structured profile off 18
                            # messages and the JSON-schema validator
                            # gives back nothing), don't just `continue`
                            # past — that locks the user at whatever
                            # trader_score they had pre-fix forever
                            # (Tied at #4 with score 55 lived on this
                            # branch for 9 days). Instead, partial-
                            # update just the trader_score and racism
                            # score using the new activity multiplier
                            # against their prior chatter estimate.
                            # profile_text + rationales stay untouched
                            # (upsert COALESCEs Nones). Tied at 18
                            # msgs with prior trader_score 55 (mostly
                            # chatter) gets rescored to 2 cleanly.
                            prior = existing_profiles.get(uid) or {}
                            prior_trader = prior.get("trader_score")
                            prior_humor = prior.get("racial_humor_score")
                            thin_user = len(msgs) < TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS
                            if thin_user and (
                                prior_trader is not None
                                or prior_humor is not None
                            ):
                                try:
                                    _ledger_fb = db.compute_member_points(uid, days=21)
                                    _receipt_fb = int(_ledger_fb.get("points") or 0)
                                except Exception:
                                    _receipt_fb = 0
                                _mult_fb = min(
                                    1.0,
                                    len(msgs) / TRADER_SCORE_ACTIVITY_FULL_CREDIT_MSGS,
                                )
                                # Estimate prior chatter from prior
                                # trader_score - prior_receipts. The
                                # prior_receipts isn't stored, but for
                                # thin users it's usually small/zero;
                                # the multiplier is small enough that
                                # a few-point over-estimate doesn't
                                # move the result much.
                                if prior_trader is not None:
                                    prior_chatter = max(0, min(50, int(prior_trader)))
                                    rescored_trader = max(
                                        0,
                                        min(
                                            100,
                                            round(prior_chatter * _mult_fb) + _receipt_fb,
                                        ),
                                    )
                                else:
                                    rescored_trader = None
                                if prior_humor is not None:
                                    rescored_humor = max(
                                        0,
                                        min(
                                            100,
                                            round(int(prior_humor) * _mult_fb),
                                        ),
                                    )
                                else:
                                    rescored_humor = None
                                try:
                                    # Pass the EXISTING profile_text
                                    # so the upsert's NOT-NULL on that
                                    # column stays satisfied. Other
                                    # fields stay COALESCE'd via Nones
                                    # → prior values preserved.
                                    db.upsert_user_profile(
                                        user_id=uid,
                                        username=meta.get("username"),
                                        display_name=meta.get("display_name"),
                                        profile_text=(prior.get("profile_text") or ""),
                                        message_count_at_update=len(msgs),
                                        last_seen_message_at=(
                                            msgs[-1]["timestamp"] if msgs else None
                                        ),
                                        trader_score=rescored_trader,
                                        racial_humor_score=rescored_humor,
                                    )
                                    print(
                                        f"    rescored {meta['display_name']} via fallback: "
                                        f"trader {prior_trader}→{rescored_trader}, "
                                        f"humor {prior_humor}→{rescored_humor}",
                                        flush=True,
                                    )
                                    n_failure_empty -= 1  # rescore counts as recovery
                                except Exception as fb_err:
                                    print(
                                        f"    fallback rescore write failed: {fb_err}",
                                        flush=True,
                                    )
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

            # Dormant-user sweep. Anyone with an existing profile who
            # didn't show up in by_user (zero messages in the window)
            # is invisible to the main refresh loop — they never get
            # iterated, never get the activity-ramp treatment. Their
            # scores stay frozen at whatever they had when last active.
            # KsFs lived in this bug for 9 days, sitting at score 45
            # while not having posted in over a month. Same hide
            # behavior for dozens of other dormant ex-active users.
            #
            # Fix: rescore every dormant profile with msgs=0 in window.
            # Activity multiplier = 0, so trader_score = receipts only.
            # No Gemini call needed — just arithmetic on stored fields.
            # Profile_text stays untouched (upsert COALESCEs Nones).
            # Fetch ALL existing profiles, not just the ones in
            # by_user. The `existing_profiles` dict above was built
            # via get_profiles_for_users(by_user.keys()) and only
            # contains profiles for users with messages in the
            # window — which is exactly the set we need to EXCLUDE
            # from the dormant sweep. We need the COMPLEMENT.
            n_dormant = 0
            seen_uids = set(by_user.keys())
            try:
                all_profile_rows = db.get_connection().execute(
                    "SELECT user_id, username, display_name, "
                    "       profile_text, trader_score, "
                    "       racial_humor_score, last_seen_message_at "
                    "  FROM user_profiles"
                ).fetchall()
                all_existing_profiles = {
                    int(r["user_id"]): dict(r) for r in all_profile_rows
                }
            except Exception as e:
                print(f"Dormant sweep: profile fetch failed: {e}", flush=True)
                all_existing_profiles = {}
            for uid, prior in all_existing_profiles.items():
                if uid in seen_uids:
                    continue
                # Skip users we just couldn't reach (no username = data
                # corruption / orphan row).
                username = prior.get("username")
                display_name = prior.get("display_name")
                if not username:
                    continue
                try:
                    _ledger_d = db.compute_member_points(uid, days=21)
                    _receipt_d = int(_ledger_d.get("points") or 0)
                except Exception:
                    _receipt_d = 0
                # msgs=0 → multiplier=0 → chatter contribution = 0.
                # New score = receipts only. Dormant users with no
                # receipts drop to 0; dormant users with stale receipts
                # (caller closes still in 14d window) keep their
                # receipt component.
                prior_trader = prior.get("trader_score")
                prior_humor = prior.get("racial_humor_score")
                # 2026-06-02 policy: no upper cap on trader_score. Dormant
                # rescore preserves whatever receipts remain in window.
                rescored_trader = (
                    max(0, _receipt_d)
                    if prior_trader is not None else None
                )
                rescored_humor = (
                    0 if prior_humor is not None else None
                )
                # last_seen stays as the prior value — they haven't
                # posted, so no new last_seen to record. Use prior
                # last_seen_message_at to keep the row coherent.
                last_seen = prior.get("last_seen_message_at")
                try:
                    db.upsert_user_profile(
                        user_id=uid,
                        username=username,
                        display_name=display_name,
                        profile_text=(prior.get("profile_text") or ""),
                        message_count_at_update=0,
                        last_seen_message_at=last_seen,
                        trader_score=rescored_trader,
                        racial_humor_score=rescored_humor,
                    )
                    n_dormant += 1
                except Exception as d_err:
                    print(
                        f"    dormant rescore failed for "
                        f"{display_name or username}: {d_err}",
                        flush=True,
                    )
            if n_dormant:
                print(
                    f"\nDormant sweep: rescored {n_dormant} users with "
                    f"zero messages in the {days}-day window "
                    f"(score → receipts-only).",
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
