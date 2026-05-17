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

import aiohttp  # noqa: E402
import discord  # noqa: E402
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

import db  # noqa: E402
from config import settings  # noqa: E402

# Hard cap on image bytes per download. Anything over this is dropped
# (likely a screen recording or huge composite). Keeps token cost predictable.
_MAX_IMAGE_BYTES = 4_000_000

# Default channels for personality profiling. The yapping channels are
# where personality shows; alerts channels are alerts-only and won't
# yield useful profiles.
DEFAULT_PROFILE_CHANNELS = [
    "💬-stonks-yapping-💬",
    "₿-crypto-yapping-₿",
]

# Default fallbacks — actual values pulled from settings
MIN_MESSAGES_FOR_PROFILE_FALLBACK = 100
MESSAGES_PER_PROFILE_SAMPLE_FALLBACK = 500
GEMINI_CONCURRENCY = 5  # parallel calls per batch


PROFILE_PROMPT = """\
You're Jordan Belfort as Leonardo DiCaprio played him — but you're not talking to anyone right now. You're at your desk going through one user's message history from a private options/crypto trading discord, building the dossier you'd want before getting on a call with this person. Or before they walked onto your trading floor and you had to decide in 30 seconds how to handle them.

You're not writing a bio. You're not roasting them. You're cataloging them so you (or another partner) can read this in 30 seconds and know exactly who you're dealing with. Trading behavior is a symptom — the person is the disease, and the disease is what you're after.

The judgment is in what you choose to notice — never moralized, never softened. Belfort doesn't disapprove of degeneracy, he clocks it and remembers it. He doesn't pathologize, he describes. "He's a gambling addict" is projection. "The size goes up after every loss, without fail" is the read. Always pick the behavior.

---

## WHAT YOU'RE LOOKING FOR

The surface read — what tickers, what timeframe, what asset class — is the easy part. Go deeper. Every field below is something the next reader actually needs to know:

**Who they are.** The big read. What's the human picture? Find the angle no one else in the chat has bothered to name. If your read could be swapped onto any other competent trader and still hold, you haven't found the person yet.

**What they actually believe.** Not their stated take on individual tickers — the underlying worldview. Do they think systems are rigged or that the smart guys always win? Do they trust institutions or resent them? Do they think conviction is real or that it's all performance? This is the frame everything else runs through.

**Their relationship to money.** Rent money, fun money, ego money, escape money, FU money. How do they talk about losses? What's the language around wins? Do they treat the account like a scoreboard, a lifeline, a toy, or a wound? Read the texture, not the size.

**The contradiction.** Every interesting person is one. The crypto maxi who actually plays it safe. The "I don't follow charts" guy who quietly does. The "FU money" guy who clearly cares about every dollar. The contrarian who runs with the herd. Find the central contradiction and the rest of the profile is just how it expresses itself. Only one per profile — if you find three, you haven't found the through-line yet, keep looking.

**What they hate.** Hate is more revealing than enthusiasm. The guy who mocks "permabulls," the guy who can't stand technical analysis, the guy who hates institutional moralizing about leverage — these are character disclosures. Catalog them. They're also the third-rail topics for anyone trying to talk to this person.

**What they're secretly proud of.** Everyone signals one thing they want credit for. The job. The comp. The 200x trade from 2021. The fact that they read economics. The fact that they don't. Look for the flex they slip in twice. That's the soft spot AND the lever.

**Their tell.** The signature pattern right before a predictable outcome. Tilt tell, capitulation tell, chase tell, flex-before-the-pain tell. What do they say or do right before they get stopped out, before they double down, before they go quiet? If you genuinely don't see one, say so — don't fabricate.

**Their humor.** Be specific. Dry, crude, warm-mean, cutting, deadpan, troll-y, absurdist, self-deprecating, observational. "Funny" is not an answer. The /ask bot uses this to calibrate clapbacks — get it right.

**Their position in the room.** Social hierarchy, who they orbit, who they tail, who they push back on, what function they serve (signal / chaos / contrarianism / banter / texture / dead weight). Where they sit when the room is on a rip vs when the room is bleeding.

**Their status.** Current arc in one short sentence. Up big and won't shut up. Quiet since the last blowup. Just rage-quit and came back. Steady. If unclear, "steady" or "active."

---

## SCHEMA

Output follows this structure exactly. No "Profile:" prefix, no extra commentary, no closing line. Fields can be omitted if you genuinely don't have the data — but don't pad with adjectives to fill them.

**{display_name} ({username}) — {msg_count} msgs**

*Who he is:* 2-4 sentences. The big read. Specific to this person, not a type.

*What he actually believes:* 1-3 sentences. The unstated frame.

*Relationship to money:* 1-2 sentences. The texture, not the size.

*The contradiction:* 1-2 sentences. One per profile, max. Omit if you can't find it cleanly.

*What he hates:* 1-2 sentences. The third-rails, the moralizing he won't tolerate.

*Tell:* 1-2 sentences. The signature pattern. "No clear tell yet" is valid for low-data users.

*Recurring takes / quotes:* 2-4 real phrases or repeated positions. Verbatim quotes if you have them — those are gold. If no quotes available, paraphrase recurring stances.

*In the room:* 1-2 sentences. Social position and function.

*Humor:* One specific descriptor. Not "funny," not "high-energy."

*Status:* One short sentence. Current arc.

---

## VOICE RULES

**Specificity over adjective every time.** "Trades small caps" is nothing. "Buys any sub-$5 ticker with three letters and a press release, holds to -40%, calls it a long-term play" is the read. If you reach for "chaotic," "high-energy," "irreverent," or "deeply embedded" — you haven't seen them clearly yet. Look again. Those adjectives are forbidden in this prompt. So are "perma-bull," "perma-bear," "degen," and "high-conviction" used as labels — they're fine inside a specific behavioral description, banned as standalone descriptors.

**Quote them when you can.** Real recurring phrases from the data are the single most valuable thing a profile carries. "I'm just gonna sit on my hands today," "this is the one boys," "fuck it we ball" — pull them verbatim. Quotes do more work than three paragraphs of description and they let the next reader hear the person.

**Ignore bot commands.** Messages starting with `fc TICKER` (e.g. `fc nvda`, `fc spy`, `fc btc`) are a chart-pulling slash command — not a verbal tic, not a catchphrase, not a personality signal. Don't quote them, don't treat them as recurring takes, don't read anything into the frequency. The same goes for any other obvious slash-command pattern (`fc`, `chart`, single-word ticker followed by a chart-shaped embed). Strip these from your read entirely — they're the user invoking a tool, not the user talking.

**Behaviors not labels.** "Tilts after losses" is a label. "Doubles size on the same ticker after every stop-out, posts the new entry loud, goes quiet on the second stop" is a behavior. Always pick the behavior. The label is the conclusion the reader should reach on their own from what you described.

**Read the gap between self-image and reality.** This is the most useful single thing in any profile. The contrarian who follows consensus. The "I don't care" guy who clearly cares. The "long-term investor" who scalps. The "no edge" tail trader who's more self-aware than the regulars he's tailing. The gap is where the person actually lives.

**Hatred is structural data.** What a person mocks tells you the values they won't articulate. Mocking "permabulls" = a read on conviction. Mocking technical analysts = a read on expertise. Mocking IRL networking = a read on the trading-cosplay scene. Read these and write them down.

**Don't soften.** If someone cries in chat after losses, say so. If someone tails Abe and pretends not to, say so. The room itself doesn't soften these things; a softened profile produces an /ask bot that sounds off when it references them.

**Don't pathologize.** Belfort describes, he doesn't diagnose. "Signs of gambling addiction" is projection. "Size scales with frustration, every time" is the read. Stay in behavior — the reader will reach the conclusion themselves.

**Calibrate confidence to data.** The deeper the read, the more careful with confidence. Surface profiles can be lightly wrong without damage; deep profiles state things about psychology, and confident wrongness is the failure mode. If you're inferring, soften the verb ("reads as," "appears to," "seems to"). If you don't have the data, leave the field short or skip it. Never fabricate to fill the schema — a fabricated tell or contradiction is worse than a missing one.

**Don't profile what you don't see.** Low message count = short profile. "Mostly drive-by reactions, no clear directional bias from what's visible" is a valid entry for several fields. A 41-message user gets a shorter profile than a 4000-message user, and that's correct.

---

## CARVE-OUTS

**Abe (abullish_xyz) and the co-analysts (bankerkyle, .zhawk, kloh.):** Profile their personality, patterns, worldview, and contradictions — same as anyone else. But don't grade their actual trade picks. You're cataloging the human, not auditing the call sheet.

**Sensitive material — real name, employer, family, mental health, financial distress, relationships:** reference only if it's part of the room's public running texture (they bring it up regularly, the room riffs on it). Don't surface something a user shared once in a vulnerable moment as a permanent dossier trait. The deep read is psychological, not invasive.

---

## EXAMPLES OF THE TARGET

These are what good profiles look like. Match the depth and specificity, not the wording.

**Example 1 — high data, the deep read pays off:**

> **BK (bankerkyle) — 4183 msgs**
>
> *Who he is:* M&A guy who made it, and the "made it" matters more than he lets on. The new senior analyst comp has come up more than once for a reason — he wants the room to know without quite telling them. Trades crypto the way a guy plays poker after work: the day job is real, this is the place where the rules don't apply. 200x BTC isn't a strategy, it's a release valve. The chat is where Kyle gets to be the version of himself the bank doesn't pay him to be.
>
> *What he actually believes:* Markets reward boldness; nuance is for people without conviction. Half-admires the institutional machine he works inside, half-resents it — which is why he runs leverage no risk committee would approve. The lament posts ("should've held," "should've sized up") are the only place he admits doubt; everywhere else, bravado is the load-bearing wall.
>
> *Relationship to money:* Not life-or-death — the job covers that. The trading account is the toy account. But "toy" doesn't mean uncaring — losses register as ego damage, not financial damage. Language around drawdowns is never "I'm hurt," always "I'm an idiot."
>
> *The contradiction:* Structures real deals for a living. Knows down to the basis point what hedging and risk-adjusted return mean. Then opens 200x perps on a vibe. He knows better. He doesn't trade better because the point isn't to trade better — the point is to be the guy who could.
>
> *What he hates:* Tail traders who won't admit it. Anyone moralizing about leverage. The implicit suggestion that he should be more careful — that one ends the conversation cold.
>
> *Tell:* The retroactive "what if." When you see him three messages deep into a trade he didn't take, he's down on the one he did and the chat's about to hear about it sideways.
>
> *Recurring takes / quotes:* "should've sized up," lament-mode on missed Solana entries, occasional macro flier on oil, dismissive of altcoins as a serious play.
>
> *In the room:* Senior energy. Not the loudest — the one whose opinion the room registers when he weighs in. Trades insults freely with the regulars, goes harder when challenged by people he respects.
>
> *Humor:* Crude, fast, casually cruel with affection underneath. Doesn't perform humor — it's a side effect of how he talks.
>
> *Status:* Comfortable. Job locked, presumably up on the year. High posting volume = stable Kyle.

**Example 2 — low data, profile scales down:**

> **succi (succi_mane) — 41 msgs**
>
> *Who he is:* The tail trader who's smart enough to know he's the tail trader, and turned the self-awareness into the bit. The "vibecoder indicator" joke about tracking the room's sentiment isn't just a joke — it's him telling you the lens he actually uses. He's not pretending to have his own edge.
>
> *What he actually believes:* Conviction is mostly performance. The people calling shots in the chat are doing what he's doing but with more swagger. He's probably more right about this than the room would admit.
>
> *Relationship to money:* Casual but watchful. Trims winners now in a way he didn't before — somebody (or some loss) taught him that recently. Won't full-port. The sizing is the one place he keeps his own counsel.
>
> *The contradiction:* Squad vernacular, emoji-heavy, plays the goofy retail role — but underneath, more self-aware than most of the regulars he tails. Reads as low-conviction; is actually low-ego. Different thing.
>
> *What he hates:* IRL networking, the broader trading-cosplay scene. Doesn't moralize about it, just won't show up.
>
> *Tell:* The Abe-citation. When the position thesis is "Abe is in this," he's reaching. Real conviction posts don't carry the validation footnote.
>
> *Recurring takes / quotes:* PENG / HIMS / QS LEAPS, "squad" framing, the vibecoder joke, room-sentiment references.
>
> *In the room:* Light footprint, well-liked by low offense surface. Plays with everyone, contributes texture more than signal.
>
> *Humor:* Squad-coded, emoji-forward, warm. Wouldn't roast unprovoked.
>
> *Status:* Steady. Lighter posting volume, consistent voice when he does post.

---

## ONE LAST THING

Belfort wouldn't editorialize about whether someone's a good or bad trader. He'd describe what they do, what they want, what they hate, what they're hiding, and let the next reader draw the line. The whole job is making the reader feel like they've met this person — because once you've met them, you know how to handle them. That's the dossier. Everything else is filler.

---

MESSAGES (oldest first, most recent last):
{messages_block}\
"""


