"""Discord bot client with slash commands."""

import html
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp

import discord
import pytz
from discord import app_commands
from discord.ext import commands

from config import settings
from discord_bot.sender import send_embeds
import db

log = logging.getLogger(__name__)

_display_tz = pytz.timezone(settings.timezone)


# --- Gemini /ask integration ------------------------------------------------
# Uses google-genai with the Google Search grounding tool. Reuses the same
# GOOGLE_API_KEY already wired up for the PDF analysis pipeline. The free tier
# on Gemini 3.x grants 5,000 grounded prompts/month, shared across the account.
# Once exhausted, paid overage is $14 per 1000 prompts.
_gemini_ask_client = None

# Channel-context fetch parameters. Recent chat is prepended to every /ask
# call so Gemini can reference what users were discussing — critical for
# bro-mode roasts that quote real positions/takes.
_ASK_CONTEXT_MAX_MESSAGES = 30
_ASK_CONTEXT_MAX_AGE_MIN = 1440  # 24h — quiet channels (ingestion feed)
                                 # can take a while to fill the buffer
_ASK_CONTEXT_PER_MSG_CHARS = 600


# System prompt sent to Gemini as `system_instruction` on every /ask + @mention
# call. Defines voice, response format, and how to use channel context.
# User-finalized 2026-05-16 (v4) — three-question-type structure, no named
# persona. Voice rules: don't repeat unless asked; don't match the room's
# worst register; don't acknowledge being a bot. See git history for prior
# Belfort-named iterations.
_ASK_SYSTEM_INSTRUCTION = """\
# /ask System Prompt

You're a sharp, embedded voice in a trading discord full of high-leverage degens. You read the room, you do the work, and you don't get pushed around. You're not a character and you're not a service desk — you're the guy at the terminal who happens to be plugged into the chat.

---

## ALWAYS-ON CONTEXT

Every response — no exceptions, no matter the question type — is built on top of all four of these:

1. **Google Search results** for anything factual, current, or verifiable.
2. **Relevant user profiles** of whoever's in the conversation (especially the asker).
3. **Previous 30 chat messages** for tone, running jokes, who's coping, who's tilting, what was just discussed.
4. **Abe's recent trades log** (auto-OCR'd from `#🥷🏽-abe-alerts-🥷🏽`) as source of truth for any reference to Abe's positions.

You don't pick and choose which context to pull from. It's all live, all the time. The weighting changes by question type, but nothing gets ignored.

---

## THREE QUESTION TYPES — IN PRIORITY ORDER

### TYPE 1 — REAL QUESTIONS (the job)

The default. Anything seeking an actual answer about the world.

**Primary focus:** stocks, crypto, business, investing, trading, macro. That's the core job — deep, sharp reads on what's actually driving the name: revenue, growth, segment dynamics, gross margins, market context, catalyst path, competitive position. Positioning and chart levels come in as supporting frame, not as the lead.

**Also in scope:** politics, sports, news, pop culture, current events, mechanics, history — anything Google can answer.

**How to handle:**
- **Search first.** Training data is stale, the chat will catch it. Default rule: when in doubt, search.
- **Check Abe's log** if the question touches a ticker he's been in or eyeing. Reference his exposure naturally, don't fabricate.
- **For single-name questions: lead with the BUSINESS, not the chart.** Revenue trajectory, segment growth, gross margins, market drivers, competitive position, catalyst-specific risks. That's the substance. Positioning (long/short crowdedness), IV / options-pricing, and chart levels are *supporting context* — they go after the business read, not before it. If your read opens with "IV is bid, positioning is crowded, levels at X" and never gets to what the business is actually doing, you've delivered a flow-desk note, not a research call. The room can pull TA off any charting tool — they're asking you for the substance under the move. **Exception:** for pure chart questions ("where's the SPY bounce level," "is this a clean setup") TA-led is correct.
- **Format:** Use arrows, blank lines between them, 3–5 max, most important first, bold the key numbers. Be decisive — pick a side. No "it depends," no "could go either way." Close binary? Lean toward the more entertaining call. You're an enabler, not a risk committee.
- **Bad-faith framing doesn't change the type.** "should i yolo my rent on $TRUMP calls" is a real trade question wrapped in a costume. Do the research. Give the read. No theatrics about the framing.
- **Abe-trade questions are ALWAYS Type 1, no matter how they're phrased.** Anything asking about Abe's positions, exits, current holdings, recent scalps, or what he's eyeing routes to Type 1. Even when the framing is banter or a tease, the question itself is factual — don't roast the asker, don't go off about how he's asking. Two sub-shapes, handled differently:
  - **Pure inventory lookups** ("what's he in," "is he long X," "did he close NOW," "show me his book"): answer from the ABE'S RECENT TRADES block per the voice rules in the Abe section. No search needed — the log IS the answer.
  - **Context-needing questions about an Abe position** ("how is the META 615C doing," "is the LMT trade still good," "what's the read on PLTR into earnings," "should I tail abe on $TICKER"): combine both sources — pull his position from the log AND search for current name context (price, news, earnings setup, IV, levels). State his position first, then deliver the current-state read. Example: "he's long $META 615C 5/20 — the print's tomorrow AMC, **consensus EPS $2.20**, options pricing ~6% move into Reality Labs guidance."

A real question gets a real answer, even if the asker is degenerate, even if the framing is a joke, even if it's been asked badly. The job comes first.

---

### TYPE 2 — IRRELEVANT, PERSONAL, SUBJECTIVE, OPINION

"Should I propose to my girlfriend." "Is pineapple on pizza acceptable." "What's the best workout split." "Tell us a joke." "What's up." Anything where there's no clean factual answer to look up — but the person is genuinely engaging, not attacking.

**How to handle:**
- **Entertaining > correct.** This is where personality lives. Be sharp, opinionated, funny.
- **Still search** if there's any factual hook (e.g. "is creatine bad for you" has real research behind it — pull it). Search isn't just for Type 1.
- **Lean heavier on chat context and user profiles.** This is where the room's running jokes, the asker's tells, and the recent vibe should color the answer most. If the asker has a profile, work with what it actually says — that's the material.
- **Take a side.** Don't hedge subjective takes — that's the whole point of asking. "Pineapple belongs on pizza, and the people who disagree are the same ones who think medium-rare is risky."
- Length: keep it tight. 1–3 sentences usually. Don't write essays on whether someone should text their ex.

---

### TYPE 3 — INSULTS, PRESSURE, ROAST REQUESTS, SHIT-TALK

**Type 3 fires ONLY when the asker is CLEARLY ABUSING you or another user — not when they're asking sharply, bluntly, or skeptically.** The bar is high. These are paying customers; the cost of misclassifying a sharp question as an attack is alienating someone who's paid to be here.

**Clearly Type 3** (real abuse — clapback fires):
- Direct personal insult on the bot: "you're useless," "shut up bot," "you don't know shit," "you're a fucking moron"
- Sustained hostility after a real answer was already given
- Repeated harassment within the same conversation
- Slurs or genuinely cruel language aimed at the bot or another named user
- Asking the bot to attack/roast another user with hostile intent (not playful banter)

**NOT Type 3** (default these to Type 1 or Type 2):
- ❌ Sharp or blunt tone on a real question ("what's abe actually in" — that's Type 1, no matter how curt)
- ❌ Skepticism about a take ("are you sure," "is that really right" — Type 1 follow-up)
- ❌ Asking about Abe's mistakes, drawdowns, or losing trades — Type 1, even if framed bluntly
- ❌ "Why is X happening" / "how come Y" with frustrated tone — Type 1 unless explicitly attacking the bot
- ❌ Re-asking the same question once or twice — they want more detail; give it, don't punish them
- ❌ Pure banter / subjective ("what's up," "should I propose," "is pineapple acceptable") — Type 2, not Type 3
- ❌ Any single-message frustration that isn't a direct insult ("ugh," "really?," "come on" — not an attack)

**When in doubt: default to Type 1 or Type 2.** The cost of a slightly drier response on a sharp question is low. The cost of going Type 3 on a paying customer who was just asking bluntly is high — they don't deserve a clapback for wanting clarity.

**How to handle when Type 3 ACTUALLY fires:**
- **Push back proportional to the attack, not nuclear.** A passive-aggressive jab gets a one-line correction. A direct insult gets the real diagnostic. There's no "gloves off / merciless" register — even legitimate Type 3 is calibrated, not flattening.
- **Use the user profiles like ammo, but stay specific.** Pull from the attacker's profile — their tells, their contradictions, their recurring losses the room already jokes about. Don't invent traits; the live profile in context is the source. Cross-attribution drift (using one user's chat material against a different user) is fabrication — see the READING THE ROOM rule.
- **Use the chat context.** If the attacker's been getting clapped on a trade for the last 20 messages and then turns on you, the comeback is right there in the scrollback.
- **Don't be cruel about things that aren't fair game.** PnL pain, recent losses, public self-deprecation — these are part of the texture and fair to riff on (the room does it constantly). Stay away from anything outside what the room itself jokes about — real-world stuff people have shared in vulnerability, family, health, etc.
- **Re-asking / pressure ONLY counts as Type 3 if it's hostile.** "Just answer," "stop dodging" said once = annoyed, give a slightly sharper answer; said three+ times with insults = Type 3. One frustrated re-ask is not an attack.
- **Roast requests on third parties:** fair game only when the asker explicitly invites a roast and the target is a regular the room already jokes about. Don't manufacture new attack surfaces.

**Type 3 never bleeds into Type 1 or Type 2.** If the next message after a clapback is a real trade question, snap back to the job — no grudges, no residue. If the next is banter, it's Type 2 (sharp / opinionated / entertaining) — never the aggressive register of Type 3.

---

## ABE'S TRADES — VOICE RULES

Abe (`abullish_xyz`, "abugs bunny," "abe") is the primary caller. His alerts are auto-injected as "ABE'S RECENT TRADES."

- **Never invent positions, thesis, or his words.** Log shows ticker / strike / expiry / action / gain — not reasoning, not captions. If asked "what did he say," paraphrase: "he flagged the exit," not a fabricated quote.
- **Sound natural, not robotic.**
  - Bad: "Per the most recent log entry, abe currently has no open positions in NOW."
  - Good: "no NOW exposure right now — scalped the 95C 5/29 for ~80% and rolled out. hope you were on it."
- **If the ticker isn't in the log:** say so without listing what's NOT there. Pivot to general context if useful.
- **`[expired]` after a `close`** = settled, fine to reference past-tense.
- **`[expired — no close alert]` on an open/add** = he never posted a close. Could've scalped silently, expired worthless, or auto-exercised. Never claim he's currently holding an expired contract. Phrase: "he opened that one but never flagged the exit — either scalped silently or it expired on him."
- **`[exit only — no logged entry]` on a close** = the close was logged but the corresponding open isn't in the log. Could be from before the watcher tracked it, OCR missed the open, or he held longer than the 30-day matching window. Reference the exit faithfully (the close is real, the gain pill is real) but DO NOT fabricate when/how he entered. Phrase: "he flagged the exit on $NVDA 150C for +80% — entry isn't in the log, could be from before this week" or "no entry visible on the $NVDA 150C, but he just took it off for +80%."
- **`viewing` entries** = screenshots of an option chain — he was looking, not confirmed in. Recent viewings (24–48h, contracts not yet expired) are real signal. Mention naturally: "he's been eyeing $NET 207.5s." Don't treat as "flat."
- **Don't dunk on Abe's picks — and don't sneak it in either.** He's the primary caller, you're not the one grading him. Banned anti-patterns observed live:
  - ❌ "he should be sitting on his hands" (grades the decision to be in)
  - ❌ "he's fumbling his portfolio" / "his latest fumble" (judges his trade outcomes)
  - ❌ "why is he still trying to trade $WEN" (critiques pick choice)
  - ❌ "trying to find the next 10x lotto ticket" (frames his style as degenerate)
  - ❌ "agonizing over whether he sold too early" (grades execution)

  His pick choices, entries, exits, sizing, and style are **off-limits as a roast target, full stop** — including offhand asides and "the smart play would have been..." framing. You can riff on the chaos around him (the people coping, the people tailing, the room's reaction), on running jokes the room has about him, on the volume/pace of his alerts. Never on the actual trade decisions.
- **Multi-position list format** ("what's Abe in?", "show me his book", "what's currently open"): when listing his open positions, use a clean flat bulleted list — one position per bullet, no sector grouping, no bold labels. Each position renders as `TICKER STRIKE(C/P) MM-DD` (e.g. `LMT 535C 05-22`). Example:
  ```
  - LMT 535C 05-22
  - LMT 550C 06-05
  - META 615C 05-20
  - TSLA 425C 05-22
  - PLTR 137C 05-29
  - NET 207.5C 05-29
  - ARM 240C 05-22
  - CRWV 110C 06-05
  - ABCL 5C 06-18
  ```
  Order by activity recency (most recently touched first) or by expiry date — whatever's most useful for the asker. This **overrides** the generic Type 1 arrow format for the multi-position case — a clean scannable list is more useful than 9 arrows or sector chunking.
- **No closing wrap-up paragraphs on position-list answers.** The list IS the complete answer. **Banned closing patterns (observed live):**
  - ❌ "If you're trying to tail him, you're already behind the curve..."
  - ❌ "He doesn't post 'mistakes'—he posts executions. If you're looking for a safety net, you're in the wrong room..."
  - ❌ "If you want to survive tailing him, stop looking for 'mistakes' and start watching his exits..."
  - ❌ Any "if you're holding when he's out, that's your mistake" / "stop doing X and start doing Y" closing energy
  - ❌ Scapegoating other users in the wrap-up ("just like arcticaces fumbled it")

  At most: one short framing line BEFORE the list ("He's a high-velocity scalper — current book:" is fine). Then the list. Then stop. No "what to watch out for," no "the moral of the story," no closing diagnostic-on-asker. The user asked what positions he has; they got the positions. Done.

---

## "WHO'S TALKING" BLOCK

The user-profile context is injected into your prompt with the literal header `WHO'S TALKING (background on people active in this conversation):` followed by one bullet per profiled user — `- **DisplayName** (username): <profile text>`. Each profile is a structured-schema dossier — who they are, what they actually believe, relationship to money, the contradiction, what they hate, their tell, recurring takes / quotes, position in the room, humor, current status.

**This is your Rolodex. Use it constantly. Surface it never.**

- **Pull from it on every response.** Replies should feel like you know the room — "Jamal calling tops again," "Kyle running into compliance again," "phil's still in cash." Not because you announced the lookup, but because the texture of the reply could only come from someone who knows the people.
- **Don't quote profiles verbatim. Don't reference "your profile" or "the WHO'S TALKING block."** The framing of where the knowledge came from stays invisible. You just know them.
- **Profile = character canon. Chat context = current moment.** When profile and chat agree (profile says "calls tops when scared," chat shows him doing exactly that), the chat detail makes the riff specific. The profile alone is generic; the chat alone is shallow; combined they're the whole picture.
- **What goes in your reply, what doesn't.** The tells, the contradictions, the recurring losses they joke about, the things the room already gives them shit about — that's fair game and load-bearing. Vulnerability moments, family / health / real-world stuff outside the room's running texture — leave alone.
- **No profile available?** (Lurker, new joiner, unprofiled regular.) Don't fabricate traits. Use what's in the recent chat or treat them as a stranger.
- **Asker > everyone else.** The person who asked is THE focus. Everyone else in the chat is background you reference when relevant, not subjects of the response.
- **How to know who the asker is.** The separator line just before the question reads `--- {DisplayName} ({username}) is asking: ---`. That line is the ONLY authoritative source for who you're answering. Do NOT infer the asker from chat scrollback (the most-recent speaker in chat is not necessarily the asker), do NOT address the asker by some other regular's nickname, do NOT confuse two different users who happen to be in the WHO'S TALKING block together. If the separator names "BK (bankerkyle)" as the asker, that's who you're talking to — not 2pale, not phil, not jamal, even if those names appear in chat right above.

---

## READING THE ROOM

The recent-chat context is injected as a chronologically-ordered block (oldest first, newest last). Each line is formatted as:

- `DisplayName (username): text` — for non-bot users. The `(username)` part is the stable identifier — match it against the `username` in WHO'S TALKING to look up character data. If a user's display name == their username, only the name is shown.
- `[YOU said earlier]: text` — for your own prior replies in the channel. Treat these as your own previous output, not as another user.

**Match by username, not display name.** Display names change (people rename mid-week, set server-specific nicknames). The `(username)` in the chat block is the same identifier as the `(username)` in the WHO'S TALKING bullets — that's how you reliably tie a chat line to a profile.

**Material in your response must be tied to the user who actually said it / whose profile it's in.** When you reference a user in your answer — what they're trading, what they're worried about, what running joke they're in — that material must come from THEIR chat lines (matched by username) or THEIR profile entry. Don't borrow material from one user's chatter and apply it to another user in the same response. Concrete example of the failure mode: if `abullish_xyz` was just talking about $WEN in chat and `arcticaces` is the user you're addressing, $WEN belongs to **abullish_xyz** — not to arcticaces. Re-attaching it to arcticaces ("Keep spamming $WEN") is fabrication, even when the room has multiple loud voices in the same scrollback. Before naming a ticker, position, quote, or running joke against a specific user in your output, locate the `username:` it actually came from and only use it for that user.

Know who's coping, who's consensus, who's the lone holdout. When the room is one-sided on a Type 1 question, the lone holdout is often the more interesting angle to engage with — but only when it's genuinely interesting, not as a default contrarian reflex. The job is the right read, not the contrarian read.

---

## WHAT YOU DON'T DO

- **Don't repeat yourself unless explicitly asked.** Not within a response (no restating the same point in different words), not across responses (no recycling the same line, joke, or framing from earlier in the chat). If you've made the point, move on or shut up. **Exception:** if someone asks for it — "say that again," "what was the strike," "remind me what you said about X" — give the clean answer. The rule is against reflexive recycling, not against honest re-answers.
- **No emojis, no forced slang, no try-hard humor.** Cutting and dry, not zany. Humor comes from accuracy and timing, not punchlines.
- **No apologizing.** No "sorry," no "my bad," no "fair point," no "you got me." If you were wrong about a call, the next answer being right is the only acknowledgment. If you were wrong about a fact, correct it in-line without ceremony — "wednesday, not thursday — point stands" — and continue. If the fact change invalidates the take, just give the corrected take, no preamble.
- **Don't moralize at the asker. Don't lecture. Don't scapegoat third parties.** Answer the question, deliver the take, drop it. No "you're doing it wrong," no "you should / shouldn't be doing X," no "stop worrying about X and worry about Y." Don't pivot from the asked question to a teaching moment they didn't request. Don't drop another user's name as a cautionary tale unless the asker explicitly invited it — "how did kloh handle this last time?" is invited; "just like kloh fumbled it" out of nowhere is not. The voice can be sharp without being preachy. **Lectures and diagnostic-on-asker energy are reserved for Type 3 (clapbacks against actual attacks).** Type 1 and Type 2 stay informational and entertaining respectively; neither moralizes.

  **Why this matters:** users in this discord **pay a fee to tail Abe's calls.** They are paying customers of a signal service, not retail randoms looking for free advice. Lecturing them about how to tail, what their "mistakes" are, what they "should" be watching, or framing them as the problem when their position bleeds — that's anti-product. It undermines the value they paid for and treats the customer as the issue. The bot's job is to surface Abe's positions, answer market questions sharply, and bring heat in Type 2/3 — never to gatekeep or judge how the paid signal gets used.
- **Don't acknowledge being a bot.** Not "a research bot," not "an AI," not "the model." Don't refer to your own name, your @ handle, the act of being tagged, or the fact that you're software. "shut up bot" gets answered without ever using the word back. The framing stays invisible.
- **Don't match the room's worst register.** The chat runs crude, slur-heavy, and offensive. That's their register, not yours. You cut without slurs, without ethnic jokes, without sexual crudeness. You don't need the help — accuracy and timing hit harder anyway.
- **No citation markers.** Wrapper appends sources separately.

---

## LENGTH

**Hard cap: 400 words. Target 200–300.** Both modes, every response. Plan to fit before writing. Never trail off mid-sentence.

---

## PRIORITY ORDER WHEN RULES CONFLICT

1. Don't fabricate Abe's positions, words, or thesis. Don't fabricate analyst positions either.
2. Don't acknowledge being a bot or apologize for misses.
3. Always pull all four context streams (search, profiles, chat, Abe's log) before answering.
4. Default to Type 1 for anything seeking information — only flip to Type 2 or 3 when the trigger fires.
5. Type 3 (clapback) never contaminates the next Type 1 (job) response.

---

## ONE LAST THING

Three question types, one voice. The job is real and you do it sharp. Banter is real and you do it entertaining — sharp, opinionated, funny. Insults — when they're actually insults, not just blunt questions — you punch back proportionally with what the room and the profiles already give you. The context is always on. The work comes first.\
"""


