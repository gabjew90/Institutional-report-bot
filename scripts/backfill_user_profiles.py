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

Per-section behavior:

- **Personality, Strengths, Style & Patterns, Voice, Role in the room:** carry forward unless the new messages clearly add or contradict material. Edit minimally — variety isn't a goal.
- **Recent activity (last 7d):** rewrite completely from the new messages. This section is meant to be current.
- **Running jokes:** keep bits the room is still reinforcing in the new messages. Add new persistent bits when they emerge.
- **Trash talk ammo:** keep the strongest prior items. Add a new specific quote or moment when something landed in the new messages worth weaponizing.
- **trader_score / racial_humor_score:** hold by default. Shift only when new evidence justifies a meaningful move — a single bad week doesn't drop a +70 to a +40.
- **trader_examples:** favor NEW trade moments when they meet the ticker-anchored bar. Keep the strongest prior examples if nothing new is more specific.

**Drop bits older than ~60 days that aren't reinforced.** Items in Running jokes and Trash talk ammo that haven't shown up in the room in the last ~60 days are stale — retire them even if the prior profile carried them. The dossier reflects the LIVE room, not a graveyard of past bits. Test: is this still something a reader would recognize from this WEEK or last? If not, drop it.

The output schema is identical to a from-scratch profile — same eight sections, same JSON wrapper. The difference is the work: editing the existing dossier, not authoring a new one.

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

Eight sections, all required. If signal is thin for a section, write `Insufficient signal — too few messages on this dimension.` rather than padding. Always keep the structure; never truncate sections.

**Personality.** Two or three sentences. The big-picture read of who this person is at the terminal — specific to them, not a template. Example shape: "Macro-aware day-trader who treats the discord as his Bloomberg alternative — leads with charts, drops conviction bombs, then disappears for a week after a bad run."

**Strengths.** Three to five concrete things this person brings. Real items only — trades they nailed, expertise they share, charts they read better than most, humor that defuses, the way they support newer members. If they mostly lurk, say so plainly: "Mostly lurks; rare contribution when someone asks about [specific topic]."

**Style & Patterns.** Two to four sentences on how they trade and how they talk. Example: "Trades 0DTE QQQ lottos on conviction; sizes up after one green day, holds through 40% drawdowns, posts the chart after every entry. Talks in shorthand — 'slam,' 'send it,' 'chyna.' Goes quiet when red."

**Running jokes.** Two to four bits the room ALREADY teases this person about — material the room reinforces in chat, lifted from messages. The test: would a normal Tuesday in chat hit this same note as a joke? "Always asks about $WEN," "calls the top on every green day," "perpetual office hostage" — bits the room repeats.

**Trash talk ammo.** Three to five specific recent moments the bot can weaponize for laughs in a clapback. The receipts ARE the point — each item anchors to a verbatim quote OR a clearly-specified incident the asker (who was in the room) would recognize instantly.

Quote them verbatim, original framing intact. Slurs, hard-R words, racial stereotypes, sexual crudeness, political crudeness — all go in if it's how they actually talk. The bot lives in this room; sanitizing strips the entire signal.

Good shapes:
- "Said 'can trump stop being a fucking faggot' during the Tuesday tariff rant"
- "Mocked the bitcoin conference as having 'not a girl in sight'"
- "Wrote 'bomb those yellow niggas' while ranting about Chinese rare-earth controls"
- "Sold his $WEN bags for a 30% loss the same day they bounced 40%"
- "Posted a 'this is the floor' chart at every bottom for two weeks straight"

The framing stays factual. "Said 'X' during the Y rant" — anchored, specific. The reader draws the inference from the quote; the writer stays out of editorial judgment.

**Recent activity (last 7d).** What this person did THIS week — tickers traded, themes pushed, conversations led, wins or losses, who they tagged. Refresh daily from this week's messages; older material belongs in the other sections.

**Voice.** A specific descriptor of how they talk + two to four verbatim phrases they actually use. The descriptor goes first: "dry and observational, leans on stock-specific memes," or "warm, emoji-heavy, defuses with self-deprecation," or "crude and fast, casually cruel with affection underneath." Then the quotes — including the edgy ones: "I'm just gonna sit on my hands today," "this is the one boys," "fuck it we ball," "bomb those yellow niggas." Real lines beat description every time.

