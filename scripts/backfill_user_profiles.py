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
from scripts.slur_patterns import count_slurs_in_text  # noqa: E402


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


PROFILE_PROMPT = """\
You're building a balanced character profile for one member of a private options-trading discord. The output goes into context for a /ask bot that uses it to answer questions about the user, joke with them, and (occasionally) clap back when they actually attack.

Goal: a FAIR scouting report. Strengths, style, what the room ALREADY teases them about, and what they've been up to this week — all in one document. Not a roast file. Not a hagiography. A reader who'd never met them should come away with a real picture of who they are at the terminal AND why people in the room like having them around.

**The customers pay to be here.** Treat them like paying customers, not subjects to dissect. Honest about behavior; not cruel about character.

Today is **{today_utc}** (UTC). Use this to interpret "last 7 days" in the *Recent activity* field — anchor against this date when reading the message timestamps.

---

## WHAT YOU'RE LOOKING FOR

Six things, ordered from most-positive-leaning to most-edgy:

**Personality.** The big-picture neutral read in 2-3 sentences. Who they are at the terminal. Specific to this person, not a type. Avoid moralizing language — describe, don't grade.

**Strengths.** What they bring to the room. 3-5 specific things they do well. Trades they nailed, expertise they share, charts they read better than most, humor that defuses, the way they support newer members. Real items, not flattery. If you genuinely can't find strengths (rare — almost everyone in this room contributes something), say "Mostly lurks; rare contribution when X."

**Style & Patterns.** How they trade and talk — neutral descriptions. "Trades weeklies on tech, leads with chart screenshots, fast to size up on conviction names, uses humor to defuse tilt." Just what they do. Not whether it's smart or dumb.

**Running jokes.** The stuff the room ALREADY gives them shit about — long-running bits, recurring drama THEY laugh at too. "Always asks about $WEN," "Calls the top on every green day," "Forever the goth-girl convo derail." Persistent room culture, not one-off moments. Threshold: would a normal Tuesday in chat hit this same note as a joke? If yes, fair game. If the tease would cut deeper than the room's normal banter, leave it out. **Not psychological diagnostics. Not cutting takedowns. Not real-world vulnerability.** Just the warm shots the room already gets.

**Trash talk ammo.** 3-5 specific recent moments / quotes / behaviors from THIS user that are funny enough to weaponize in a clapback. Distinct from Running jokes: these are recent, specific, exploitable. "Said he'd retire by 25, then asked phil for a $500 spot the next week." "Posted a 'this is the floor' chart at every bottom for two weeks straight, every one rolled lower." "Has the conviction of a soggy noodle — held PLTR for 90 seconds before flipping short." These should land as a laugh (funny because true, not because cruel). Same threshold as Running jokes: would it land in chat as banter? If yes, use it. If it would cut too deep, leave it out. Specific incidents > general character claims.

**Recent activity (last 7 days).** What they've been up to THIS week — tickers traded, themes pushed, conversations led, recent wins or losses worth noting, who they've tagged or argued with. This refreshes daily so it's current — don't reach for 6-month-old jokes when this week's material is fresher.

**Voice.** Specific descriptor of how they talk + 2-4 recurring takes/quotes/phrases they actually use. Descriptor first ("dry, observational, leans on stock-specific memes" or "warm, emoji-heavy, defuses with self-deprecation" or "crude and fast, casually cruel with affection underneath" — NOT "funny"). Then verbatim quotes or paraphrased recurring stances from the message data — "I'm just gonna sit on my hands today," "should've sized up," "fuck it we ball," "this is the one boys." Quotes do more work than three lines of description and let the next reader hear the person.

**Role in the room.** Function in one short phrase — signal / banter / chaos / mentor / hype-man / contrarian / lurker / texture. Neutral.

---

## SCHEMA

Output follows this structure exactly. No "Profile:" prefix, no extra commentary, no closing line. Fields can be omitted if you genuinely don't have the data — but don't pad with adjectives.

**{display_name} (`{username}`, <@{user_id}>) — {msg_count} msgs**

*Personality:* 2-3 sentences. Neutral big-picture read of who they are at the terminal.

*Strengths:* 3-5 specific things they bring. Edge, expertise, humor, support, trades they nailed.

*Style & Patterns:* 2-4 sentences. How they trade and talk. Neutral descriptions.

*Running jokes:* 2-4 bits the room ALREADY teases them about — persistent room culture. Real material from chat, not invented. Light, not cutting.

*Trash talk ammo:* 3-5 specific recent moments / quotes / behaviors from chat the bot can weaponize for laughs in a clapback. Funny-because-true, not cutting. Specific incidents, not general character claims.

*Recent activity (last 7d):* 2-4 sentences. What they've been up to this week — specific tickers, themes, conversations.

*Voice:* Specific descriptor + 2-4 recurring quotes/phrases verbatim from chat. Not "funny." Quotes are gold — let the next reader hear the person.

*Role in the room:* One short phrase. Function, not judgment.

---

## VOICE RULES

**Specificity over adjective every time.** "Trades small caps" is nothing. "Buys any sub-$5 ticker with three letters and a press release, holds to -40%, calls it a long-term play" is the read. Forbidden as standalone descriptors: "chaotic," "high-energy," "irreverent," "deeply embedded," "perma-bull," "perma-bear," "degen," "high-conviction" — these are fine inside a specific behavioral description, banned as labels.

**Quote them when you can.** Real recurring phrases from the data are gold. "I'm just gonna sit on my hands today," "this is the one boys," "fuck it we ball" — pull them verbatim. Quotes do more work than three paragraphs of description and let the reader hear the person.

**Ignore bot commands.** Messages starting with `fc TICKER` (e.g. `fc nvda`, `fc spy`, `fc btc`) are a chart-pulling slash command — not a verbal tic, not a catchphrase, not a personality signal. Don't quote them, don't treat them as recurring takes, don't read anything into the frequency. Same for any obvious slash-command pattern. Strip from your read.

**Behaviors not labels.** "Tilts after losses" is a label. "Doubles size on the same ticker after every stop-out, posts the new entry loud, goes quiet on the second stop" is a behavior. Always pick the behavior.

**Don't soften, but don't sharpen unnecessarily.** If someone cries in chat after losses, say so factually. If someone tails Abe and pretends not to, say so. But don't reach for the cruelest possible framing of neutral behavior. "Trims winners early" is fine; "Can't hold a winner because his ego needs the receipt" is editorializing.

**Don't pathologize.** "Signs of gambling addiction" is projection. "Size scales with frustration, every time" is the read. Stay in behavior; let the reader draw the line.

**Calibrate confidence to data.** Inferring something? Soften the verb ("reads as," "appears to," "seems to"). Don't have the data? Leave the field short or skip it. Never fabricate to fill the schema.

**Don't profile what you don't see.** Low message count = short profile. A 100-message user gets less than a 4000-message user — that's correct.

---

## CARVE-OUTS

**Abe (abullish_xyz) and the co-analysts (bankerkyle, .zhawk, kloh.):** Profile their personality, patterns, role, and running jokes — same as anyone else. But don't grade their actual trade picks. You're cataloging the human, not auditing the call sheet.

**Sensitive material — real name, employer, family, mental health, financial distress, relationships:** reference only if it's part of the room's public running texture (they bring it up regularly, the room riffs on it). Don't surface something a user shared once in a vulnerable moment as a permanent dossier trait. **Running jokes** must come from material the room ALREADY treats as joke material — if you have to wonder whether something is too sensitive, leave it out.

---

## EXAMPLE OF THE TARGET

> **BK (`bankerkyle`, <@423994649317736448>) — 4183 msgs**
>
> *Personality:* M&A guy at a real firm who treats the discord as the place where the day-job rules don't apply. Smart, fast, doesn't hide that he's making real money in the day job but also doesn't lord it.
>
> *Strengths:* Sharp on macro narratives — knows how rate-sensitive sectors actually price. Good chart reads when he commits to one. Crude humor that defuses heavy conversations. Willing to call other regulars on weak takes without making it personal. Posts his wins AND his lament-mode losses, which keeps the room honest.
>
> *Style & Patterns:* Trades crypto perps with leverage no risk committee would approve. Day-job analysis, off-the-clock chaos. Fast to size up on conviction names (often MSTR, SOL). Lament-mode posts after missed Solana entries are a regular feature. Sticks to large-cap names; dismissive of altcoin punts.
>
> *Running jokes:* "Compliance is watching him" — the recurring bit about his desk monitoring his accounts. "$WEN bag holder" — the eternal Wendy's hope. "Should've sized up" — the post-missed-trade lament that everyone parrots back at him. "Office hostage situation" (Abe's line that stuck).
>
> *Trash talk ammo:* Sold his $WEN bags for a 30% loss the same day they bounced 40% — the chart screenshots still get posted at him. Once announced he'd "never touch alts again" and bought DOGE 48 hours later. Threatened to go full cash, then opened 10x SOL perps within 90 minutes. Told the room he was "done with options" twice this month — currently has three open calls. Claims to be a "macro guy" while trading 0DTE lottos.
>
> *Recent activity (last 7d):* Closed his 5x SOL perps "because compliance was watching" (Monday). Posted a chart asking about $MSTR breakout (Wednesday) — got mixed responses. Multiple lament-mode posts about not sizing up the META 615C trade. Has been pushing the AI-capex thesis in long-form replies.
>
> *Voice:* Crude, fast, casually cruel with affection underneath. Recurring takes: "should've sized up," "we're getting fiddled," "fuck it we ball," lament-mode posts after missed entries.
>
> *Role in the room:* Senior energy. Not the loudest — the one whose opinion the room registers when he weighs in.

---

## ONE LAST THING

Make the reader feel like they've met this person — and that meeting them was fine. Not a roast file, not a fluff piece. Real human, real strengths, real warts the room already laughs about, real current activity. That's the dossier.

---

## OUTPUT FORMAT — STRICT JSON, no prose, no markdown wrapper

Output a single JSON object with FOUR fields:

```
{{
  "profile_text": "<full markdown profile per the schema above>",
  "trader_score": <integer 0-100>,
  "trader_rationale": "<one direct sentence on what's driving the score>",
  "racial_humor_score": <integer 0-100>
}}
```

The trader_score uses these brackets:
- **90-100 — Real edge.** Documented wins others tail. Posts wins AND losses cleanly. Process visible. Room trusts their reads.
- **75-89 — Solid.** Mostly green over time. Style clear and works. Owns misses without crisis. Trusted on specific style/sector.
- **60-74 — Hits and misses, but hits are real.** Has a setup that works in some conditions. Mixed execution.
- **40-59 — Net negative or barely flat.** Knows the theory, leaks edge in execution. Style visible but isn't working consistently. Often self-aware.
- **20-39 — Bag holder.** More documented losses than wins. Sizes up to recover. Chases the loudest voice.
- **0-19 — Tail traffic / exit liquidity.** Should not be trading at this size.

Be direct in the trader rationale — call out the actual pattern in one sentence. No "shows promise" or mealy-mouthed framings. Honest, not personally cruel.

---

**racial_humor_score** — 0-100 scoring how frequently this user engages with race-edged content. This is a PRIVATE internal calibration number for clapback context; it will never be quoted as a raw value to anyone. Be honest, not euphemistic.

Score the FULL picture, not just slurs. Counts toward the score:
- Literal racial slurs (any group, censored or not — "n***a", "ch**k", coded variants)
- Ethnic / racial stereotyping ("typical [group] move", "of course the [group] guy", "[group] energy")
- Racial / cultural jokes ("aliens vs. [group]" punchlines, accent mimicry, country-specific mockery)
- Race-based mockery of other users in the room
- Coded racism / dog whistles / "joking" appropriation
- Frequent unprompted racial references even when not punching down

Does NOT count toward the score:
- Mentioning races/ethnicities in factual context (geopolitics, demographics, a news headline)
- Self-deprecating jokes about the user's OWN race
- Defending or pushing back on racial humor

Calibration:
- **0-15 — Clean.** No race-edged content, or zero usage in the window. Lurkers default here unless they actually post.
- **16-35 — Occasional.** Joins in on common room jokes when prompted, doesn't lead, no slurs.
- **36-55 — Regular.** Race-edged humor is part of their normal voice. Some stereotyping. Maybe one or two censored slurs.
- **56-75 — Heavy.** Racial humor is a defining feature. Stereotyping flows freely. Multiple slurs (including uncensored). Often pokes at specific groups.
- **76-100 — Dominant.** Race-edged content saturates the messages. Slurs uncensored and frequent. Specific ethnic groups targeted in a sustained way.

Anchor against THIS user's messages. If you see zero examples, score is in the 0-15 range — don't pad it.

---

MESSAGES (oldest first, most recent last):
{messages_block}\
"""