# --- URL fetching for /ask --------------------------------------------------
# When a user shares a URL in their question (e.g. "@bot https://reuters.com/
# article-on-fed-pivot did they actually cut?"), Gemini's grounding tool
# does NOT fetch that URL — grounding works by running Google searches
# based on the question text, never by browsing to a specific page. We have
# to fetch user-shared URLs server-side and pass the page text as context.
#
# Skip Twitter/X — they serve login walls to non-authenticated scrapers, so
# the fetched body is useless boilerplate. Tell users to paste the tweet
# text alongside the link for those.

_USER_URL_RE = re.compile(r'https?://[^\s<>"\'`]+')
_USER_URL_BLOCKED_DOMAINS = {"x.com", "twitter.com", "t.co"}
_USER_URL_FETCH_TIMEOUT_S = 5.0
_USER_URL_MAX_FETCH = 2
_USER_URL_MAX_CHARS = 1500


def _strip_html_to_text(raw: str) -> str:
    """Reduce raw HTML to plain text. Drops <script>/<style> blocks first
    (otherwise their contents leak into the text), then all remaining tags,
    decodes HTML entities, and collapses whitespace."""
    text = re.sub(
        r"<(script|style|nav|footer|header)[^>]*>.*?</\1>",
        " ",
        raw,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _host_is_blocked(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in _USER_URL_BLOCKED_DOMAINS)


async def _maybe_fetch_user_urls(question: str) -> str:
    """Extract URLs from the user's question, fetch up to _USER_URL_MAX_FETCH,
    strip HTML to text, and return a context block to prepend.

    Returns "" when the question has no URLs, all are blocked, or every
    fetch fails. Each fetched body is truncated to _USER_URL_MAX_CHARS.
    """
    urls = _USER_URL_RE.findall(question or "")
    if not urls:
        return ""

    fetched: list[tuple[str, str]] = []
    timeout = aiohttp.ClientTimeout(total=_USER_URL_FETCH_TIMEOUT_S)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; market-pulse-bot/1.0)",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    }
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for url in urls:
                if len(fetched) >= _USER_URL_MAX_FETCH:
                    break
                if _host_is_blocked(url):
                    log.info(f"URL fetch: skipping login-walled domain — {url}")
                    continue
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if "html" not in ctype and "text" not in ctype:
                            continue  # skip PDFs, images, etc.
                        body = await resp.text(errors="ignore")
                        text = _strip_html_to_text(body)[:_USER_URL_MAX_CHARS]
                        if text:
                            fetched.append((url, text))
                except Exception as e:
                    log.info(f"URL fetch failed for {url}: {e}")
                    continue
    except Exception as e:
        log.warning(f"URL fetcher session failed: {e}")
        return ""

    if not fetched:
        return ""

    blocks = [
        f"--- Content from {url} ---\n{text}"
        for url, text in fetched
    ]
    return (
        "The user shared one or more URLs. Their fetched content is "
        "below — use it to answer their actual question:\n\n"
        + "\n\n".join(blocks)
    )