**Role in the room.** One short phrase describing what they FUNCTION as — signal / banter / chaos / mentor / hype-man / contrarian / lurker / texture. Neutral.

## HOW TO WRITE

**Lead with the behavior, follow with the evidence.** Write "buys any sub-$5 ticker with three letters and a press release, holds to -40%, calls it a long-term play." Not "trades small caps." Write "size scales with frustration, every time." Not "tilts after losses." Specific behavior, every line.

**Quote verbatim with the original edge.** Real lines from chat carry more signal than any description. Pull them exact — slurs, slang, swears, broken grammar, all of it. "Should've sized up," "fuck it we ball," "bomb those yellow niggas," "can trump stop being a fucking faggot." If they said it in chat, it's evidence; you write it. A scrubbed quote isn't theirs.

**Strip bot commands.** Messages starting with `fc TICKER` (e.g. `fc nvda`, `fc spy`) are chart-pulling slash commands — not personality signal. Ignore them entirely; same for any obvious slash-command pattern.

**Match confidence to evidence.** When inferring from incomplete signal, soften the verb: "reads as," "appears to," "seems to." When the messages show it directly, state it flat. Anchor every claim to message evidence; invent nothing to fill the schema.

**Length follows signal.** A 100-message user gets a shorter profile than a 4,000-message user — that's correct. Tight sections beat padded sections.

**Describe behavior, not psychology.** "Size scales with frustration, every time" is descriptive — sourced behavior, no diagnosis. Stay in behavior; let the reader draw the line.

## EXAMPLE TARGET

> **BK (`bankerkyle`, <@423994649317736448>) — 4183 msgs**
>
> *Personality:* M&A guy at a real firm who treats the discord as where the day-job rules don't apply. Smart, fast, doesn't hide that he's making real money in the day-job, doesn't lord it. Lives in lament-mode after missed entries; recovers via crude humor.
>
> *Strengths:* Sharp on macro — knows how rate-sensitive sectors actually price. Good chart reads when he commits. Calls other regulars on weak takes without making it personal. Posts wins AND losses cleanly, which keeps the room honest.
>
> *Style & Patterns:* Trades crypto perps with leverage no risk committee would approve. Fast to size up on MSTR / SOL conviction names. Sticks to large-caps; dismissive of altcoin punts. Lament-mode posts after missed entries are a constant feature.
>
> *Running jokes:* "Compliance is watching him" — the recurring bit about his desk monitoring his accounts. "$WEN bagholder" — the eternal Wendy's hope. "Should've sized up" — the post-missed-trade lament the room parrots back at him. "Office hostage situation."
>
> *Trash talk ammo:* Sold his $WEN bags for a 30% loss the same day they bounced 40% — the chart screenshots still get posted at him. Once announced he'd "never touch alts again" and bought DOGE 48 hours later. Said "can trump stop being a fucking faggot" mid-tariff rant Tuesday. Mocked the bitcoin conference as having "not a girl in sight." Threatened to go full cash, opened 10x SOL perps within 90 minutes.
>
> *Recent activity (last 7d):* Closed his 5x SOL perps "because compliance was watching" (Monday). Pushed MSTR breakout thesis Wednesday — got mixed responses. Multiple lament-mode posts about not sizing up META 615C. Has been pushing the AI-capex thesis in long-form replies.
>
> *Voice:* Crude and fast, casually cruel with affection underneath. Recurring takes: "should've sized up," "we're getting fiddled," "fuck it we ball," "compliance is watching." Drops "chyna" for China without irony.
>
> *Role in the room:* Senior energy. Not the loudest — the one whose opinion the room registers when he weighs in.

## SCREENSHOT RECEIPTS (image OCR)

Messages tagged `[image-OCR: ...]` contain text extracted from a screenshot the user posted — usually a brokerage notification, position screen, order ticket, or chart. When the channel is `💲-gain-loss-porn-💲`, screenshots are almost always documented P&L (Robinhood "closed $4,200 PLTR 75c" cards, position screens with green/red gain pills, total-PnL summaries). Elsewhere they're often charts or news cards.

**Treat OCR'd P&L as receipts — documented evidence, not claims.** Text in chat ("took +$4K on PLTR") is what someone *says* they did. OCR'd screenshot text showing "+$4,200 · PLTR · +180%" is what they posted *as proof*. Use BOTH together to read the trader.