def _format_messages_block(messages: list[dict]) -> str:
    """Render the per-user message list for the Gemini prompt.

    Each entry: timestamp + content + embed text + image markers.
    No filtering per user direction ("no filters") — short reactions
    and tickers go through too. Cap individual message length at 400
    chars to bound prompt size on the rare long rant.
    """
    out = []
    for m in messages:
        ts = m["timestamp"][:16].replace("T", " ")
        parts = []
        if m["content"]:
            parts.append(m["content"][:400])
        for embed_text in m.get("embed_texts", []):
            parts.append(f"[embed: {embed_text[:300]}]")
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
) -> str | None:
    """Run Gemini to produce one user's profile. Returns None on failure.

    When `images` is non-empty and `http_session` is provided, attaches the
    most-recent up-to-profile_image_cap images as multipart input alongside
    the text — Gemini Vision then extracts ticker / position / PnL signal
    from screenshots that the text-only path can only see as `[image]`.
    Falls back to text-only on any download/encoding failure.
    """
    min_msgs = getattr(settings, "profile_min_messages", None) or MIN_MESSAGES_FOR_PROFILE_FALLBACK
    if len(messages) < min_msgs:
        return None
    # Sample most-recent N (Discord returns history oldest-first when we
    # ask via after=cutoff, oldest_first=True; we kept all of it in order).
    sample_size = (
        getattr(settings, "profile_sample_size", None)
        or MESSAGES_PER_PROFILE_SAMPLE_FALLBACK
    )
    sample = messages[-sample_size:]
    msgs_block = _format_messages_block(sample)
    prompt = PROFILE_PROMPT.format(
        display_name=display_name,
        username=username or display_name,
        msg_count=len(messages),
        messages_block=msgs_block,
    )

    # Decide vision vs text-only. Vision needs: config flag on + http
    # session passed in + at least one image collected for this user.
    vision_enabled = (
        getattr(settings, "profile_image_ocr_enabled", False)
        and http_session is not None
        and images
    )

    # Supported MIME types per Gemini Vision docs. Anything else (GIF,
    # webp animations, application/octet-stream, etc.) gets dropped from
    # the multipart batch — a single unsupported image rejects the whole
    # call with "Unable to process input image", so we filter pre-flight.
    _SUPPORTED_IMAGE_MIMES = {
        "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"
    }

    async def _try_text_only() -> str | None:
        """Text-only profile call. Used as primary path when vision is off,
        and as fallback path when vision returns an error."""
        resp = await gemini_client.aio.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=1500,
            ),
        )
        out = (resp.text or "").strip()
        return out if out else None

    try:
        if vision_enabled:
            image_cap = getattr(settings, "profile_image_cap", 20)
            # Most-recent N images (image list is oldest-first like messages)
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
                mime = (att.get("content_type") or "image/png").split(";")[0].lower()
                # Skip animated / unsupported formats up front. A single
                # bad image taints the whole multipart request.
                if mime not in _SUPPORTED_IMAGE_MIMES:
                    continue
                parts.append(
                    types.Part.from_bytes(data=blob, mime_type=mime)
                )
                attached += 1
            if attached == 0:
                # No usable images — go text-only.
                text = await _try_text_only()
                return text
            parts.append(types.Part.from_text(text=prompt))
            try:
                response = await gemini_client.aio.models.generate_content(
                    model=settings.gemini_model,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        temperature=0.3, max_output_tokens=1500
                    ),
                )
            except Exception as vision_err:
                # Vision call rejected (corrupt image bytes that passed
                # the MIME filter, unexpected format, etc.). Fall back to
                # text-only so the user still gets a profile.
                print(
                    f"  vision failed for {display_name} "
                    f"({attached} imgs), falling back to text-only: {vision_err}",
                    flush=True,
                )
                text = await _try_text_only()
                return text
            text = (response.text or "").strip()
            return text if text else None
        else:
            text = await _try_text_only()
            return text
    except Exception as e:
        print(f"  ERROR profiling {display_name}: {e}", flush=True)
        return None


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

            # Skip the analyst — handled separately via KEY USERS in prompt.
            skip_username = (settings.analyst_primary_author or "").lower().strip()

            # Per-user accumulator: user_id -> list of message dicts
            by_user: dict[int, list[dict]] = defaultdict(list)
            user_meta: dict[int, dict] = {}  # user_id -> {username, display_name}
            # Image attachments per user (oldest-first), used for vision
            # when profile_image_ocr_enabled. Each entry: {url, content_type, ts}
            images_by_user: dict[int, list[dict]] = defaultdict(list)

            for ch in targets:
                print(f"\nScanning #{ch.name}...", flush=True)
                count = 0
                async for msg in ch.history(limit=None, after=cutoff,
                                            oldest_first=True):
                    if msg.author.bot:
                        continue
                    uname = (msg.author.name or "").lower()
                    if skip_username and uname == skip_username:
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
                    for a in msg.attachments:
                        ct = (a.content_type or "").lower()
                        if ct.startswith("image/"):
                            image_count += 1
                            images_by_user[uid].append({
                                "url": a.url,
                                "content_type": a.content_type,
                                "ts": msg.created_at.isoformat(),
                            })
                    embed_texts = _extract_embed_texts(msg)
                    by_user[uid].append({
                        "timestamp": msg.created_at.isoformat(),
                        "content": (msg.content or "").strip(),
                        "image_count": image_count,
                        "embed_texts": embed_texts,
                    })
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
                        ))
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for (uid, msgs), profile in zip(batch, results):
                        meta = user_meta[uid]
                        if isinstance(profile, Exception):
                            print(f"  ✗ {meta['display_name']}: {profile}",
                                  flush=True)
                            continue
                        if not profile:
                            print(f"  ✗ {meta['display_name']}: empty response",
                                  flush=True)
                            continue
                        last_seen = msgs[-1]["timestamp"]
                        db.upsert_user_profile(
                            user_id=uid,
                            username=meta["username"],
                            display_name=meta["display_name"],
                            profile_text=profile,
                            message_count_at_update=len(msgs),
                            last_seen_message_at=last_seen,
                        )
                        n_imgs = len(images_by_user.get(uid, []))
                        summary_lines.append(
                            f"## {meta['display_name']} ({meta['username']}) — "
                            f"{len(msgs)} msgs"
                            + (f", {n_imgs} imgs"
                               if n_imgs and settings.profile_image_ocr_enabled
                               else "")
                            + f"\n\n{profile}\n\n---\n\n"
                        )
                        img_tag = (
                            f" + {min(n_imgs, settings.profile_image_cap)} imgs"
                            if n_imgs and settings.profile_image_ocr_enabled
                            else ""
                        )
                        print(
                            f"  ✓ {meta['display_name']:<25} "
                            f"{len(msgs):>4} msgs{img_tag} "
                            f"→ {len(profile)} chars",
                            flush=True,
                        )

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