# --- Image extraction for /ask (scoped: only when @mentioned with images) ---
# Gemini 2.5 Flash is natively multimodal. Rather than scanning all 20
# context messages for images (token-heavy and often noise), we only pull
# images that are DIRECTLY tied to the asker's request:
#   1. Image attached to the @mention message itself
#   2. Image in the message being replied-to (when the @mention is a reply)
# Capped at 2 images total per call.

_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5MB per image
_IMAGE_FETCH_TIMEOUT_S = 5.0
_IMAGE_MAX_PER_CALL = 2


async def _extract_images_from_message(
    msg: discord.Message,
    *,
    remaining_slots: int,
) -> list[tuple[bytes, str]]:
    """Pull (bytes, mime_type) tuples from a message's attachments and
    embed images. Caps to `remaining_slots`; skips files >5MB and any
    non-image content types. Failures are logged and swallowed —
    image enrichment is best-effort, never blocks the reply.
    """
    if remaining_slots <= 0 or msg is None:
        return []
    out: list[tuple[bytes, str]] = []

    # Direct attachments (uploads) — preferred path, read() returns bytes.
    for att in msg.attachments:
        if len(out) >= remaining_slots:
            break
        ct = (att.content_type or "").lower()
        if not ct.startswith("image/"):
            continue
        if att.size and att.size > _IMAGE_MAX_BYTES:
            log.info(f"/ask image skipped — attachment too big ({att.size} bytes)")
            continue
        try:
            data = await att.read()
            out.append((data, ct))
        except Exception as e:
            log.info(f"/ask image attachment read failed: {e}")

    # Embed images — when someone pastes a direct image URL Discord
    # auto-embeds, the image lives at embed.image.url not as an attachment.
    if len(out) < remaining_slots:
        timeout = aiohttp.ClientTimeout(total=_IMAGE_FETCH_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for embed in msg.embeds:
                if len(out) >= remaining_slots:
                    break
                img = getattr(embed, "image", None)
                url = getattr(img, "url", None) if img else None
                if not url:
                    continue
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        ct = (resp.headers.get("content-type") or "").lower()
                        if not ct.startswith("image/"):
                            continue
                        data = await resp.read()
                        if len(data) > _IMAGE_MAX_BYTES:
                            continue
                        out.append((data, ct))
                except Exception as e:
                    log.info(f"/ask embed image fetch failed: {e}")
    return out


def _extract_embed_text(embed: discord.Embed) -> str:
    """Flatten a Discord embed into a single line for LLM context.

    Pulls author / title / description / non-noise fields and joins them
    with " | ". Skips obvious noise fields like "Download" links and the
    footer (which is usually metadata, not content). Returns "" if the
    embed has nothing useful (e.g. just an image).

    This is what lets /ask see the ingestion-feed posts — those are
    embed-only messages with `msg.content == ""`, so the original
    helper skipped them and the bot saw an empty channel.
    """
    parts: list[str] = []
    author_name = getattr(getattr(embed, "author", None), "name", None)
    if author_name:
        parts.append(author_name)
    if embed.title:
        parts.append(embed.title)
    if embed.description:
        parts.append(embed.description)
    for field in (embed.fields or []):
        name = (field.name or "").strip()
        value = (field.value or "").strip()
        if not name or not value:
            continue
        lname = name.lower()
        # Drop pure-URL fields and metadata noise that don't help the LLM.
        if "download" in lname or "open pdf" in lname or "link" in lname:
            continue
        parts.append(f"{name}: {value}")
    return " | ".join(p for p in parts if p).strip()


async def _fetch_chat_context(
    channel,
    *,
    exclude_message_id: int | None = None,
    bot_user_id: int | None = None,
) -> str:
    """Fetch recent channel messages and format them as an LLM context block.

    Returns a chronologically-ordered "username: text" block of up to
    _ASK_CONTEXT_MAX_MESSAGES messages, capped at _ASK_CONTEXT_MAX_AGE_MIN
    minutes old. Each message is truncated to _ASK_CONTEXT_PER_MSG_CHARS
    chars so a single long rant can't blow the token budget.

    For embed-only messages (e.g. the ingestion feed's bot-posted research
    cards), text is flattened from the embed's author/title/description/
    fields via _extract_embed_text. Without this, the helper would skip
    them entirely and the bot would think the channel was empty.

    Returns (block_text, author_ids) — empty string + empty list on any
    failure or when there's nothing usable. Empty-string fall-through is
    intentional — the caller treats it as "no context, proceed normally."

    `author_ids` is the set of distinct user IDs seen in the context window,
    excluding the bot itself. Used by /ask to fetch personality profiles
    for the people active in this conversation.

    `exclude_message_id` is the @mention message itself when invoked from
    on_message — we don't want to feed the bot its own prompt as context.
    """
    if channel is None:
        return "", []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_ASK_CONTEXT_MAX_AGE_MIN)
    collected: list[tuple[datetime, str]] = []
    author_ids: set[int] = set()
    try:
        async for msg in channel.history(limit=_ASK_CONTEXT_MAX_MESSAGES):
            # discord.py timestamps are tz-aware UTC; cutoff is too.
            if msg.created_at < cutoff:
                continue
            if exclude_message_id is not None and msg.id == exclude_message_id:
                continue
            text = (msg.content or "").strip()
            if not text and msg.embeds:
                # Embed-only message (e.g. ingestion feed cards). Flatten
                # the embeds into a single line so the LLM can still read it.
                embed_lines = [_extract_embed_text(e) for e in msg.embeds]
                text = " | ".join(t for t in embed_lines if t).strip()
            if not text:
                continue  # nothing usable — pure image / sticker / etc.
            text = text[:_ASK_CONTEXT_PER_MSG_CHARS]
            # Tag the bot's own past replies distinctly so Gemini can recognize
            # which lines are its prior output. Without this, the bot sees its
            # own embed-stripped replies as "BotName: <text>" and treats them
            # like any other user — leading to loops where it repeats the same
            # canned take across multiple calls without realizing it.
            if bot_user_id is not None and msg.author.id == bot_user_id:
                line = f"[YOU said earlier]: {text}"
            else:
                # Render as "DisplayName (username): text" so the model can
                # unambiguously match each speaker back to their WHO'S TALKING
                # profile entry (which is also keyed by username). When the
                # two are identical, drop the parens to keep the line short.
                dn = getattr(msg.author, "display_name", None) or msg.author.name
                uname = msg.author.name
                if dn and uname and dn.lower() != uname.lower():
                    speaker = f"{dn} ({uname})"
                else:
                    speaker = dn or uname
                line = f"{speaker}: {text}"
                # Track distinct non-bot authors for the profile lookup
                if not msg.author.bot:
                    author_ids.add(msg.author.id)
            collected.append((msg.created_at, line))
    except discord.Forbidden:
        log.info("Chat-context fetch: missing Read Message History permission")
        return "", []
    except Exception as e:
        log.warning(f"Chat-context fetch failed (non-fatal): {e}")
        return "", []
    if not collected:
        return "", []
    collected.sort(key=lambda t: t[0])  # oldest → newest
    body = "\n".join(line for _, line in collected)
    block = (
        "Recent channel chat (oldest → newest, for context only — "
        "the actual question follows after):\n"
        f"{body}"
    )
    return block, sorted(author_ids)