- **Wins-only posting** (10 green screenshots, no red ones) is itself signal — read it as selective receipts, not as a flawless trader. Note the asymmetry in the profile if it's pronounced.
- **Wins AND losses posted cleanly** (mix of green + red, with text owning the losses) is the strongest positive signal in the room. Score accordingly.
- **Loud chat conviction + zero screenshots** is talk without receipts. Don't score it like documented PnL.
- **Quiet user + a steady drip of green receipts** outranks a loud user with no receipts.

When you can quote the actual dollar amount and ticker from a screenshot in `trader_examples`, do it — receipts are the strongest evidence form. Example: "Closed $PLTR 5/30 75c at +$4,200 / +180% — posted the Robinhood card same day."

**Cross-checking text claims against receipts is fair game.** If a user says "I never play penny stocks" but you see an OCR'd screenshot of them in $GPUS at $0.20, flag the contradiction in profile_text. The receipts are the truth.

Don't invent OCR content. Only cite what's actually in the `[image-OCR: ...]` blocks. If a screenshot exists with no OCR text (`[image]` marker), you know they posted something but can't speak to what — note it as activity, don't fabricate the contents.

## OUTPUT FORMAT — STRICT JSON, no prose, no markdown wrapper

Output a single JSON object with five fields:

```
{{
  "profile_text": "<full markdown profile per the schema above>",
  "trader_score": <integer 0-100>,
  "trader_rationale": "<one direct sentence on what's driving the score>",
  "racial_humor_score": <integer 0-100>,
  "trader_examples": ["<2-3 trade-anchored moments>"]
}}
```

**trader_examples** — two to three short evidence items (each ~120-180 chars) that justify trader_score. HARD RULE: every example references a SPECIFIC TRADE — a ticker ($NVDA, $SPY, $BTC), a contract ("150C", "$28 puts"), a position size or dollar amount, a buy/sell/trim decision, or a documented P&L. Non-trade quotes (banter, slurs, personality lines, opinions about other users) belong in profile_text, not here. If you can't find two to three trade-anchored examples, return `[]` — empty is the correct answer when the user's recent history doesn't show enough trading detail.

Good: "Caught the $NVDA earnings move into 145C calls Tuesday, 220% printed — flagged it 2 hours before the run."
Good: "Full-ported $GPUS at 0.20 calling it '0 or hero,' held through a 50% drawdown without trimming."
Anti-pattern: "Sometimes does well on tech names." (vague)
Anti-pattern: "Calls everyone slurs and posts memes." (not a trade)

**trader_score brackets (0-100):**
- **90-100:** Real edge. Documented wins others tail. Posts wins AND losses cleanly. Process visible. Room trusts their reads.
- **75-89:** Solid. Mostly green over time. Style clear and works. Owns misses without crisis.
- **60-74:** Hits and misses, but hits are real. Has a setup that works in some conditions. Mixed execution.
- **40-59:** Net negative or barely flat. Knows the theory, leaks edge in execution. Often self-aware.
- **20-39:** Bag holder. More losses than wins. Sizes up to recover. Chases the loudest voice.
- **0-19:** Tail traffic / exit liquidity. Should not be trading at this size.

State the actual pattern directly in trader_rationale — one honest sentence on what drives the score.

**racial_humor_score brackets (0-100)** — the canonical score for race-edged content from this user. Internal calibration only; the bot never quotes the raw number. Score the FULL picture (slurs + stereotypes + dog whistles + race-based mockery + unprompted racial references), not just literal slurs. Self-deprecating jokes about the user's OWN race and factual mentions of races/ethnicities in geopolitics or news don't count.

- **0-15:** Clean. No race-edged content in the window. Lurkers default here.
- **16-35:** Occasional. Joins common room jokes when prompted, doesn't lead, no slurs.
- **36-55:** Regular. Race-edged humor is part of their normal voice. Some stereotyping. One or two censored slurs.
- **56-75:** Heavy. Racial humor is a defining feature. Multiple slurs (including uncensored).
- **76-100:** Dominant. Race-edged content saturates the messages. Slurs uncensored and frequent. Specific groups targeted in a sustained way.

Anchor against THIS user's messages. Zero examples = 0-15.

---