def _format_messages_block(messages: list[dict]) -> str:
    """Render the per-user message list for the Gemini prompt.

    Each entry: timestamp + content + embed text + image markers.
    No filtering per user direction ("no filters") — short reactions
    and tickers go through too. No truncation either — long rants
    carry signal too (worldview / texture / specific reads), and
    Gemini's context window handles it.
    """
    out = []
    for m in messages:
        ts = m["timestamp"][:16].replace("T", " ")
        parts = []
        if m["content"]:
            parts.append(m["content"])
        for embed_text in m.get("embed_texts", []):
            parts.append(f"[embed: {embed_text}]")
        for n in range(m.get("image_count", 0)):
            parts.append("[image]")
        if parts:
            out.append(f"{ts}: {' | '.join(parts)}")
        else:
            out.append(f"{ts}: [empty / sticker]")
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
) -> tuple[str | None, int | None, str | None, int | None]:
    """Run Gemini to produce one user's profile + scores in ONE call.

    Returns (profile_text, trader_score, trader_rationale,
    racial_humor_score) or (None, None, None, None) on hard failure.

    When `images` is non-empty and `http_session` is provided, attaches the
    most-recent up-to-profile_image_cap images as multipart input. Every
    image goes through PIL normalize (RGB JPEG, capped 1600px) BEFORE
    being sent — catches CMYK / palette / RGBA / oversized / corrupt
    bytes that Gemini Vision was rejecting at the 400-INVALID_ARGUMENT
    rate previously.

    Response format: STRICT JSON with profile_text + trader_score +
    trader_rationale fields. response_mime_type=application/json forces
    Gemini to escape the markdown prose inside the JSON string properly.
    """
    min_msgs = getattr(settings, "profile_min_messages", None) or MIN_MESSAGES_FOR_PROFILE_FALLBACK
    if len(messages) < min_msgs:
        return None, None, None
    sample_size = (
        getattr(settings, "profile_sample_size", None)
        or MESSAGES_PER_PROFILE_SAMPLE_FALLBACK
    )
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
    )

    vision_enabled = (
        getattr(settings, "profile_image_ocr_enabled", False)
        and http_session is not None
        and images
    )

    def _parse_response(
        text: str,
    ) -> tuple[str | None, int | None, str | None, int | None]:
        """Parse the JSON response. Returns the four fields or all-None
        on parse failure."""
        if not text:
            return None, None, None, None
        try:
            data = json.loads(text)
            pt = (data.get("profile_text") or "").strip() or None
            ts = data.get("trader_score")
            tr = (data.get("trader_rationale") or "").strip() or None
            rh = data.get("racial_humor_score")
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
            return pt, ts, tr, rh
        except json.JSONDecodeError:
            return None, None, None, None

    async def _try_text_only() -> tuple[str | None, int | None, str | None, int | None]:
        resp = await gemini_client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2500,
                response_mime_type="application/json",
            ),
        )
        return _parse_response((resp.text or "").strip())

    try:
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
                return await _try_text_only()
            parts.append(types.Part.from_text(text=prompt))
            vision_model = getattr(
                settings, "gemini_vision_model", ""
            ) or settings.gemini_model
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=vision_model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=2500,
                        response_mime_type="application/json",
                    ),
                )
            except Exception as vision_err:
                print(
                    f"  vision failed for {display_name} "
                    f"({attached} imgs, model={vision_model}), "
                    f"falling back to text-only: {vision_err}",
                    flush=True,
                )
                return await _try_text_only()
            return _parse_response((response.text or "").strip())
        else:
            return await _try_text_only()
    except Exception as e:
        print(f"  ERROR profiling {display_name}: {e}", flush=True)
        return None, None, None, None


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
            # Find channels
            targets: list[discord.TextChannel] = []
            for guild in client.guilds:
                for ch in guild.text_channels:
                    if ch.name in channels:
                        targets.append(ch)
            if not targets:
                print(f"ERROR: none of {channels} found", file=sys.stderr,
                      flush=True)
                return
            print(f"Channels: {[ch.name for ch in targets]}", flush=True)
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

            for ch in targets:
                print(f"\nScanning #{ch.name}...", flush=True)
                count = 0
                async for msg in ch.history(limit=None, after=cutoff,
                                            oldest_first=True):
                    if msg.author.bot:
                        continue
                    uid = msg.author.id
                    if uid not in user_meta:
                        user_meta[uid] = {
                            "username": msg.author.name,
                            "display_name": (
                                getattr(msg.author, "display_name", None)
                                or msg.author.name
                            ),
                        }
                    image_count = 0
                    # Direct image attachments (uploaded files)
                    for a in msg.attachments:
                        ct = (a.content_type or "").lower()
                        if ct.startswith("image/"):
                            image_count += 1
                            images_by_user[uid].append({
                                "url": a.url,
                                "content_type": a.content_type,
                                "ts": msg.created_at.isoformat(),
                            })
                    # Embed images (tweets / articles / linked previews).
                    # Discord renders X/Twitter posts + article links as
                    # embeds with .image and .thumbnail proxies. Worth
                    # capturing — a shared tweet w/ a chart screenshot
                    # carries real signal. PIL re-encode handles whatever
                    # format the CDN returns.
                    for e in msg.embeds:
                        for attr in ("image", "thumbnail"):
                            ref = getattr(e, attr, None)
                            url = getattr(ref, "url", None) if ref else None
                            if url and isinstance(url, str):
                                image_count += 1
                                images_by_user[uid].append({
                                    "url": url,
                                    "content_type": None,  # CDN response will dictate
                                    "ts": msg.created_at.isoformat(),
                                })
                    embed_texts = _extract_embed_texts(msg)
                    content = (msg.content or "").strip()
                    by_user[uid].append({
                        "timestamp": msg.created_at.isoformat(),
                        "content": content,
                        "image_count": image_count,
                        "embed_texts": embed_texts,
                    })
                    # Tally slurs from this user's own message text
                    if content:
                        slur_counts[uid] += count_slurs_in_text(content)
                    count += 1
                print(f"  {count} messages, {len(by_user)} unique authors so far",
                      flush=True)

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
            summary_lines.append(
                f"- **Channels:** {', '.join(ch.name for ch in targets)}\n"
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
                        tasks.append(_generate_profile(
                            gemini_client,
                            meta["display_name"],
                            msgs,
                            username=meta.get("username", ""),
                            http_session=http_session,
                            images=images_by_user.get(uid, []),
                            user_id=uid,
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
                        profile, trader_score, trader_rationale, racial_humor_score = result
                        if not profile:
                            print(f"  ✗ {meta['display_name']}: empty / unparseable response",
                                  flush=True)
                            continue
                        last_seen = msgs[-1]["timestamp"]
                        slur_n = slur_counts.get(uid, 0)

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

            # After all upserts: recompute ordinal trader_rank across all
            # profiled users (1 = highest score). Cheap, single pass.
            db.recompute_trader_ranks_on_profiles()

            print(f"\nProfiled {len(eligible)} users.", flush=True)
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
    parser.add_argument(
        "--channels", type=str, default=",".join(DEFAULT_PROFILE_CHANNELS),
        help=f"Comma-separated channel names (default: "
             f"{','.join(DEFAULT_PROFILE_CHANNELS)})"
    )
    args = parser.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(run(args.days, channels))


if __name__ == "__main__":
    main()