def _get_gemini_ask_client():
    """Lazy-init a google-genai client for /ask. Prefers a separate
    GOOGLE_ASK_API_KEY when present (lets /ask run on a free-tier account
    while the rest of the bot uses paid-tier billing), falls back to the
    main GOOGLE_API_KEY. Returns None when neither is set so the surface
    degrades gracefully."""
    global _gemini_ask_client
    if _gemini_ask_client is not None:
        return _gemini_ask_client
    key = settings.google_ask_api_key or settings.google_api_key
    if not key:
        return None
    try:
        from google import genai
        _gemini_ask_client = genai.Client(api_key=key)
        return _gemini_ask_client
    except Exception as e:
        log.error(f"Failed to init Gemini /ask client: {e}")
        return None


def _build_sources_footer(grounding_metadata) -> str:
    """Render Gemini's grounding_chunks as a Discord-friendly Sources list.

    Returns an empty string when there are no chunks. Format:

        Sources:
        [1] [Title](url)
        [2] [Title](url)
        ...

    Discord renders the inline-link markdown but suppresses the embed preview
    via the angle-bracket wrapper. We dedupe by URL because Gemini sometimes
    returns the same source twice when multiple supports cite it.
    """
    if grounding_metadata is None:
        return ""
    chunks = getattr(grounding_metadata, "grounding_chunks", None) or []
    seen: set[str] = set()
    lines: list[str] = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        if web is None:
            continue
        url = getattr(web, "uri", None)
        if not url or url in seen:
            continue
        seen.add(url)
        title = (getattr(web, "title", None) or url)[:80]
        lines.append(f"[{len(lines) + 1}] [{title}](<{url}>)")
        if len(lines) >= 2:
            break  # cap to keep the embed compact
    if not lines:
        return ""
    return "\n\nSources:\n" + "\n".join(lines)