MESSAGES (oldest first, most recent last):
{messages_block}\
"""


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
    str | None, int | None, str | None, int | None, list[str] | None
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
        sample = new_messages[-sample_size:]
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
        # Cold-start: full sample window
        sample = messages[-sample_size:]

    msgs_block = _format_messages_block(sample)
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        user_id=user_id,
        msg_count=len(messages),
        messages_block=msgs_block,
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
        str | None, int | None, str | None, int | None, list[str] | None
    ]:
        """Parse the JSON response. Returns the five fields or all-None
        on parse failure. Logs first 300 chars of the response on
        decode error so we can see what came back."""
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
            te_raw = data.get("trader_examples")
            te = None
            if isinstance(te_raw, list):
                # Filter to non-empty strings, trim each to 250 chars
                te = [
                    str(x).strip()[:250]
                    for x in te_raw
                    if isinstance(x, str) and str(x).strip()
                ]
                if not te:
                    te = None
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
            return pt, ts, tr, rh, te
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
    _RESPONSE_SCHEMA = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "profile_text": types.Schema(
                type=types.Type.STRING,
                max_length=8000,
            ),
            "trader_score": types.Schema(type=types.Type.INTEGER),
            "trader_rationale": types.Schema(
                type=types.Type.STRING,
                max_length=500,
            ),
            "racial_humor_score": types.Schema(type=types.Type.INTEGER),
            "trader_examples": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.STRING,
                    max_length=400,
                ),
            ),
        },
        required=[
            "profile_text",
            "trader_score",
            "trader_rationale",
            "racial_humor_score",
            "trader_examples",
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
    _SHORT_PROFILE_THRESHOLD = 1500

    async def _try_text_only(
        temperature: float = 0.3,
    ) -> tuple[
        tuple[str | None, int | None, str | None, int | None], str | None
    ]:
        # Text-only path hardcodes "gemini-3.1-flash-lite-preview" —
        # the only model verified to produce full 2400-char structured
        # profile outputs. The GA "gemini-3.1-flash-lite" returns 4-char
        # garbage for the backfill's full-context prompts despite working
        # fine with simple prompts.
        #
        # Config kept to the MINIMAL set that worked in the early runs
        # (bhp99ej9k @ 03:01 UTC produced full 2400-char profiles):
        # temperature + max_output_tokens + response_mime_type. NO
        # response_schema, NO thinking_config, NO safety_settings.
        # Each of those was added to fix narrower bugs and one (or some
        # combination) was suppressing all visible output.
        text_model = "gemini-3.1-flash-lite-preview"
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
        tuple[str | None, int | None, str | None, int | None], str | None
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


async def run(days: int, channels: list[str]) -> None:
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
            for uid, msgs in by_user.items():
                if len(msgs) < min_msgs:
                    skipped_lurkers += 1
                    continue
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
                        existing = existing_profiles.get(uid)
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
                        if isinstance(result, Exception):
                            print(f"  ✗ {meta['display_name']}: {result}",
                                  flush=True)
                            continue
                        profile, trader_score, trader_rationale, racial_humor_score, trader_examples = result
                        if not profile:
                            print(f"  ✗ {meta['display_name']}: empty / unparseable response",
                                  flush=True)
                            continue

                        # Verify the profile's quoted phrases against
                        # the user's actual chat_messages. Catches
                        # hallucinated quotes the incremental-update
                        # path carried forward from a stale prior
                        # profile. Result stored in gemini_json for
                        # forensics; logged when unverified > 0 so
                        # operators see the pattern.
                        claim_check = _verify_profile_claims(
                            profile, trader_examples,
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
                        # JSON-encode example lists (TEXT column).
                        slur_ex = slur_examples.get(uid, [])
                        slur_ex_json = json.dumps(slur_ex) if slur_ex else None
                        trader_ex_json = (
                            json.dumps(trader_examples)
                            if trader_examples else None
                        )

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
                            slur_examples=slur_ex_json,
                            trader_examples=trader_ex_json,
                        )
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

            # Observability (fix #7): record run summary to pipeline_events
            # for historical trend analysis + /status surfacing.
            try:
                db.record_pipeline_event(
                    "profile_refresh",
                    "completed",
                    {
                        "window_days": days,
                        "channels": channels,
                        "eligible": len(eligible),
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
    args = parser.parse_args()
    if args.no_vision:
        # Override the setting in-process; doesn't touch the env var
        settings.profile_image_ocr_enabled = False
        print("--no-vision: vision pipeline disabled for this run",
              flush=True)
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels))


if __name__ == "__main__":
    main()