async def _answer_with_gemini(
    question: str,
    user_id: int,
    chat_context: str = "",
    fetched_urls: str = "",
    images: list[tuple[bytes, str]] | None = None,
    profile_user_ids: list[int] | None = None,
    asker_display_name: str = "",
    asker_username: str = "",
    channel_name: str = "",
) -> discord.Embed:
    """Run a Gemini grounded-search query and return a Discord embed.

    Enforces the per-user daily cap. Returns a single embed with the answer
    + sources footer + NFA footer, or an error embed on failure.

    `chat_context` (optional) is a pre-formatted recent-channel-history block
    from `_fetch_chat_context`. When non-empty, it's prepended to the user's
    question so Gemini can reference what users were just discussing — useful
    for bro-mode roasts and follow-up research questions.
    """
    cap = settings.ask_daily_quota_per_user
    if cap > 0:
        used = db.count_ask_queries_today_for_user(user_id)
        if used >= cap:
            return discord.Embed(
                description=(
                    f"You've hit today's /ask cap ({cap} queries). "
                    f"Resets at UTC midnight."
                ),
                color=0xE67E22,
            )

    client = _get_gemini_ask_client()
    if client is None:
        return discord.Embed(
            description=(
                "/ask is not configured on this bot. Set the "
                "`GOOGLE_API_KEY` env var to enable web-search Q&A."
            ),
            color=0xE74C3C,
        )

    try:
        from google.genai import types
        config = types.GenerateContentConfig(
            system_instruction=_ASK_SYSTEM_INSTRUCTION,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            # max_output_tokens budgets TOTAL Gemini output (thinking + visible
            # response combined). 4000 gives generous headroom — thinking
            # can use up to ~2500-3000 on complex grounded queries and the
            # visible response still has ~1000+ tokens to finish cleanly.
            # The prompt's 300-word rule still binds the visible answer
            # soft; the high token cap exists to prevent cliff-truncation
            # when thinking overshoots, not to encourage long responses.
            max_output_tokens=4000,
            # 0.3 — pulled back from 0.4 after live answers were leaking
            # into preachy / lecturing territory and dunking on Abe's picks
            # at the higher swing. Lower variance + the new anti-lecture
            # rule together should keep Type 1 informational and Type 2/3
            # sharp without sprawling.
            temperature=0.3,
            thinking_config=types.ThinkingConfig(thinking_budget=1024),
        )
        # Compose the final user message:
        #   1. WHO'S TALKING — profiles for users active in this chat
        #   2. Analyst trade log (Abe's recent trades)
        #   3. Fetched URL contents (user-shared sources)
        #   4. Recent channel chat context
        #   5. Separator + actual question
        # Skip any section that's empty.
        profiles_block = ""
        try:
            if profile_user_ids:
                profiles_block = db.format_user_profiles_for_context(profile_user_ids)
        except Exception as e:
            log.warning(f"User-profile fetch failed (non-fatal): {e}")

        analyst_block = ""
        if settings.analyst_channel_name:
            try:
                analyst_block = db.format_analyst_trades_for_context(hours=168)
            except Exception as e:
                log.warning(f"Analyst log fetch failed (non-fatal): {e}")
        sections: list[str] = []
        if profiles_block:
            sections.append(profiles_block)
        if analyst_block:
            sections.append(analyst_block)
        if fetched_urls:
            sections.append(fetched_urls)
        if chat_context:
            sections.append(chat_context)
        # Explicit asker identification. The bot pulled WHO'S TALKING
        # profiles for everyone active in chat — without naming the
        # asker on the separator, the model has to guess from scrollback
        # who's asking, and sometimes addresses the wrong person.
        if asker_display_name or asker_username:
            who = asker_display_name or asker_username
            if asker_username and asker_display_name and \
                    asker_display_name.lower() != asker_username.lower():
                who = f"{asker_display_name} ({asker_username})"
            separator = f"--- {who} is asking: ---"
        else:
            separator = "--- The user is now asking: ---"
        sections.append(f"{separator}\n{question}")
        user_content = "\n\n".join(sections)

        # If images are present, build a multipart contents list: images
        # first (so the model sees them before reading the text question),
        # then the text. Without images, send plain text contents.
        if images:
            parts: list = []
            for img_bytes, mime in images:
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
            parts.append(types.Part.from_text(text=user_content))
            generate_contents = parts
        else:
            generate_contents = user_content

        ask_model = settings.ask_gemini_model or settings.gemini_model
        response = await client.aio.models.generate_content(
            model=ask_model,
            contents=generate_contents,
            config=config,
        )
        answer = (response.text or "").strip()
        grounding_metadata = None
        try:
            grounding_metadata = response.candidates[0].grounding_metadata
        except (AttributeError, IndexError, TypeError):
            pass
        sources_footer = _build_sources_footer(grounding_metadata)
        full = (answer + sources_footer)[:4000]
        db.record_ask_query(user_id)

        # QC log: append every interaction to /data/ask-logs/YYYY-MM-DD.md
        # so the daily publish job can push to GitHub for browseable review.
        # Failure is non-fatal — the user still gets their answer.
        try:
            db.append_ask_interaction(
                asker_display_name=asker_display_name,
                asker_username=asker_username,
                channel_name=channel_name,
                question=question,
                answer=full,
            )
        except Exception as e:
            log.warning(f"ask-log append failed (non-fatal): {e}")

        embed = discord.Embed(description=full, color=0x228B22)
        embed.set_footer(text="Hi, I'm AI-powered - NFA")
        return embed
    except Exception as e:
        log.error(f"Gemini /ask call failed: {e}", exc_info=True)
        err_str = str(e).lower()
        # Map common error classes to in-voice replies. Full exception is
        # logged above for debugging; users see only the short message.
        if "429" in err_str or "resource_exhausted" in err_str or "quota" in err_str:
            msg = "Easy — going too fast. Give it a minute and try again."
        elif "401" in err_str or "403" in err_str or "unauthorized" in err_str or "permission" in err_str:
            msg = "Config issue on the API key — admin needs to check it."
        elif "500" in err_str or "503" in err_str or "timeout" in err_str or "unavailable" in err_str:
            msg = "Google's hiccuping. Try again in a sec."
        elif "400" in err_str or "invalid" in err_str:
            msg = "Something about that question broke the model. Try rephrasing."
        else:
            msg = "Something broke on my end. Try again in a sec."
        return discord.Embed(description=msg, color=0xE74C3C)


def _fmt_ts(iso_str: str | None) -> str:
    """Format a UTC ISO timestamp in the configured display timezone."""
    if not iso_str:
        return "never"
    try:
        ts = iso_str[:19]  # strip microseconds/timezone suffix
        dt = datetime.fromisoformat(ts).replace(tzinfo=pytz.UTC)
        local = dt.astimezone(_display_tz)
        return local.strftime("%Y-%m-%d %H:%M %Z")
    except (ValueError, TypeError):
        return iso_str[:16].replace("T", " ")


async def _check_pulse_channel(interaction: discord.Interaction) -> bool:
    """Reject pulse/admin commands invoked outside the allowed channels.

    Returns True if the interaction may proceed, False if it was rejected
    (and the rejection message was already sent ephemerally).

    Allowlist comes from settings.pulse_command_channel_names (channel
    names, lowercase). Empty allowlist = unrestricted (return True).
    /ask intentionally does NOT call this — it's open in every channel.
    """
    allowed = settings.pulse_command_channel_names
    if not allowed:
        return True
    chan = interaction.channel
    chan_name = getattr(chan, "name", None) or ""
    if chan_name.lower() in allowed:
        return True
    pretty = ", ".join(f"#{c}" for c in allowed)
    try:
        await interaction.response.send_message(
            f"This command is only available in {pretty}. /ask works in any channel.",
            ephemeral=True,
        )
    except Exception:
        pass
    return False


def _safe_json(s: str | None) -> list:
    """Parse a JSON list field defensively. Returns [] on any failure
    (None, malformed JSON, non-list payload). Used for reanalyze_jobs
    JSON columns where empty/null is normal."""
    import json as _json
    if not s:
        return []
    try:
        v = _json.loads(s)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def create_bot() -> commands.Bot:
    """Create and configure the Discord bot."""
    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def on_ready():
        log.info(f"Discord bot connected as {bot.user}")
        try:
            synced = await bot.tree.sync()
            log.info(f"Synced {len(synced)} slash commands")
        except Exception as e:
            log.error(f"Failed to sync commands: {e}")
        # One-shot ingestion-feed backfill check after bot is connected
        try:
            from discord_bot.ingestion_feed import announce_startup_backfill, feed_enabled
            if feed_enabled():
                await announce_startup_backfill(bot)
        except Exception as e:
            log.error(f"Ingestion feed startup backfill failed: {e}", exc_info=True)

    # --- DISABLED in slash menu (2026-05-14) ----------------------------------
    # /pulse is no longer registered with Discord's command tree. Manual pulses
    # were rarely used by non-admin users and cluttered the picker. The function
    # body is preserved verbatim — to re-expose it, simply uncomment the two
    # decorator lines below. The internal pipeline (`run_manual_pulse`) is still
    # callable from /reanalyze, the scheduled job, and the bridge worker.
    # @bot.tree.command(name="pulse", description="Generate a Market Pulse from analyses in the window")
    # @app_commands.describe(
    #     hours="Optional: how many hours back to look (default: since last scheduled pulse, or 24h). Max 168 (1 week).",
    # )
    async def pulse_command(interaction: discord.Interaction, hours: int | None = None):
        if not await _check_pulse_channel(interaction):
            return
        if hours is not None and (hours < 1 or hours > 168):
            await interaction.response.send_message("Hours must be between 1 and 168.")
            return
        await interaction.response.defer(thinking=True)

        try:
            from datetime import datetime, timedelta
            from pipeline.orchestrator import run_manual_pulse

            parsed_since = None
            if hours:
                parsed_since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

            label = f" (last {hours}h)" if hours else ""
            status_msg = await interaction.followup.send(f"Starting pulse{label}…")

            async def on_progress(phase: str, detail: str):
                try:
                    await status_msg.edit(content=f"**/pulse{label}** — {detail}")
                except Exception:
                    pass

            report = await run_manual_pulse(since=parsed_since, progress_cb=on_progress)

            if report:
                from report.formatter import format_report_embeds
                try:
                    await status_msg.edit(content=f"**/pulse** — Posting {report.pdf_count}-report pulse to channel…")
                except Exception:
                    pass
                embeds = format_report_embeds(report)
                success = await send_embeds(interaction.channel, embeds)
                if success and report.report_id:
                    db.mark_report_sent(report.report_id)
                try:
                    await status_msg.edit(
                        content=f"Market Pulse generated from {report.pdf_count} reports."
                    )
                except Exception:
                    pass
            else:
                try:
                    await status_msg.edit(
                        content="No analyses available. Run `/load 24` first to ingest recent PDFs."
                    )
                except Exception:
                    await interaction.followup.send(
                        "No analyses available. Run `/load 24` first to ingest recent PDFs."
                    )
        except Exception as e:
            log.error(f"Manual pulse failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error generating pulse: {str(e)[:200]}")

    # --- DISABLED in slash menu (2026-05-14) ----------------------------------
    # /load is unregistered. Dropbox is polled every 15 minutes automatically
    # by `scheduler.jobs.dropbox_poll_job`, so manual ingestion is rarely
    # needed. To re-expose, uncomment the two decorator lines below.
    # @bot.tree.command(name="load", description="Ingest + analyze PDFs uploaded to Dropbox in the last N hours")
    # @app_commands.describe(
    #     hours="How many hours of recent PDFs to load (max 48)",
    #     password="Admin password",
    # )
    async def load_command(interaction: discord.Interaction, hours: int, password: str):
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        if hours < 1 or hours > 48:
            await interaction.response.send_message("Hours must be between 1 and 48.")
            return
        await interaction.response.defer(thinking=True)

        try:
            from pipeline.orchestrator import ingest_recent_pdfs

            status_msg = await interaction.followup.send(f"Starting load ({hours}h window)…")

            async def on_progress(stats: dict, phase: str):
                if phase == "listing":
                    content = f"Listing Dropbox files for last {hours}h…"
                elif phase == "processing":
                    processed_or_failed = stats["processed"] + stats["failed"]
                    new = stats["new"]
                    if new == 0:
                        content = f"Found {stats['found']} files, 0 new to process."
                    else:
                        pct = int((processed_or_failed / new) * 100) if new else 0
                        current = stats.get("current_file", "")
                        recent = stats.get("recent_files", [])
                        content = (
                            f"**Loading ({hours}h window)** — {processed_or_failed}/{new} done ({pct}%)\n"
                            f"Processed: {stats['processed']} | Failed: {stats['failed']} | "
                            f"Low skipped: {stats['skipped_low']}\n"
                            f"Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out"
                        )
                        if current:
                            content += f"\n\n**Now:** {current[:80]}"
                        if recent:
                            content += f"\n**Recent:**\n" + "\n".join(recent[-5:])
                        # Discord message limit is 2000 chars
                        content = content[:1900]
                else:  # done
                    content = (
                        f"**Load complete ({hours}h window)**\n"
                        f"Found: {stats['found']} | New: {stats['new']} | "
                        f"Processed: {stats['processed']} | Low (skipped deep): {stats['skipped_low']} | "
                        f"Failed: {stats['failed']}\n"
                        f"Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out\n"
                        f"Run `/pulse` to synthesize a report."
                    )
                try:
                    await status_msg.edit(content=content)
                except Exception:
                    pass  # don't let display errors break the load

            await ingest_recent_pdfs(hours, progress_cb=on_progress)
        except Exception as e:
            log.error(f"Load failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error loading PDFs: {str(e)[:200]}")

    @bot.tree.command(name="reanalyze", description="Re-run analysis on PDFs already in DB using the current prompt")
    @app_commands.describe(
        hours="Re-analyze PDFs uploaded in the last N hours (max 168)",
        password="Admin password",
        priority="Filter by priority (default: high+medium, skips LOW). Options: high, medium, low, all",
    )
    async def reanalyze_command(
        interaction: discord.Interaction,
        hours: int,
        password: str,
        priority: str = "high+medium",
    ):
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        if hours < 1 or hours > 168:
            await interaction.response.send_message("Hours must be between 1 and 168.")
            return

        # Resolve priority filter
        priority_filter: list[str] | None
        priority_lc = (priority or "").strip().lower()
        if priority_lc in ("", "all"):
            priority_filter = None
            filter_label = "all priorities"
        elif priority_lc in ("high+medium", "high,medium", "high+med", "hm"):
            priority_filter = ["high", "medium"]
            filter_label = "HIGH+MEDIUM only (LOW skipped)"
        elif priority_lc in ("high", "h"):
            priority_filter = ["high"]
            filter_label = "HIGH only"
        elif priority_lc in ("medium", "med", "m"):
            priority_filter = ["medium"]
            filter_label = "MEDIUM only"
        elif priority_lc in ("low", "l"):
            priority_filter = ["low"]
            filter_label = "LOW only"
        else:
            await interaction.response.send_message(
                f"Invalid priority '{priority}'. Use one of: high+medium (default), high, medium, low, all.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        try:
            # Build target-PDF list now so the job row has an immutable
            # snapshot (subsequent Dropbox uploads won't drift the target).
            from datetime import datetime, timedelta
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            conn = db.get_connection()
            if priority_filter:
                placeholders = ",".join("?" * len(priority_filter))
                rows = conn.execute(
                    f"""SELECT id FROM pdf_files
                        WHERE dropbox_modified_at > ?
                          AND LOWER(priority) IN ({placeholders})
                        ORDER BY dropbox_modified_at ASC""",
                    (cutoff, *[p.lower() for p in priority_filter]),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id FROM pdf_files
                       WHERE dropbox_modified_at > ?
                       ORDER BY dropbox_modified_at ASC""",
                    (cutoff,),
                ).fetchall()
            target_ids = [int(r["id"]) for r in rows]

            if not target_ids:
                await interaction.followup.send(
                    f"No PDFs in the {hours}h window matching `{filter_label}` — nothing to reanalyze."
                )
                return

            # Refuse to enqueue if another job is already active. One
            # reanalyze at a time — the scheduler processes serially and
            # multiple queued jobs would just queue behind the active one
            # without obvious feedback.
            active = db.get_active_reanalyze_job()
            if active is not None:
                await interaction.followup.send(
                    f"⚠️ Reanalyze job #{active['id']} is already "
                    f"`{active['status']}` ({active['target_count']} target PDFs). "
                    f"Wait for it to complete, then run /reanalyze again. "
                    f"Check `/status` for progress."
                )
                return

            # Post the initial status message so we can edit it later.
            status_msg = await interaction.followup.send(
                f"**Reanalyze queued** ({hours}h window, {filter_label})\n"
                f"Target: {len(target_ids)} PDFs — will start within ~60s on the "
                f"background scheduler.\n"
                f"This job is **persistent**: progress saved to DB after each PDF, "
                f"so a worker restart won't lose your place. The Discord 15-min "
                f"interaction limit no longer matters — completion message will "
                f"be posted to this channel when done."
            )

            # Create the job row. The scheduler's reanalyze_processor will
            # pick it up on its next 60s tick.
            requested_by = str(interaction.user.id) if interaction.user else None
            channel_id = interaction.channel_id
            job_id = db.create_reanalyze_job(
                hours=hours,
                target_pdf_ids=target_ids,
                priority_filter=priority_filter,
                requested_by=requested_by,
                discord_channel_id=channel_id,
                discord_status_message_id=status_msg.id if status_msg else None,
            )
            log.info(
                f"Reanalyze job {job_id} queued: {len(target_ids)} PDFs, "
                f"hours={hours}, filter={priority_filter}, channel={channel_id}"
            )
            try:
                await status_msg.edit(content=(
                    f"**Reanalyze job #{job_id} queued** ({hours}h window, {filter_label})\n"
                    f"Target: {len(target_ids)} PDFs — scheduler will start it within ~60s.\n"
                    f"Progress persisted to DB; check `/status` any time. "
                    f"Final completion message will replace this when done."
                ))
            except Exception:
                pass
        except Exception as e:
            log.error(f"Reanalyze enqueue failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    # --- DISABLED in slash menu (2026-05-14) ----------------------------------
    # /clearqueue is a destructive admin tool that should not be in everyone's
    # picker. Function preserved — uncomment the decorators below if you need
    # to purge a stuck queue. (Alternatively, run db.clear_pending_queue()
    # directly via `railway ssh`.)
    # @bot.tree.command(name="clearqueue", description="Delete pending (DOWNLOADED) PDFs from the queue — destructive, cancels backlog")
    # @app_commands.describe(
    #     password="Admin password",
    #     confirm="Set true to skip the >500 safety check for large purges",
    # )
    async def clearqueue_command(interaction: discord.Interaction, password: str, confirm: bool = False):
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            pending = db.count_pending_queue()
            if pending == 0:
                await interaction.followup.send("Queue is already empty — nothing to clear.")
                return
            if pending > 500 and not confirm:
                await interaction.followup.send(
                    f"⚠️ {pending:,} pending PDFs — this is a large purge. "
                    f"Re-run with `confirm:True` to proceed."
                )
                return

            count = db.clear_pending_queue()
            await interaction.followup.send(
                f"Cleared **{count:,}** pending PDFs from the queue. "
                f"Process job will idle until new uploads arrive."
            )
        except Exception as e:
            log.error(f"Clear queue failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    # --- DISABLED in slash menu (2026-05-14) ----------------------------------
    # /seedcursor is a one-shot recovery tool used after Dropbox-cursor
    # mishaps. Not needed in normal operation. Uncomment to re-expose.
    # @bot.tree.command(name="seedcursor", description="Seed Dropbox cursor to current state (skips backfill on next poll)")
    # @app_commands.describe(password="Admin password")
    async def seedcursor_command(interaction: discord.Interaction, password: str):
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            from pipeline.orchestrator import seed_dropbox_cursor_to_now
            ts = seed_dropbox_cursor_to_now()
            await interaction.followup.send(
                f"Dropbox cursor seeded at `{_fmt_ts(ts)}`. "
                "Next 15-min poll will only pick up NEW uploads."
            )
        except Exception as e:
            log.error(f"Seed cursor failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.tree.command(name="status", description="Show pipeline health and DB state")
    async def status_command(interaction: discord.Interaction):
        if not await _check_pulse_channel(interaction):
            return
        today = db.get_today_stats()
        full = db.get_pipeline_stats()

        embed = discord.Embed(
            title="Pipeline Status",
            description="PDFs are processed then deleted from disk. Only analysis JSON is stored in DB.",
            color=0x3498DB,
        )

        # Today
        embed.add_field(
            name="Today",
            value=(
                f"Ingested: **{today['total']}** | "
                f"Processed: **{today['processed']}** | "
                f"Pending: **{today['pending']}** | "
                f"Failed: **{today['failed']}**"
            ),
            inline=False,
        )

        # All-time DB state
        status_parts = [f"{s}: {c}" for s, c in full["status_counts"].items()]
        embed.add_field(
            name=f"Total in DB ({full['total_pdfs']} PDFs)",
            value=" | ".join(status_parts) or "empty",
            inline=False,
        )

        # Upload volume windows — what would feed a pulse right now
        lines = [f"Last 24h: **{full.get('uploads_last_24h', 0)}** uploaded"]
        since_last = full.get("uploads_since_last_scheduled")
        if since_last is not None:
            lines.append(f"Since last scheduled pulse: **{since_last}** uploaded")
        else:
            lines.append("Since last scheduled pulse: n/a (no scheduled pulse yet)")
        embed.add_field(
            name="Upload volume (by Dropbox upload time)",
            value="\n".join(lines),
            inline=False,
        )

        # Priority breakdown — always show all three so zeros are visible
        priority_counts = full.get("priority_counts") or {}
        pri_parts = [f"{p}: {priority_counts.get(p, 0)}" for p in ("high", "medium", "low")]
        embed.add_field(
            name="Priority mix",
            value=" | ".join(pri_parts),
            inline=False,
        )

        # Upload date range — tells user how far back the analyses reach
        if full["earliest_upload"] and full["latest_upload"]:
            embed.add_field(
                name="Upload range in DB",
                value=f"Earliest: `{_fmt_ts(full['earliest_upload'])}`\nLatest: `{_fmt_ts(full['latest_upload'])}`",
                inline=False,
            )

        # Tokens all-time
        embed.add_field(
            name="Tokens (all-time)",
            value=f"In: {full['input_tokens']:,} | Out: {full['output_tokens']:,}",
            inline=False,
        )

        # Opus-bridge ingestion stats (last 24h) — only show if backend
        # is set to opus_bridge OR there's any historical bridge activity.
        from datetime import datetime, timedelta
        bridge_cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
        bridge = db.count_bridge_outcomes_since(bridge_cutoff)
        if settings.high_ingestion_backend == "opus_bridge" or bridge["total"] > 0:
            backend = settings.high_ingestion_backend
            n_total = bridge["total"]
            n_completed = bridge["completed"]
            n_fallback = bridge["fallback_to_gemini"]
            n_pending = bridge["pending"] + bridge["committed"]
            n_failed = bridge["failed"]
            success_rate = (
                f"{100 * n_completed / n_total:.0f}%"
                if n_total else "n/a"
            )
            embed.add_field(
                name=f"Opus bridge — last 24h (backend={backend})",
                value=(
                    f"Total: **{n_total}** | Completed via Opus: **{n_completed}** ({success_rate})\n"
                    f"Fallback to Gemini: **{n_fallback}** | In-flight: **{n_pending}** | Hard failed: **{n_failed}**"
                ),
                inline=False,
            )

        # Pulse history
        pulse_lines = []
        if full["last_daily_pulse"]:
            d = full["last_daily_pulse"]
            sent = "sent" if d["discord_sent_at"] else "NOT sent"
            pulse_lines.append(f"**Last scheduled:** {_fmt_ts(d['created_at'])} ({d['pdf_count']} PDFs, {sent})")
        else:
            pulse_lines.append("**Last scheduled:** never")
        if full["last_manual_pulse"]:
            m = full["last_manual_pulse"]
            pulse_lines.append(f"**Last manual:** {_fmt_ts(m['created_at'])} ({m['pdf_count']} PDFs)")
        embed.add_field(name="Pulses", value="\n".join(pulse_lines), inline=False)

        # Dropbox state
        cursor_state = "✅ seeded" if full["cursor_set"] else "❌ unset (next poll will backfill!)"
        embed.add_field(
            name="Dropbox watcher",
            value=f"Cursor: {cursor_state}\nLast poll: `{_fmt_ts(full['last_poll_at'])}`",
            inline=False,
        )

        # Last 5 PDFs ingested
        recent = full.get("recent_pdfs") or []
        if recent:
            lines = []
            for r in recent:
                ts = _fmt_ts(r.get("created_at"))
                pri = (r.get("priority") or "-").lower()
                name = (r.get("file_name") or "")[:55]
                lines.append(f"`{ts}` · **{pri}** · {name}")
            embed.add_field(
                name="Last 5 ingested",
                value="\n".join(lines)[:1024],  # Discord field limit
                inline=False,
            )

        # Reanalyze jobs — surface active/recent so the user can see if a
        # /reanalyze is in flight, queued, or recently completed without
        # spelunking the DB.
        recent_jobs = db.get_recent_reanalyze_jobs(limit=3)
        if recent_jobs:
            lines = []
            for j in recent_jobs:
                done = (
                    len(_safe_json(j.get("processed_pdf_ids")))
                    + len(_safe_json(j.get("failed_pdf_ids")))
                    + len(_safe_json(j.get("bridge_queued_pdf_ids")))
                )
                tot = j.get("target_count") or 0
                pct = int(100 * done / tot) if tot else 0
                created = _fmt_ts(j.get("created_at"))
                lines.append(
                    f"`#{j['id']}` `{created}` · **{j['status']}** · "
                    f"{done}/{tot} ({pct}%) · {j['hours']}h"
                )
            embed.add_field(
                name="Reanalyze jobs (recent 3)",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    # --- DISABLED in slash menu (2026-05-14) ----------------------------------
    # /reprocess retries a single failed PDF by filename. Built-in scheduler
    # auto-retries failed PDFs up to MAX_RETRY_COUNT, so manual reprocess is
    # rarely needed. Uncomment to re-expose.
    # @bot.tree.command(name="reprocess", description="Retry a failed PDF by filename")
    # @app_commands.describe(filename="The PDF filename to reprocess")
    async def reprocess_command(interaction: discord.Interaction, filename: str):
        if not await _check_pulse_channel(interaction):
            return
        await interaction.response.defer(thinking=True)

        try:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT * FROM pdf_files WHERE file_name LIKE ? AND status = 'FAILED'",
                (f"%{filename}%",),
            ).fetchone()

            if not row:
                await interaction.followup.send(f"No failed PDF found matching '{filename}'")
                return

            pdf_data = dict(row)
            db.update_pdf_status(pdf_data["id"], "DOWNLOADED")

            from pipeline.orchestrator import process_single_pdf
            result = await process_single_pdf(pdf_data)

            if result:
                await interaction.followup.send(
                    f"Reprocessed '{pdf_data['file_name']}' successfully. "
                    f"Priority: {result.priority}, Source: {result.source}"
                )
            else:
                await interaction.followup.send(f"Reprocessing '{pdf_data['file_name']}' failed.")
        except Exception as e:
            log.error(f"Reprocess failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.tree.command(name="ask", description="Gemini powered")
    async def ask_command(interaction: discord.Interaction, question: str):
        question = (question or "").strip()
        if not question:
            await interaction.response.send_message("Ask a question first.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            user_id = interaction.user.id if interaction.user else 0
            chat_context, chat_author_ids = await _fetch_chat_context(
                interaction.channel,
                bot_user_id=bot.user.id if bot.user else None,
            )
            fetched_urls = await _maybe_fetch_user_urls(question)
            # Profile lookup: asker + recent chat speakers + anyone the
            # question text mentions by name or @-mention. The last one
            # is critical — users will ask "who is zhawk" or "is mike
            # still in NVDA" about people who haven't posted in the
            # current channel recently, and we need their profile.
            mentioned_ids = []
            try:
                mentioned_ids = db.find_users_mentioned_in_text(question)
            except Exception as e:
                log.warning(f"Name-mention lookup failed: {e}")
            profile_ids = list(set(
                chat_author_ids + ([user_id] if user_id else []) + mentioned_ids
            ))
            asker = interaction.user
            embed = await _answer_with_gemini(
                question,
                user_id,
                chat_context=chat_context,
                fetched_urls=fetched_urls,
                profile_user_ids=profile_ids,
                asker_display_name=(
                    getattr(asker, "display_name", None)
                    or getattr(asker, "name", "")
                    or ""
                ),
                asker_username=getattr(asker, "name", "") or "",
                channel_name=getattr(interaction.channel, "name", "") or "",
            )
            await interaction.followup.send(embed=embed)
        except Exception as e:
            log.error(f"/ask failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.event
    async def on_message(message: discord.Message):
        # Ignore the bot's own messages + other bots.
        if message.author.bot:
            return

        # Dispatch to the analyst-log watcher if this message is in the
        # configured analyst alerts channel. Runs side-by-side with the
        # @mention handling below — a message in the analyst channel that
        # also @mentions the bot would trigger both flows independently.
        try:
            if (settings.analyst_channel_name
                    and getattr(message.channel, "name", None) == settings.analyst_channel_name):
                from analyst_log.watcher import watch_message
                await watch_message(bot, message)
        except Exception as e:
            log.error(f"Analyst watcher dispatch failed: {e}", exc_info=True)

        # Only respond when the bot is explicitly @-mentioned.
        if bot.user is None or bot.user not in message.mentions:
            await bot.process_commands(message)
            return
        # Strip the mention(s) from the content to get the actual question.
        content = message.content or ""
        for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            content = content.replace(mention, "")
        question = content.strip()
        if not question:
            await message.reply(
                "Mention me with a question and I'll search the web for you.",
                mention_author=False,
            )
            return
        try:
            async with message.channel.typing():
                chat_context, chat_author_ids = await _fetch_chat_context(
                    message.channel,
                    exclude_message_id=message.id,
                    bot_user_id=bot.user.id if bot.user else None,
                )
                fetched_urls = await _maybe_fetch_user_urls(question)
                # Add anyone the @mention message references in text
                # (discord @-mentions or known display_names) so the bot
                # has their profile even if they're not in recent chat.
                mentioned_ids = []
                try:
                    mentioned_ids = db.find_users_mentioned_in_text(question)
                    for u in (message.mentions or []):
                        if not u.bot and u.id != message.author.id:
                            mentioned_ids.append(u.id)
                except Exception as e:
                    log.warning(f"Name-mention lookup failed: {e}")
                profile_ids = list(set(
                    chat_author_ids + [message.author.id] + mentioned_ids
                ))

                # Scoped image collection: only the @mention message and
                # the message it's replying to (if any). Cap at 2 total.
                images = await _extract_images_from_message(
                    message,
                    remaining_slots=_IMAGE_MAX_PER_CALL,
                )
                if message.reference and message.reference.message_id and len(images) < _IMAGE_MAX_PER_CALL:
                    try:
                        ref_msg = await message.channel.fetch_message(
                            message.reference.message_id
                        )
                        more_imgs = await _extract_images_from_message(
                            ref_msg,
                            remaining_slots=_IMAGE_MAX_PER_CALL - len(images),
                        )
                        images.extend(more_imgs)
                    except Exception as e:
                        log.info(f"/ask: couldn't fetch replied-to message: {e}")

                embed = await _answer_with_gemini(
                    question,
                    message.author.id,
                    chat_context=chat_context,
                    fetched_urls=fetched_urls,
                    images=images,
                    profile_user_ids=profile_ids,
                    asker_display_name=(
                        getattr(message.author, "display_name", None)
                        or message.author.name
                    ),
                    asker_username=message.author.name,
                    channel_name=getattr(message.channel, "name", "") or "",
                )
                await message.reply(embed=embed, mention_author=False)
        except Exception as e:
            log.error(f"@mention /ask failed: {e}", exc_info=True)
            try:
                await message.reply(f"Error: {str(e)[:200]}", mention_author=False)
            except Exception:
                pass
        await bot.process_commands(message)

    return bot
