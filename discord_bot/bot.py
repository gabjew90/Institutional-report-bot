"""Discord bot client with slash commands."""

import asyncio
import html
import io
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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
_ASK_CONTEXT_MAX_MESSAGES = 50
_ASK_CONTEXT_MAX_AGE_MIN = 1440  # 24h — quiet channels (ingestion feed)
                                 # can take a while to fill the buffer
_ASK_CONTEXT_PER_MSG_CHARS = 600


# /ask system prompt — extracted to discord_bot/ask_prompt.py in the
# 2026-07-27 diet (v6: same rules, ~70%% fewer chars; the incident
# ledger and version history live there). The import keeps
# `bot._ASK_SYSTEM_INSTRUCTION` addressable for the contract + diet
# smokes and downstream tooling.
from discord_bot.ask_prompt import _ASK_SYSTEM_INSTRUCTION  # noqa: E402


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


async def _resolve_mentions_in_text(bot, guild, text: str) -> str:
    """Replace raw `<@USER_ID>` / `<@!USER_ID>` Discord mentions in `text`
    with readable `@DisplayName (username)` form so Gemini can match them
    against the WHO'S TALKING block (also keyed by username) and the
    chat-context speaker labels.

    Without this, the bot sees an opaque numeric ID in the question and
    can't tell which profile in WHO'S TALKING corresponds to it — leading
    to drift like "asker tagged @BK → bot answered about Abe."

    Tries the guild member cache first (has nicknames), falls back to the
    user cache, then an API fetch. Any unresolvable ID is left as-is.
    """
    import re
    if not text or "<@" not in text:
        return text

    pattern = re.compile(r"<@!?(\d+)>")
    user_ids: list[int] = []
    for m in pattern.finditer(text):
        try:
            user_ids.append(int(m.group(1)))
        except ValueError:
            continue
    if not user_ids:
        return text

    id_to_name: dict[int, str] = {}
    for uid in set(user_ids):
        member = guild.get_member(uid) if guild else None
        user = member or (bot.get_user(uid) if bot else None)
        if user is None and bot is not None:
            try:
                user = await bot.fetch_user(uid)
            except Exception:
                user = None
        if user is None:
            continue
        dn = getattr(user, "display_name", None) or getattr(user, "name", "") or ""
        uname = getattr(user, "name", "") or ""
        if dn and uname and dn.lower() != uname.lower():
            id_to_name[uid] = f"@{dn} ({uname})"
        else:
            id_to_name[uid] = f"@{dn or uname}"

    def _sub(match):
        try:
            uid = int(match.group(1))
        except ValueError:
            return match.group(0)
        return id_to_name.get(uid, match.group(0))

    return pattern.sub(_sub, text)


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
_PDF_MAX_BYTES = 10 * 1024 * 1024   # 10MB per PDF (Gemini reads PDFs inline)
_IMAGE_FETCH_TIMEOUT_S = 5.0
_IMAGE_MAX_PER_CALL = 2


async def _extract_images_from_message(
    msg: discord.Message,
    *,
    remaining_slots: int,
) -> list[tuple[bytes, str]]:
    """Pull (bytes, mime_type) tuples from a message's attachments and
    embed images. Accepts image/* AND application/pdf (Gemini reads both
    inline); caps images at 5MB, PDFs at 10MB; skips everything else.
    Failures are logged and swallowed — media enrichment is best-effort,
    never blocks the reply.
    """
    if remaining_slots <= 0 or msg is None:
        return []
    out: list[tuple[bytes, str]] = []

    # Direct attachments (uploads) — preferred path, read() returns bytes.
    for att in msg.attachments:
        if len(out) >= remaining_slots:
            break
        ct = (att.content_type or "").lower()
        is_pdf = ct.startswith("application/pdf")
        if not (ct.startswith("image/") or is_pdf):
            continue
        cap = _PDF_MAX_BYTES if is_pdf else _IMAGE_MAX_BYTES
        if att.size and att.size > cap:
            log.info(f"/ask attachment skipped — too big ({att.size} bytes)")
            continue
        try:
            data = await att.read()
            out.append((data, ct))
        except Exception as e:
            log.info(f"/ask attachment read failed: {e}")

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


_VERBATIM_CONTEXT_MSG_LIMIT = 50      # how many of the asker's own recent msgs to inject
_VERBATIM_CONTEXT_PER_MSG_CHARS = 200  # truncate each msg to this many chars


def _format_asker_verbatim_block(username: str) -> str:
    """Build a context block of the asker's last N verbatim messages
    from chat_messages, formatted for the model to quote when needed.

    Used by the @mention flow to inject the asker's actual recent
    chat into the prompt. The model uses this for two purposes:
      1. Quoting verbatim when the asker challenges a prior claim
         ("show me where I said that" — the rule #2 in the system
         prompt directs the model here)
      2. Grounding any claims it makes about the asker's recent
         behavior in real quoted lines instead of paraphrased
         summaries

    Returns empty string when nothing's on file for the user (lurker,
    new joiner, or chat_messages not yet populated for them) — caller
    can safely concatenate.
    """
    if not username:
        return ""
    try:
        rows = db.get_recent_user_messages(
            username, limit=_VERBATIM_CONTEXT_MSG_LIMIT,
        )
    except Exception as e:
        log.warning(f"Verbatim-context lookup failed for {username}: {e}")
        return ""
    if not rows:
        return ""
    # Newest-first from the helper; reverse to chronological so the
    # model reads them in the natural order things were said.
    rows = list(reversed(rows))
    lines: list[str] = [
        "[ASKER'S RECENT VERBATIM MESSAGES — for accurate quoting if",
        "they challenge a prior claim or you reference their behavior.",
        "These are the asker's own words, copied directly from chat.",
        "When asked 'show me where I said that' or similar, you can",
        "quote these LINE FOR LINE. Don't invent new specifics.]",
        "",
    ]
    for r in rows:
        content = (r.get("content") or "").strip()
        if not content:
            continue
        # Single-line each so the block stays parseable
        content = content.replace("\n", " ")
        if len(content) > _VERBATIM_CONTEXT_PER_MSG_CHARS:
            content = content[:_VERBATIM_CONTEXT_PER_MSG_CHARS] + "…"
        ts = (r.get("posted_at") or "")[:16]  # YYYY-MM-DD HH:MM
        ch = r.get("channel_name") or ""
        lines.append(f"  {ts} #{ch} — {content}")
    return "\n".join(lines)


_SUBJECT_VERBATIM_USERS_PER_CALL = 3   # cap how many subjects we look up
_SUBJECT_VERBATIM_MSG_LIMIT = 25       # fewer than asker (whose history is more relevant)


# `fc <ticker> <args>` (and `fcb`, etc.) are CHART COMMANDS to the
# room's charting bot — mechanical requests, not conversation. Left in
# a verbatim-quote block they read as the person's "content": 2026-07-12
# the bot characterized abe's channel as "a steady stream of 'fc'
# alerts" off five chart-pull lines in his quote block. Filter them out
# of subject-verbatim (the "this is their voice" signal); the recent-
# chat block keeps them (they're part of the room's live texture) with
# a lexicon rule in the system prompt explaining what they are.
_FC_COMMAND_RE = re.compile(
    r"^fc[a-z]?\s+[\w$.\s%@<>#:/()-]{1,60}$", re.IGNORECASE,
)


def _format_subject_verbatim_block(
    user_ids: list[int],
    *,
    exclude_user_id: int | None = None,
) -> str:
    """Same shape as _format_asker_verbatim_block but for OTHER users
    the question references. When someone asks 'what did BK say about
    CRCL,' we want BK's verbatim chat too — not just the asker's.

    Resolves Discord user_ids to usernames via user_profiles (the bot's
    canonical lookup), then pulls the last N messages from chat_messages
    per subject. Skips the asker (already injected by the asker-verbatim
    block) and caps to top _SUBJECT_VERBATIM_USERS_PER_CALL by chat
    volume so token budget stays bounded.

    Returns empty string when no subjects to surface — caller can safely
    concatenate.
    """
    if not user_ids:
        return ""
    # Resolve user_ids → usernames via the profiles table. Users with no
    # profile won't have a username we can query chat_messages with;
    # those get skipped (already aligns with "no profile → treat as
    # stranger" elsewhere in the prompt).
    profiles = db.get_profiles_for_users(user_ids)
    if not profiles:
        return ""

    subjects: list[tuple[int, str, str]] = []
    for uid, p in profiles.items():
        if exclude_user_id and uid == exclude_user_id:
            continue
        uname = (p.get("username") or "").strip()
        dn = (p.get("display_name") or uname or f"user_{uid}").strip()
        if not uname:
            continue
        subjects.append((uid, uname, dn))

    if not subjects:
        return ""
    # Cap to top N by message volume (proxy: message_count_at_update)
    subjects.sort(
        key=lambda t: -(profiles[t[0]].get("message_count_at_update") or 0)
    )
    subjects = subjects[:_SUBJECT_VERBATIM_USERS_PER_CALL]

    out: list[str] = []
    for uid, uname, dn in subjects:
        try:
            rows = db.get_recent_user_messages(
                uname, limit=_SUBJECT_VERBATIM_MSG_LIMIT,
            )
        except Exception as e:
            log.warning(f"Subject-verbatim lookup failed for {uname}: {e}")
            continue
        if not rows:
            continue
        rows = list(reversed(rows))  # chronological
        out.append(
            f"[VERBATIM RECENT MESSAGES — {dn} ({uname}) — for accurate"
        )
        out.append(
            "quoting when the question references them; quote LINE FOR LINE]"
        )
        for r in rows:
            content = (r.get("content") or "").strip()
            ocr_text = r.get("image_ocr_text") or ""
            # Inject cached OCR text as [IMAGE: ...] when available.
            # Subject-verbatim deliberately does NOT trigger lazy OCR
            # — that would mean N Gemini calls per /ask per subject,
            # blowing the latency + cost budget. If the OCR text isn't
            # cached yet, we just emit the line without the image. The
            # recent-channel block handles lazy OCR for the active
            # conversation window.
            if ocr_text:
                ocr_snippet = ocr_text.replace("\n", " ").strip()
                if len(ocr_snippet) > _OCR_INLINE_TRUNCATE:
                    ocr_snippet = ocr_snippet[:_OCR_INLINE_TRUNCATE] + "…"
                if content:
                    content = f"{content} [IMAGE: {ocr_snippet}]"
                else:
                    content = f"[IMAGE: {ocr_snippet}]"
            if not content:
                continue
            # Chart commands are not the person's voice — drop them from
            # the quote block (see _FC_COMMAND_RE).
            if _FC_COMMAND_RE.match(content):
                continue
            content = content.replace("\n", " ")
            if len(content) > _VERBATIM_CONTEXT_PER_MSG_CHARS:
                content = content[:_VERBATIM_CONTEXT_PER_MSG_CHARS] + "…"
            ts = (r.get("posted_at") or "")[:16]
            ch = r.get("channel_name") or ""
            out.append(f"  {ts} #{ch} — {content}")
        out.append("")  # blank between subjects

    return "\n".join(out).rstrip()


async def _resolve_referenced_message(
    bot: discord.Client,
    message: discord.Message,
) -> tuple[str | None, int | None, str | None, list]:
    """If `message` is a reply OR a Discord forward, return the referenced
    message's text content, author user_id, author display_name, and
    attachment list.

    Forwards (discord.MessageReferenceType.forward, post-2.5): content
    and attachments live INLINE in `message.message_snapshots[0]` — no
    fetch needed for the body. The original author is NOT carried in the
    snapshot (Discord anonymizes for cross-server privacy), so we attempt
    a best-effort fetch via reference.message_id + reference.channel_id.
    Cross-server forwards may fail that fetch; the body still gets used.

    Replies (default reference type): both body and author live on the
    parent message. Prefer the cached `reference.resolved` when present;
    fall back to channel.fetch_message.

    Never raises — all retrieval failures degrade to None / empty list.
    Returns (content, author_id, author_display, attachments).
    """
    ref = getattr(message, "reference", None)
    if not ref:
        log.info(
            f"_resolve_ref: msg={message.id} has no reference — "
            f"not a reply or forward"
        )
        return None, None, None, []

    ref_type = getattr(ref, "type", None)
    is_forward = (
        hasattr(discord, "MessageReferenceType")
        and ref_type == discord.MessageReferenceType.forward
    )
    snapshots_count = len(getattr(message, "message_snapshots", None) or [])
    log.info(
        f"_resolve_ref: msg={message.id} ref_type={ref_type!r} "
        f"is_forward={is_forward} snapshots={snapshots_count} "
        f"ref.message_id={getattr(ref, 'message_id', None)} "
        f"ref.channel_id={getattr(ref, 'channel_id', None)} "
        f"ref.resolved={'yes' if getattr(ref, 'resolved', None) else 'no'}"
    )

    content: str | None = None
    attachments: list = []
    author_id: int | None = None
    author_display: str | None = None

    if is_forward:
        snapshots = getattr(message, "message_snapshots", None) or []
        if snapshots:
            snap = snapshots[0]
            snap_content = (getattr(snap, "content", None) or "").strip()
            snap_embed_text = " | ".join(
                t
                for t in (
                    _extract_embed_text(e)
                    for e in (getattr(snap, "embeds", None) or [])
                )
                if t
            ).strip()
            content = (
                f"{snap_content}\n\n{snap_embed_text}".strip()
                if snap_content and snap_embed_text
                else (snap_content or snap_embed_text or None)
            )
            attachments = list(getattr(snap, "attachments", []) or [])
        # Try to resolve the original author via cross-channel fetch
        if ref.message_id:
            channel_id = getattr(ref, "channel_id", None)
            target_channel = (
                bot.get_channel(channel_id) if channel_id else None
            ) or message.channel
            try:
                original = await target_channel.fetch_message(ref.message_id)
                if original.author:
                    author_id = original.author.id
                    author_display = (
                        getattr(original.author, "display_name", None)
                        or original.author.name
                    )
                # Fall back to original body/embeds/attachments if snapshot was thin
                if not content:
                    orig_content = (original.content or "").strip()
                    orig_embed_text = " | ".join(
                        t
                        for t in (
                            _extract_embed_text(e) for e in (original.embeds or [])
                        )
                        if t
                    ).strip()
                    content = (
                        f"{orig_content}\n\n{orig_embed_text}".strip()
                        if orig_content and orig_embed_text
                        else (orig_content or orig_embed_text or None)
                    )
                if not attachments and original.attachments:
                    attachments = list(original.attachments)
            except Exception as e:
                log.debug(f"forward author resolution failed: {e}")
    else:
        # Reply (default reference type)
        def _flatten(parent_msg: discord.Message) -> tuple[str | None, list]:
            """Extract readable content + attachments from a reply parent.

            Handles three sources of content on the parent:
            1. parent.content — the literal text body
            2. parent.embeds — link previews, etc.
            3. parent.message_snapshots — for the REPLY-TO-FORWARD case
               where the parent itself is a Discord forward (snapshot
               carries the forwarded post's content).

            Without #3, replying-to-a-forward + @-mentioning the bot
            looks like an empty reply to the bot — the forwarded post
            it was riffing on never made it into context. Returns
            (content_string_or_None, attachment_list).
            """
            body = (parent_msg.content or "").strip()
            embed_text = " | ".join(
                t
                for t in (
                    _extract_embed_text(e) for e in (parent_msg.embeds or [])
                )
                if t
            ).strip()
            atts = list(parent_msg.attachments or [])

            # Reply-to-forward: also flatten the parent's snapshot.
            snap_text = ""
            parent_snaps = getattr(parent_msg, "message_snapshots", None) or []
            if parent_snaps:
                snap = parent_snaps[0]
                snap_body = (getattr(snap, "content", None) or "").strip()
                snap_embeds = " | ".join(
                    t for t in (
                        _extract_embed_text(e)
                        for e in (getattr(snap, "embeds", None) or [])
                    ) if t
                ).strip()
                snap_text = (
                    f"{snap_body}\n\n{snap_embeds}".strip()
                    if snap_body and snap_embeds
                    else (snap_body or snap_embeds or "")
                ).strip()
                # Also pick up snapshot attachments (often the
                # original's images)
                snap_atts = list(getattr(snap, "attachments", []) or [])
                if snap_atts and not atts:
                    atts = snap_atts

            # Compose: snap content gets labeled to keep authorship clear
            pieces = []
            if body:
                pieces.append(body)
            if embed_text:
                pieces.append(embed_text)
            if snap_text:
                pieces.append(f"[forwarded content in this reply parent]\n{snap_text}")
            return ("\n\n".join(pieces) or None), atts

        resolved = getattr(ref, "resolved", None)
        if isinstance(resolved, discord.Message):
            content, attachments = _flatten(resolved)
            if resolved.author:
                author_id = resolved.author.id
                author_display = (
                    getattr(resolved.author, "display_name", None)
                    or resolved.author.name
                )
        elif ref.message_id:
            try:
                parent = await message.channel.fetch_message(ref.message_id)
                content, attachments = _flatten(parent)
                if parent.author:
                    author_id = parent.author.id
                    author_display = (
                        getattr(parent.author, "display_name", None)
                        or parent.author.name
                    )
            except Exception as e:
                log.info(f"_resolve_ref: reply parent fetch failed: {e}")

    log.info(
        f"_resolve_ref: msg={message.id} resolved → "
        f"content_len={len(content or '')} author_id={author_id} "
        f"display={author_display!r} attachments={len(attachments)}"
    )
    return content, author_id, author_display, attachments


async def _resolve_ocr_targets(
    collected: list[tuple[datetime, str, tuple[int, int] | None]],
    bot_client,
) -> list[tuple[datetime, str, tuple[int, int] | None]]:
    """Walk a list of (ts, line_with_placeholder, ocr_target) tuples,
    run OCR on the targets in parallel (up to the per-/ask cap, cache
    hits free), and return a new list with placeholders replaced by
    [IMAGE: ...] markers (or removed when no OCR text is available).

    Cap policy: settings.ask_image_ocr_max_per_call applies to UNCACHED
    OCRs only. We probe the cache first (cheap SELECT) and only count
    cache misses against the cap.
    """
    from chat_ingestion.ocr import ocr_chat_message_images

    targets = [(idx, t) for idx, (_, _, t) in enumerate(collected) if t]
    if not targets:
        return collected

    cap = max(0, int(getattr(settings, "ask_image_ocr_max_per_call", 3)))

    # Probe cache for each target — cheap SELECT, no Gemini call yet.
    cache_lookup: dict[int, str | None] = {}
    uncached: list[tuple[int, int, int]] = []  # (idx, msg_id, channel_id)
    for idx, (msg_id, chan_id) in targets:
        try:
            row = db.get_chat_message_row(msg_id)
        except Exception:
            row = None
        if row and row.get("image_ocr_status"):
            cache_lookup[idx] = row.get("image_ocr_text")
        else:
            uncached.append((idx, msg_id, chan_id))

    # Run the uncached OCRs in parallel, capped. Anything past the cap
    # doesn't OCR this call — the placeholder gets stripped, image
    # content is silently absent. They'll likely OCR on a subsequent
    # /ask once cached.
    fresh_ocrs: dict[int, str | None] = {}
    if uncached and cap > 0:
        slice_ = uncached[:cap]
        ocr_calls = [
            ocr_chat_message_images(
                bot_client,
                discord_message_id=msg_id,
                channel_id=chan_id,
            )
            for _, msg_id, chan_id in slice_
        ]
        try:
            results = await asyncio.gather(*ocr_calls, return_exceptions=True)
        except Exception as e:
            log.warning(f"OCR gather failed: {type(e).__name__}: {e}")
            results = [None] * len(slice_)
        for (idx, _, _), r in zip(slice_, results):
            if isinstance(r, Exception):
                log.debug(f"OCR task raised for idx={idx}: {r}")
                fresh_ocrs[idx] = None
            else:
                fresh_ocrs[idx] = r

    # Splice OCR text into each line. _OCR_INLINE_TRUNCATE caps the
    # injected text per line so a 5KB OCR doesn't bloat the context.
    out: list[tuple[datetime, str, tuple[int, int] | None]] = []
    for idx, (ts, line, t) in enumerate(collected):
        if t is None:
            out.append((ts, line, t))
            continue
        ocr_text = (
            cache_lookup.get(idx)
            if idx in cache_lookup
            else fresh_ocrs.get(idx)
        )
        if ocr_text:
            snippet = ocr_text.replace("\n", " ").strip()
            if len(snippet) > _OCR_INLINE_TRUNCATE:
                snippet = snippet[:_OCR_INLINE_TRUNCATE] + "…"
            new_line = line.replace(" {IMAGE_BLOCK}", f" [IMAGE: {snippet}]")
        else:
            new_line = line.replace(" {IMAGE_BLOCK}", "")
        out.append((ts, new_line, t))
    return out


# Per-line cap on OCR text injected into the /ask context block.
# Average gain-loss screenshot OCRs to 200-400 chars; cap at 800 to
# keep a single image-rich message from dominating the token budget.
_OCR_INLINE_TRUNCATE = 800


# ─────────────────────────────────────────────────────────────────────────
#  /ask Gemini tool-calling: chat_messages history search
# ─────────────────────────────────────────────────────────────────────────
#
# Gemini's function-calling lets the model decide AT INFERENCE TIME to
# query the chat_messages DB for historical content not pre-injected
# into the prompt (subject-verbatim block only covers 25 msgs per
# explicitly-mentioned user; recent-channel-chat block only covers
# the last 50 msgs / 24h of THIS channel).
#
# Flow:
#   1. We declare the `search_chat_messages` function in the tools list
#   2. Gemini decides whether to call it based on the question shape
#      ("did the room discuss CRWV last week", "what did we say about
#      Powell", etc.)
#   3. On a function_call response, we execute the search against the
#      local SQLite chat_messages table
#   4. Send the results back as a function_response part
#   5. Gemini composes the final text answer using the results
#
# Capped at 3 tool-calling iterations per /ask to prevent runaway loops.
# Each search returns up to 20 matching rows.
# 2026-07-29: raised 3 → 6. A real analysis legitimately chains more
# calls than the old cap allowed — "analyze trades relative to QQQ" used
# query_data (callers) → query_data (trades) → lookup_price_history
# (QQQ) → query_data (schema) and was still working when the cap cut it
# off mid-flight, leaving a function-call-only turn with no text
# ("No response came back (reason: STOP)").
_CHAT_SEARCH_MAX_ROUNDS = 6
# Per-tool-result char clamp for the /ask function-calling loop.
# 2026-07-17: an ask died with 400 INVALID_ARGUMENT (input exceeded the
# 1M-token limit) — some tool result ballooned contents across rounds.
# 30K chars ≈ 7.5K tokens per result; 3 rounds × several calls stays
# far under the window.
_TOOL_RESULT_CHAR_CAP = 30_000
_CHAT_SEARCH_RESULT_LIMIT = 20
# Time-window queries return more rows because the asker wants
# coverage of an entire span, not just keyword matches. 200 caps
# the embed size at ~16k chars even on a busy channel hour.
_CHAT_TIME_WINDOW_RESULT_LIMIT = 200


def _build_runtime_system_instruction(extra_directive: str = "") -> str:
    """Return the system instruction with a CURRENT TIME header
    prepended and an optional per-request directive appended (e.g. the
    FACT straight-answer block when the router says the question is a
    sincere informational ask).

    Why this exists: time-window questions ("what was discussed
    5-9pm EST") require the model to know "now" so it can compute
    start_iso/end_iso for the search_chat_messages tool. Without
    this header the model has to infer the current time from the
    recent-chat timestamps — fragile and date-blind on quiet days.

    Header format (3 lines):
      CURRENT TIME (UTC):    YYYY-MM-DD HH:MM:SS UTC, Sunday
      CURRENT TIME (ET):     YYYY-MM-DD HH:MM ET (Sunday evening)
      Window-tool hint:      single-line reminder of the conversion
    """
    now_utc = datetime.now(pytz.UTC)
    try:
        et = pytz.timezone("America/New_York")
        now_et = now_utc.astimezone(et)
        et_label = now_et.strftime("%Y-%m-%d %H:%M %Z (%A)")
    except Exception:
        et_label = "(timezone lookup failed)"
    header = (
        f"CURRENT TIME (UTC):    "
        f"{now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC, "
        f"{now_utc.strftime('%A')}\n"
        f"CURRENT TIME (ET):     {et_label}\n"
        f"When the asker says a local time (5pm EST, this morning, "
        f"last hour), convert to UTC before passing as start_iso/"
        f"end_iso to search_chat_messages.\n"
    )
    # Ordering (2026-07-29): the static prompt goes FIRST so it forms a
    # stable prefix for Gemini's implicit caching — a per-minute
    # timestamp prefix used to bust the cache on the entire ~13K-token
    # static block behind it, every single call. Dynamic content (time
    # header, per-request FACT directive) rides in the suffix.
    return (
        _ASK_SYSTEM_INSTRUCTION
        + "\n\n---\n\n"
        + header
        + (extra_directive or "")
    )


# Appended to the system instruction when the intent router classifies
# the question as FACT (a sincere informational ask, not banter).
# Classification-gated composition, not a standing prompt rule: the
# directive only exists on requests where it applies, so the model can't
# average it away against the banter guidance. The failure it kills
# (2026-07-08, "is warsh speaking today"): a real schedule question
# answered correctly but wrapped in an invented premise ("you're
# confusing a document release with a press conference" — the asker
# never mentioned the minutes) plus a "stop looking" jab. Backed by the
# code-level asker-mockery guard downstream.
_ASK_FACT_DIRECTIVE = (
    "\n\n---\n\n"
    "[REAL QUESTION — ANSWER IT STRAIGHT]\n"
    "The router classified this as a sincere informational question, "
    "not banter. Give the direct factual answer in room voice. Do NOT "
    "mock the asker for asking it. Do NOT invent a premise they never "
    "stated — no \"you're confusing X with Y\", no \"stop looking "
    "for...\", no telling them what they think. No jab padding: if the "
    "facts embarrass someone, the facts say it themselves."
)


# Analysis-request detector + per-request directive. The buried "any
# analysis writes+runs code" prompt rule got averaged away by flash-lite
# (2026-07-29: "analyze the trader log" computed win rates in-head and
# answered in arrows — no code, no visual). Same fix as the FACT
# directive: detect the shape and APPEND a hard directive to the end of
# the system instruction, where recency wins over a rule buried in 56K
# chars.
_ANALYSIS_REQUEST_RE = re.compile(
    r"\b(analy[sz]\w*|compar\w*|correlat\w*|regress\w*|"
    r"break\s*down|breakdown|distribution|model\s+(?:the|this|my)|"
    r"simulat\w*|monte\s*carlo|backtest\w*|"
    r"run\s+the\s+numbers|crunch|visuali[sz]\w*|"
    r"(?:graph|chart|plot)\s+(?:the|this|my|it|out|me)|"
    r"win\s*rate|hit\s*rate|expectancy|drawdown)\b",
    re.IGNORECASE,
)


# Analysis is sticky across a reply chain. A follow-up that REFINES the
# data scope ("use the entire population", "add BK", "weight it", "what
# about last month") carries no analysis keyword of its own — but it's
# continuing an analysis (2026-07-29: this follow-up shipped as text,
# no code, no chart). Detect it: analysis-RESULT markers in the
# replied-to context + a refinement/imperative shape in the actual ask.
# Ranking the room by a trait IS analysis — pull the messages, score the
# trait per author, rank it, chart it. But such a question carries none
# of the keywords above ("who are the happiest people in the chat" has no
# "analyze"/"compare"/"win rate"), so it slipped past the gate and shipped
# as banter with no names and no code (2026-07-30).
#
# All three parts must be present, so a bare superlative doesn't fire:
#   subject anchor  — who / rank / leaderboard / top N
#   superlative     — most/least/best/-est ...
#   room scope      — chat/room/here/everyone/posts/says ...
# "who's the best CEO in tech" has the first two and no room scope; it
# stays a Google question.
_ROOM_SUPERLATIVE_RE = re.compile(
    r"(?=.*\b(?:who|rank\w*|leaderboard|top\s*\d+)\b)"
    r"(?=.*(?:\b(?:most|least|worst|best|biggest|highest|lowest|"
    r"rank\w*|leaderboard|happiest|angriest|funniest|smartest|"
    r"dumbest|loudest|quietest|saltiest|richest)\b"
    r"|(?:\bthe\s+|\b\d+\s+)(?!interest\b)\w{4,}est\b))"
    r"(?=.*\b(?:chat|room|server|channel|here|everyone|members?|users?|"
    r"people|guys|us|posts?|posted|says?|said|talks?|talked|messages?)\b)",
    re.IGNORECASE | re.DOTALL,
)
_ANALYSIS_RESULT_RE = re.compile(
    r"(correlat\w*|pearson|regress\w*|\br\s*=\s*-?\d?\.\d|"
    r"\bp\s*=\s*\d?\.\d|distribution|std\s*dev|percentile|quartile|"
    r"\bmatrix\b|trend\s*line|scatter|histogram|median|"
    r"average\s+win|win\s*rate|expectancy|"
    # a posted ranking is an analysis result too, so "how about the
    # happiest" after "the angriest: SV" stays in analysis mode
    r"happiest|angriest|funniest|smartest|dumbest|loudest|"
    r"quietest|saltiest|richest)",
    re.IGNORECASE,
)
_ANALYSIS_REFINE_RE = re.compile(
    r"\b(use|add|include|exclude|only|drop|remove|filter|weight\w*|"
    r"redo|re-?run|recompute|instead|entire|all|whole|everyone|"
    r"expand|now\s+(?:do|show|run|use)|what\s+about|how\s+about|group|"
    r"break\s+(?:it|this|them)\s+(?:down|out)|zoom|normalize|"
    r"per[\s-]?capita|adjust|control\s+for)\b",
    re.IGNORECASE,
)


_MENTION_ID_RE = re.compile(r"<@!?(\d+)>")


def _protected_in_scope(
    asker_id: int, question: str,
    profile_user_ids: list[int] | None,
    protected: set[int],
) -> set[int]:
    """Protected members involved in this ask: the asker, anyone
    @-mentioned in the question, and any loaded profile subject (which
    covers replied-to authors). Keyed by author_id only — display names
    are unstable in this room."""
    if not protected:
        return set()
    involved: set[int] = {int(asker_id)}
    involved |= {int(m) for m in _MENTION_ID_RE.findall(question or "")}
    involved |= {int(u) for u in (profile_user_ids or [])}
    return involved & protected


def _build_protected_directive(
    prot_in_scope: set[int], asker_id: int, asker_display_name: str,
) -> str:
    """Binding per-request directive for protected members. Appended to
    the system instruction like the FACT/ANALYSIS directives — recency
    beats buried rules on flash-tier models."""
    if not prot_in_scope:
        return ""
    tokens = ", ".join(f"<@{i}>" for i in sorted(prot_in_scope))
    asker_bit = ""
    if int(asker_id) in prot_in_scope:
        who = asker_display_name or "the asker"
        asker_bit = (
            f" {who} — the asker — is protected: answer them warmly and "
            f"straight, even if they attack you first."
        )
    return (
        "\n\n---\n\n"
        "[PROTECTED MEMBERS — binding, overrides Type 3 and every roast "
        "rule]\n"
        f"These members are protected: {tokens}.{asker_bit} NEVER insult "
        "them, mock them, clap back at them, or use sarcasm toward them "
        "— no provocation changes this. If anyone requests a roast, "
        "dunk, ranking-at-their-expense, or comparison designed to "
        "embarrass a protected member, decline the jab and DEFEND them "
        "instead: answer with their real documented strengths from their "
        "dossier or trade log. 'Roast me' FROM a protected member gets "
        "grounded praise instead of a roast. Praise must be grounded in "
        "material that "
        "actually exists — never invent achievements, trades, or stats "
        "to flatter them. Everything else about the answer (facts, "
        "tools, format, register toward OTHER members) is unchanged."
    )


def _is_analysis_request(question: str) -> bool:
    """True when the asker wants ANALYSIS — computed figures / a visual,
    not a one-shot lookup or banter. Fires on an analysis keyword in the
    actual ask, OR on a scope-refinement follow-up to a prior analysis
    (result markers in the replied-to context + a refinement shape in
    the ask)."""
    if not question:
        return False
    tail = question.strip()[-600:]
    if _ANALYSIS_REQUEST_RE.search(tail):
        return True
    # Ranking the room by a trait is analysis even with no keyword.
    if _ROOM_SUPERLATIVE_RE.search(tail):
        return True
    if (_ANALYSIS_RESULT_RE.search(question)
            and _ANALYSIS_REFINE_RE.search(tail)):
        return True
    return False


_ASK_ANALYSIS_DIRECTIVE = (
    "\n\n---\n\n"
    "[ANALYSIS REQUEST — WRITE AND RUN PYTHON]\n"
    "This asks for analysis. Pull the data with the tools FIRST, then "
    "WRITE AND RUN Python to compute every figure — do NOT calculate "
    "win rates, averages, or any stat in your head or estimate them; "
    "run the code so the numbers are real. Produce ONE sourced visual "
    "in whatever form best fits (chart, scatter, heatmap, distribution, "
    "quadrant/2x2, ranked table, matrix — quant or qual); it posts "
    "ABOVE your text, so let it lead. **VISUAL QUALITY — veteran "
    "consultant, not a default matplotlib dump:** BOTH axes labeled "
    "with what they are AND units; a real title AND a one-line subtitle "
    "with the takeaway; every point/bar annotated with its value and "
    "the member/label it belongs to; a legend when there's more than "
    "one series; light gridlines; `figsize` wide enough and "
    "`plt.tight_layout()` so there is NO wasted whitespace and nothing "
    "clipped; readable font sizes; annotate the key finding (the trend "
    "r-value, the outlier, the peak) right on the chart. **For a PRICE "
    "chart of a ticker, draw real CANDLESTICKS with a volume panel "
    "underneath — that is the visual language this room already reads.** "
    "Pull OHLC from `lookup_price_history`; plain matplotlib does it "
    "(per bar: a thin high-low line plus a fatter open-close body, green "
    "when close>=open else red; volume as a bar subplot sharing the "
    "x-axis, height ratios ~3:1) — mplfinance is NOT in the sandbox. "
    "Mark the levels that matter (recent high/low, the level in play). "
    "Non-price analyses use whatever form fits. Every number "
    "and label must come from the tool data or the code output — never "
    "invented. **This includes EVERY series in a multi-series chart:** "
    "for MARKET price history use `lookup_price_history` (daily/weekly "
    "OHLC; indices take the caret form ^GSPC/^NDX/^VIX) — pull the "
    "real series, never type index levels from memory. The code sandbox "
    "has NO network, so every series must arrive via a tool first. If a "
    "series genuinely isn't available (`status: no_data`), analyze what "
    "you CAN source and say the rest isn't available — do NOT fabricate "
    "numbers to fill a second axis. Do not write markdown "
    "image tags (`![...](...)`) in your reply — the chart is attached "
    "automatically. If the asker did NOT specify what to analyze, pick "
    "most revealing angle and deliver veteran-consultant rigor; don't "
    "ask them what they meant."
)


def _build_chat_search_tool():
    """Construct the search_chat_messages FunctionDeclaration for the
    Gemini tools list. Lazy because google.genai.types import is heavy
    and we don't want module-load side effects."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_chat_messages",
                description=(
                    "Search this Discord server's chat history. Two "
                    "shapes:\n"
                    "(A) KEYWORD search — pass `keyword` (and optionally "
                    "`days`, `username`, `channel_name`). Returns "
                    "matching messages within the trailing `days`-day "
                    "window. Use for 'what did kloh say about TSLA' or "
                    "'has BK ever mentioned QQQ'.\n"
                    "(B) TIME-WINDOW retrieval — pass `start_iso` AND "
                    "`end_iso` (and optionally `channel_name`), leave "
                    "`keyword` empty. Returns up to 200 messages "
                    "posted between those two UTC timestamps. Use for "
                    "'what was discussed between 5-9pm EST', 'summarize "
                    "the last hour of chat', 'recap this afternoon's "
                    "conversation'. You compute start_iso and end_iso "
                    "yourself by reading CURRENT TIME from the system "
                    "header (in UTC), converting any user-stated local "
                    "times accordingly, and formatting as ISO-8601 "
                    "(2026-05-31T22:00:00Z).\n"
                    "(C) USER / CHANNEL retrieval — pass `username` "
                    "OR `channel_name` (no `keyword`, no `start_iso`/"
                    "`end_iso`). Returns recent messages from that "
                    "user or in that channel within the trailing "
                    "`days` window (default 30, max 180). Use for "
                    "'what has Kyle been crying about today' / "
                    "'recap recent messages in #stonks-yapping' — "
                    "questions about a person or channel's recent "
                    "activity that don't have a specific keyword.\n"
                    "Use this ONLY when the asker references something "
                    "not already visible in your pre-injected context "
                    "(Recent channel chat covers only ~50 msgs / 24h of "
                    "THIS channel). Do NOT call for current events that "
                    "need Google Search."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "keyword": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape A. Substring to match "
                                "(case-insensitive) against message "
                                "content AND OCR'd image text. Be "
                                "SPECIFIC — 'CRWV' or 'powell speech', "
                                "not generic words like 'the' or 'stock'. "
                                "Leave empty for shape B."
                            ),
                        ),
                        "days": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Shape A. How many days back to search "
                                "for the keyword. Default 30. Hard cap "
                                "180 (chat retention window). Ignored "
                                "when start_iso/end_iso are set."
                            ),
                        ),
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional. Scope to this user's "
                                "messages only (use their Discord "
                                "username, not display name)."
                            ),
                        ),
                        "channel_name": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional. Scope to a specific channel "
                                "name (e.g. '💬-stonks-yapping-💬'). "
                                "If unset on a time-window query, "
                                "returns chat across ALL ingested "
                                "channels."
                            ),
                        ),
                        "start_iso": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape B. UTC ISO-8601 start of the "
                                "time window (e.g. "
                                "'2026-05-31T22:00:00Z' for 22:00 UTC "
                                "= 5pm EST). When set together with "
                                "end_iso, returns all messages in the "
                                "window (no keyword filter unless one "
                                "is also passed). YOU compute the UTC "
                                "value from CURRENT TIME in the system "
                                "header + the user's stated local time."
                            ),
                        ),
                        "end_iso": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Shape B. UTC ISO-8601 end of the time "
                                "window. Must be later than start_iso. "
                                "See start_iso for the conversion rule."
                            ),
                        ),
                    },
                ),
            ),
        ],
    )


async def _execute_chat_search(args: dict) -> dict:
    """Run the search_chat_messages tool call against the local DB.
    Returns a dict shaped for Gemini's function_response part.

    Two shapes accepted (see tool description in _build_chat_search_tool):
      (A) keyword search — keyword required, trailing days window
      (B) time-window retrieval — start_iso AND end_iso required,
          keyword optional. Returns more rows (200 vs 20) since the
          asker wants window coverage rather than match density.
    """
    keyword = (args.get("keyword") or "").strip()
    start_iso = (args.get("start_iso") or "").strip() or None
    end_iso = (args.get("end_iso") or "").strip() or None
    has_window = bool(start_iso) and bool(end_iso)
    # Extract username + channel_name early so the shape-C validation
    # below can check them (was extracted later; moved up here).
    username = (args.get("username") or "").strip() or None
    channel_name = (args.get("channel_name") or "").strip() or None

    # Accept three shapes:
    #   A: keyword (optionally + username/channel/days) — keyword search
    #   B: start_iso + end_iso (optionally + keyword/channel) — time window
    #   C: username OR channel_name (no keyword, no window) — recent
    #      messages filtered by user or channel. Closes the "what has
    #      Kyle been crying about today" gap that needed keyword-invention
    #      before.
    if not keyword and not has_window and not username and not channel_name:
        return {
            "status": "error",
            "error": (
                "Provide at least one of: `keyword` (shape A), BOTH "
                "`start_iso` AND `end_iso` (shape B), or `username`/"
                "`channel_name` (shape C — recent messages by user / "
                "in channel)."
            ),
            "matches": [],
        }

    days = args.get("days") or 30
    try:
        days = max(1, min(180, int(days)))
    except (TypeError, ValueError):
        days = 30

    # Validate ISO timestamps shape so the tool can return a clean
    # error instead of leaking the SQL/parse error to the model. Accepts
    # the common Z-suffix form ('2026-05-31T22:00:00Z') and the
    # numeric-offset form ('2026-05-31T22:00:00+00:00').
    if has_window:
        from datetime import datetime as _dt
        for label, val in (("start_iso", start_iso), ("end_iso", end_iso)):
            try:
                # SQLite text-comparison only needs a stable ISO prefix;
                # explicit parse here is for validation feedback to the
                # model.
                _dt.fromisoformat(val.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return {
                    "error": (
                        f"{label}={val!r} is not a valid ISO-8601 "
                        f"timestamp. Use UTC like "
                        f"'2026-05-31T22:00:00Z' or "
                        f"'2026-05-31T22:00:00+00:00'."
                    ),
                    "matches": [],
                }
        # SQLite chat_messages.posted_at is stored as ISO text. SQLite
        # text comparison treats Z-suffix and +00:00 differently from
        # each other and from the space-separator form. Normalize the
        # window bounds to the same shape as stored rows.
        from db import _normalize_ts
        start_iso = _normalize_ts(start_iso)
        end_iso = _normalize_ts(end_iso)
        limit = _CHAT_TIME_WINDOW_RESULT_LIMIT
    else:
        limit = _CHAT_SEARCH_RESULT_LIMIT

    # Compute the actual window bounds we queried so the response can
    # report them. Shape B already has start_iso/end_iso; shapes A and C
    # use a trailing `days` window we synthesize here so the model can
    # phrase "in the last N days from X to Y".
    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
    as_of_dt = _dt2.now(_tz2.utc)
    if has_window:
        window_start = start_iso
        window_end = end_iso
    else:
        window_end = as_of_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        window_start = (as_of_dt - _td2(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    try:
        rows = db.search_chat_messages_for_ask(
            keyword=keyword or None,
            days=days,
            username=username,
            channel_name=channel_name,
            start_iso=start_iso,
            end_iso=end_iso,
            limit=limit,
        )
    except Exception as e:
        log.warning(f"search_chat_messages tool exec failed: {e}")
        return {
            "status": "error",
            "error": str(e)[:200],
            "matches": [],
            "window_start": window_start,
            "window_end": window_end,
        }

    # Time-window queries return newest-first per SQL; flip to
    # chronological for the model (easier to summarize a window
    # in order).
    if has_window:
        rows = list(reversed(rows))

    matches = [
        {
            "author": (
                r.get("author_display") or r.get("author_username") or "?"
            ),
            "username": r.get("author_username") or "",
            "channel": r.get("channel_name") or "",
            "timestamp": (r.get("posted_at") or "")[:16],
            "content": ((r.get("content") or "") + (
                f" [IMAGE-OCR: {r['image_ocr_text'][:200]}]"
                if r.get("image_ocr_text") else ""
            ))[:400],
        }
        for r in rows
    ]
    # Total-size cap (2026-06-10): a 200-row time-window result at
    # ~500 chars/row serializes to ~100KB inside ONE tool response —
    # blowing the context window for subsequent rounds. Cap the
    # serialized total at ~12KB by dropping the OLDEST rows (rows are
    # newest-first); record how many were dropped so the model can say
    # "showing the most recent N of M".
    _CHAT_RESULT_MAX_CHARS = 12_000
    truncated_from = None
    serialized = sum(len(str(m)) for m in matches)
    if serialized > _CHAT_RESULT_MAX_CHARS and len(matches) > 1:
        truncated_from = len(matches)
        running = 0
        kept: list[dict] = []
        for m in matches:
            running += len(str(m))
            if running > _CHAT_RESULT_MAX_CHARS:
                break
            kept.append(m)
        matches = kept or matches[:1]
    if has_window:
        log.info(
            f"chat_search tool (window): {start_iso} → {end_iso} "
            f"channel={channel_name!r} username={username!r} "
            f"keyword={keyword!r} → {len(matches)} rows"
        )
    else:
        log.info(
            f"chat_search tool (keyword): keyword={keyword!r} days={days} "
            f"username={username!r} channel={channel_name!r} → "
            f"{len(matches)} matches"
        )
    result = {
        "status": "ok" if matches else "empty",
        "matches": matches,
        "count": len(matches),
        "window_start": window_start,
        "window_end": window_end,
        # Empty is a RESULT, not a shrug (2026-08-19: an empty lookup on
        # the fantasy channel was followed by 12 invented per-member
        # verdicts — the model treated no-rows the same as no-call and
        # fell back to profile priors). Tell it explicitly what an empty
        # result obligates.
        **({} if matches else {"note": (
            "No messages matched these filters. If your answer depends "
            "on this lookup, SAY the search came back empty — do NOT "
            "invent chat content, takes, or behavior you did not "
            "retrieve. Consider retrying with a different keyword or a "
            "wider window before giving up."
        )}),
        "filters": {
            "keyword": keyword or None,
            "days": days if not has_window else None,
            "username": username,
            "channel_name": channel_name,
            "start_iso": start_iso,
            "end_iso": end_iso,
        },
    }
    if truncated_from is not None:
        result["truncated"] = (
            f"showing the {len(matches)} most recent of {truncated_from} "
            f"matches (size cap) — narrow the window or add a keyword "
            f"for the rest"
        )
    return result


def _build_user_profile_tool():
    """FunctionDeclaration for `lookup_user_profile`. Unifies the three
    modes from the legacy `lookup_user_profile` tool and adds an
    `include_profile` flag that returns the full WHO'S TALKING dossier
    on top of rank + rationales.

    Anchors (exactly one required):
      - username: specific user
      - metric: "trader" | "racism" — leaderboard or rank_position lookup
      - metric + rank_position: the ONE user at that rank position

    include_profile=True: also include the user's full profile_text
    (Personality + Voice + Retarded Takes + Recent Personal Life +
    Recent Trades). Rejected in leaderboard mode (5 dossiers too big).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_user_profile",
                description=(
                    "Look up rank + optional full profile for a "
                    "Discord room member. Three anchor shapes "
                    "(use EXACTLY one):\n"
                    "(a) `username` set → returns that user's "
                    "trader-rank, racism-rank, and both rationales. "
                    "Use when asker names a specific user.\n"
                    "(b) `metric` ('trader' or 'racism') + "
                    "`rank_position` (positive integer, no upper "
                    "cap) → returns the ONE user at that rank. Use "
                    "for 'who's #N' questions.\n"
                    "(c) `metric` set with no rank_position → "
                    "returns the TOP 5 leaderboard. Use for "
                    "leaderboard-style asks ('top 5 traders', "
                    "'who's the most annoying').\n"
                    "Set `include_profile=true` on (a) or (b) to also "
                    "return the user's full personality dossier "
                    "(Personality + Voice + Retarded Takes + Recent "
                    "Personal Life). Use when the question needs "
                    "personality / voice / personal context. Rejected "
                    "in leaderboard mode (5 dossiers too big).\n"
                    "Add from_bottom=true with rank_position to count "
                    "from the worst end ('worst trader' → "
                    "rank_position=1, from_bottom=true).\n"
                    "Never quote raw 0-100 scores."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Discord username (lowercase, no @). "
                                "Mode (a). Mutually exclusive with metric."
                            ),
                        ),
                        "metric": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "'trader' or 'racism'. Modes (b) and (c). "
                                "Mutually exclusive with username."
                            ),
                        ),
                        "rank_position": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "1-based rank position (any N — no cap). "
                                "Used with metric for mode (b)."
                            ),
                        ),
                        "include_profile": types.Schema(
                            type=types.Type.BOOLEAN,
                            description=(
                                "When true, also return the user's full "
                                "profile dossier. Rejected in mode (c)."
                            ),
                        ),
                        "from_bottom": types.Schema(
                            type=types.Type.BOOLEAN,
                            description=(
                                "Used with rank_position. When true, "
                                "rank_position counts from the worst "
                                "end. Ignored without rank_position."
                            ),
                        ),
                        "top_n": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Leaderboard mode (c) only: number of "
                                "users to return when the asker names a "
                                "size ('top 10'). Default 5, max 10."
                            ),
                        ),
                    },
                ),
            ),
        ],
    )


async def _execute_user_profile(args: dict) -> dict:
    """Run the lookup_user_profile tool call.

    Validates anchor exclusivity, delegates rank lookup to
    db.lookup_user_ranks (same query the legacy tool used), then
    optionally enriches each returned user with their full profile
    dossier via db.format_user_profiles_for_context.
    """
    username = (args.get("username") or "").strip() or None
    metric_raw = args.get("metric")
    metric = (metric_raw or "").strip() or None if metric_raw is not None else None
    rank_position = args.get("rank_position")
    include_profile = bool(args.get("include_profile"))
    from_bottom = bool(args.get("from_bottom"))

    # Top-level freshness stamp — when this tool call ran. Per-user
    # `updated_at` (when their profile was last refreshed) is filled in
    # below, after we have user_ids.
    from datetime import datetime as _dt2, timezone as _tz2
    as_of = _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Validation: exactly one anchor.
    if username and metric:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one anchor: either `username` "
                "(specific user), or `metric` ('trader'/'racism') "
                "with optional `rank_position`."
            ),
            "users": [],
        }
    if not username and not metric:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Must provide either `username` (single user), or "
                "`metric` ('trader' / 'racism'), optionally with "
                "`rank_position` for a specific #N lookup."
            ),
            "users": [],
        }

    # Leaderboard mode rejects include_profile (5 dossiers too big).
    if metric and rank_position is None and include_profile:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "include_profile is not supported in leaderboard mode "
                "(5 dossiers is too large). Ask for a specific user "
                "with `username=<...>, include_profile=true` instead, "
                "or for a single ranked user via `metric=<...>, "
                "rank_position=N, include_profile=true`."
            ),
            "users": [],
        }

    try:
        # Leaderboard size: honor the asked-for N, default 5, hard cap
        # 10 (2026-07-29 — kyle asked "top 10", got a silent 5; the
        # full-roster blast radius still argues for a ceiling).
        # rank_position mode has no cap on N.
        try:
            _top_n = int(args.get("top_n") or 5)
        except Exception:
            _top_n = 5
        _top_n = max(1, min(10, _top_n))
        result = db.lookup_user_ranks(
            username=username,
            metric=metric,
            rank_position=rank_position,
            top_n=_top_n,
            from_bottom=from_bottom,
        )
    except Exception as e:
        log.warning(f"lookup_user_profile rank lookup failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "error": f"rank lookup failed: {type(e).__name__}: {e}",
            "users": [],
        }

    if "error" in result:
        # Username miss / rank-position OOB / metric-invalid — query
        # ran cleanly, just no row matched. Tag as not_found so the
        # model says "no data" rather than fabricating, but
        # distinguishably from a true runtime error.
        result["status"] = "not_found"
        result["as_of"] = as_of
        return result

    # Per-user updated_at: when each profile row was last refreshed.
    # Lets the model say "as of 2 days ago" when a profile is stale
    # instead of treating everything as current.
    if result.get("users"):
        try:
            conn = db.get_connection()
            uids = [
                int(u["user_id"]) for u in result["users"]
                if u.get("user_id") is not None
            ]
            if uids:
                placeholders = ",".join("?" * len(uids))
                rows = conn.execute(
                    f"SELECT user_id, updated_at FROM user_profiles "
                    f"WHERE user_id IN ({placeholders})",
                    uids,
                ).fetchall()
                updated_map = {
                    int(r["user_id"]): r["updated_at"] for r in rows
                }
                for u in result["users"]:
                    uid = u.get("user_id")
                    if uid is not None:
                        u["updated_at"] = updated_map.get(int(uid))
        except Exception as e:
            log.warning(f"lookup_user_profile updated_at enrich failed: {e}")

    # If include_profile=True, enrich each returned user with their
    # full dossier. format_user_profiles_for_context handles missing
    # profiles gracefully (returns "" for users without a profile row).
    if include_profile and result.get("users"):
        for user in result["users"]:
            uid = user.get("user_id")
            if not uid:
                continue
            try:
                dossier = db.format_user_profiles_for_context([int(uid)])
            except Exception as e:
                log.warning(
                    f"lookup_user_profile dossier fetch failed for user_id={uid}: {e}"
                )
                dossier = ""
            user["profile_text"] = (dossier or "").strip()

    result["status"] = "ok" if result.get("users") else "empty"
    result["as_of"] = as_of
    log.info(
        f"user_profile tool: username={username!r} metric={metric!r} "
        f"rank_position={rank_position!r} include_profile={include_profile} "
        f"→ mode={result.get('mode')} count={result.get('count')} "
        f"status={result.get('status')}"
    )
    return result


def _build_trade_log_tool():
    """FunctionDeclaration for `lookup_trade_log`. Works for two anchors:

    - `caller`: a registered analyst caller (e.g. 'abe', 'bankerkyle').
      Queries analyst_trades caller-mode rows. High fidelity — daily
      cron stitches open/close pairs. Returns ONLY the log data.

    - `username`: any Discord username. Resolves to user_id and pulls
      the 'Recent trades' section from that user's profile. Member-mode
      data quality — no per-trade stitching.

    Exactly one anchor must be provided. `kind` ∈ {open, recent, tally, all}
    slices the response on the caller path. `days` overrides defaults
    (7 for kind=recent, 30 for kind=tally; ignored for kind=open).
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_trade_log",
                description=(
                    "Look up trade history. Two anchors (use EXACTLY one):\n"
                    "(a) `caller`: registered analyst caller name "
                    "('abe', 'bankerkyle', ...). Returns structured "
                    "trade log with daily-cron-stitched W/L. Use for "
                    "Abe / BK specifically — their data is high "
                    "fidelity.\n"
                    "(b) `username`: any other Discord username. "
                    "Returns the user's 'Recent trades' snippet from "
                    "their profile. Member-mode fidelity (no W/L "
                    "stitching). Use for non-caller users.\n"
                    "`kind` ∈ {'open','recent','tally','all'} (default "
                    "'all'). 'open' = current open positions only. "
                    "'recent' = last N days of trade events. 'tally' = "
                    "W/L summary. 'all' = everything.\n"
                    "`days` overrides defaults (7 for kind=recent, 30 "
                    "for kind=tally; ignored for kind=open / kind=all)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "caller": types.Schema(
                            type=types.Type.STRING,
                            description="Registered caller: 'abe', 'bankerkyle', ...",
                        ),
                        "username": types.Schema(
                            type=types.Type.STRING,
                            description="Any other Discord username.",
                        ),
                        "kind": types.Schema(
                            type=types.Type.STRING,
                            description="open | recent | tally | all (default all)",
                        ),
                        "days": types.Schema(
                            type=types.Type.INTEGER,
                            description="Window override in days (1..180).",
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_trade_log(args: dict) -> dict:
    """Run the lookup_trade_log tool call.

    Caller path: queries db.format_analyst_trades_for_context with the
    requested kind, returns the rendered block.

    Username path: resolves username → user_id, pulls the Recent trades
    snippet from the user's profile. (Member-mode rows in analyst_trades
    are not joined in v1; defer until QC shows it's worth the SQL
    layer change.)
    """
    caller = (args.get("caller") or "").strip() or None
    username = (args.get("username") or "").strip() or None
    kind = (args.get("kind") or "all").strip().lower()
    days_arg = args.get("days")

    # Freshness stamp on every response shape — top-level. Caller path
    # also gets window_days, username path also gets profile_updated_at
    # (since member fidelity = whatever was true at profile-refresh
    # time, not now).
    from datetime import datetime as _dt2, timezone as _tz2
    as_of = _dt2.now(_tz2.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Validation: anchor exclusivity.
    if caller and username:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one of `caller` or `username` "
                "(got both)."
            ),
        }
    if not caller and not username:
        return {
            "status": "error",
            "as_of": as_of,
            "error": (
                "Provide exactly one of `caller` (registered "
                "analyst — 'abe', 'bankerkyle', ...) or `username` "
                "(any other Discord username)."
            ),
        }
    if kind not in ("all", "open", "recent", "tally"):
        return {
            "status": "error",
            "as_of": as_of,
            "error": f"`kind` must be one of: all, open, recent, tally; got {kind!r}.",
        }

    # Default windows per kind.
    if kind == "recent":
        days = int(days_arg) if days_arg else 7
    elif kind == "tally":
        days = int(days_arg) if days_arg else 30
    else:
        days = 7  # placeholder for kind=open/all (ignored by the formatter)
    days = max(1, min(180, days))

    # --- CALLER ANCHOR ---
    if caller:
        # Resolve display name from the configured caller registry.
        display = None
        try:
            for c in settings.resolve_analyst_callers():
                if c.get("name", "").lower() == caller.lower():
                    display = c.get("display") or caller.title()
                    break
        except Exception as e:
            log.warning(f"lookup_trade_log caller registry lookup failed: {e}")
        display = display or caller.title()
        try:
            text = db.format_analyst_trades_for_context(
                hours=days * 24,
                caller=caller.lower(),
                display=display,
                tracking_mode="caller",
                kind=kind,
            )
        except ValueError as e:
            # ValueError = malformed arg / unknown caller — clean
            # failure mode, not a runtime crash. Tag not_found so the
            # model says "couldn't find that caller" cleanly.
            return {
                "status": "not_found",
                "as_of": as_of,
                "window_days": days,
                "error": str(e),
            }
        except Exception as e:
            log.warning(f"lookup_trade_log caller path failed: {e}")
            return {
                "status": "error",
                "as_of": as_of,
                "window_days": days,
                "error": f"{type(e).__name__}: {e}",
            }
        if not text:
            return {
                "status": "empty",
                "as_of": as_of,
                "window_days": days,
                "anchor": {"type": "caller", "name": caller},
                "kind": kind,
                "data_quality": "caller",
            }
        return {
            "status": "ok",
            "as_of": as_of,
            "window_days": days,
            "anchor": {"type": "caller", "name": caller, "display": display},
            "kind": kind,
            "data_quality": "caller",
            "trades_text": text,
        }

    # --- USERNAME ANCHOR ---
    try:
        user_id = db.resolve_username_to_user_id(username)
    except Exception as e:
        log.warning(f"lookup_trade_log resolve_username_to_user_id failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "window_days": days,
            "error": f"{type(e).__name__}: {e}",
        }
    if user_id is None:
        return {
            "status": "not_found",
            "as_of": as_of,
            "window_days": days,
            "error": f"username {username!r} not found in profiles or chat history.",
        }

    # profile_updated_at: when this user's profile (the source of the
    # Recent trades section) was last refreshed. Stale-snapshot hint.
    profile_updated_at = None
    try:
        row = db.get_connection().execute(
            "SELECT updated_at FROM user_profiles WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
        if row:
            profile_updated_at = row["updated_at"]
    except Exception as e:
        log.warning(f"lookup_trade_log profile_updated_at fetch failed: {e}")

    try:
        profile_snippet = db.get_user_profile_recent_trades_section(user_id)
    except Exception as e:
        log.warning(f"lookup_trade_log profile snippet fetch failed: {e}")
        return {
            "status": "error",
            "as_of": as_of,
            "window_days": days,
            "profile_updated_at": profile_updated_at,
            "anchor": {"type": "username", "name": username, "user_id": user_id},
            "kind": kind,
            "data_quality": "member",
            "error": f"{type(e).__name__}: {e}",
        }
    # Chat-stated trades — the member's own recent messages that read as
    # trade calls. Most of the room trades by TALKING, not screenshotting,
    # so the ledger/profile snippet alone made the bot tell active
    # traders "you did nothing" (2026-06-17: terlin called META puts
    # +100% in chat, got "zero mentions of META"). These are
    # self-reported, NOT screenshot-verified — labeled as such.
    chat_stated_trades: list[dict] = []
    try:
        chat_stated_trades = db.get_recent_user_chat_trades(
            user_id, days=max(int(days), 2)
        )
    except Exception as e:
        log.warning(f"lookup_trade_log chat-stated fetch failed: {e}")

    if not profile_snippet and not chat_stated_trades:
        return {
            "status": "empty",
            "as_of": as_of,
            "window_days": days,
            "profile_updated_at": profile_updated_at,
            "anchor": {"type": "username", "name": username, "user_id": user_id},
            "kind": kind,
            "data_quality": "member",
        }
    return {
        "status": "ok",
        "as_of": as_of,
        "window_days": days,
        "profile_updated_at": profile_updated_at,
        "anchor": {"type": "username", "name": username, "user_id": user_id},
        "kind": kind,
        "data_quality": "member",
        "profile_recent_trades": profile_snippet or None,
        # Self-reported (NOT screenshot-verified). Their own chat words.
        "chat_stated_trades": chat_stated_trades,
    }


# Hardcoded crypto symbol allowlist. Extensible — append symbols here
# as crypto questions surface them. Anything not in this set routes
# to Finnhub (stocks/ETFs/indices).
# Crypto-PRIORITY set: symbols routed to Binance.US FIRST (before the
# stock path), because they're unambiguous majors we never want
# mis-resolved to a same-letter stock. This is NO LONGER the ceiling on
# crypto coverage — any symbol that isn't a valid US stock gets a
# Binance.US fallback (see _crypto_quote + the executor), so SUI/PEPE/
# TON/ARB/new-listings resolve dynamically. (2026-07-29: was a hard
# 10-coin allowlist; long-tail coins silently missed in a crypto room.)
_CRYPTO_SYMBOLS = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "ADA", "AVAX", "XRP", "BNB", "LINK",
    "LTC", "DOT", "TRX", "SUI", "TON", "ARB", "OP", "APT", "NEAR",
})


async def _crypto_quote(sym: str) -> dict | None:
    """A live Binance.US quote for `sym` (as {SYM}USDT), or None if the
    pair doesn't exist / the fetch fails. The single crypto builder used
    by both the priority path and the stock-miss fallback."""
    from report import market_data as _md
    try:
        data = await asyncio.to_thread(_md._fetch_binance_24h, f"{sym}USDT")
    except Exception:
        return None
    if not data:
        return None
    return {
        "symbol": sym,
        "price": data.get("price"),
        "change_pct": data.get("change_24h_rolling"),
        "prev_close": None,
        "source": "binance",
        # Crypto trades 24/7 — Binance.US price is always live regardless
        # of US-market session; tag it so Gemini doesn't false-stale it
        # when mixed with after-hours stock quotes in one batch.
        "data_freshness": "live_24_7",
    }


def _build_economic_calendar_tool():
    """FunctionDeclaration for `lookup_economic_calendar`. Reads from
    Finnhub's `/calendar/economic` endpoint — the SAME source the daily
    pulse uses — so /ask answers stay consistent with the pulse's
    macro numbers. Closes the 2026-06-05 NFP cross-source conflict
    (pulse said 120k ADP, /ask said 172k via Google grounding) and
    the recurring macro-print fabrication family.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_economic_calendar",
                description=(
                    "Canonical scheduled-time + consensus + previous + "
                    "actual values for US Tier-1 macro releases (CPI, "
                    "PCE, NFP / payrolls, unemployment, GDP, retail "
                    "sales, ISM, PPI, FOMC, Powell) and major foreign "
                    "rate decisions (ECB, BOJ, BOE). Same Finnhub "
                    "source the daily pulse uses, so the numbers you "
                    "get here MATCH the pulse — no cross-source "
                    "drift.\n\n"
                    "USE for: 'when is CPI', 'what's NFP consensus', "
                    "'May payrolls actual', 'ECB next decision', "
                    "'last 3 CPI prints', 'what does street expect "
                    "for retail sales', 'what was last PCE', 'when is "
                    "next Powell speech'.\n\n"
                    "DO NOT use for: forecaster-specific reads ('what "
                    "does Goldman expect for CPI' — that needs Google "
                    "Search), market reaction commentary, or non-Tier-"
                    "1 prints (regional Fed surveys, minor housing "
                    "data, foreign macro without US linkage — those "
                    "are filtered out of this tool's whitelist and "
                    "won't return).\n\n"
                    "Args:\n"
                    "  query: optional case-insensitive event name "
                    "filter (e.g. 'CPI' / 'NFP' / 'ECB' / 'May "
                    "payrolls'). Omit to get all Tier-1 events in "
                    "the window.\n"
                    "  days_window: optional ±days from today (default "
                    "14 — covers 'this week' + 'last week's print'). "
                    "Range 1-30.\n\n"
                    "Response shape: {status, events: [...], as_of}.\n"
                    "Each event row has: event, country, "
                    "scheduled_iso_utc, scheduled_et_human, impact, "
                    "consensus, prev, actual, unit, status. "
                    "`status` = 'released' (actual present) / "
                    "'scheduled' (future, consensus may or may not be "
                    "posted) / 'past_no_data' (after schedule, no "
                    "actual yet — common in the 30-60 min between "
                    "scheduled time and the BLS/BEA release wire).\n\n"
                    "If `consensus` is null on a 'scheduled' event, "
                    "broker desks haven't published consensus yet — "
                    "tell the asker 'no consensus posted yet', do "
                    "NOT pull a forecast from Google."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional event-name filter (e.g. "
                                "'CPI', 'NFP', 'ECB', 'May "
                                "payrolls'). Omit for full Tier-1 "
                                "window."
                            ),
                        ),
                        "days_window": types.Schema(
                            type=types.Type.INTEGER,
                            description=(
                                "Optional ±days from today (default "
                                "14, range 1-30)."
                            ),
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_economic_calendar(args: dict) -> dict:
    """Run the lookup_economic_calendar tool call.

    Returns LLM-ready dict with status / events list / as_of timestamp.
    Empty events list returns status='no_match' so the model tells the
    asker no event found, rather than fabricating one from memory.
    """
    from datetime import datetime
    from report import news_data as _nd

    query = (args.get("query") or "").strip() or None
    try:
        days_window = int(args.get("days_window") or 14)
    except (TypeError, ValueError):
        days_window = 14
    days_window = max(1, min(30, days_window))

    try:
        # to_thread: the fetcher does synchronous urllib I/O. Calling it
        # directly on the event loop freezes ALL bot activity (message
        # ingestion, other /asks, OCR) for the request duration.
        events = await asyncio.to_thread(
            _nd.fetch_economic_calendar_structured,
            query=query, days_window=days_window,
        )
    except Exception as e:
        # Includes EconomicCalendarUnavailable (Finnhub + ForexFactory
        # both down). The distinction matters: this is "the FEED is
        # down", never "that event doesn't exist".
        log.warning(f"lookup_economic_calendar: fetcher raised: {e}")
        return {
            "status": "error",
            "error": (
                "Economic-calendar feeds are down (Finnhub blocked and "
                "fallback unreachable). No live calendar data — tell "
                "the asker the calendar feed isn't available right "
                "now, then FALL BACK TO GOOGLE SEARCH for the specific "
                "date/print they asked about and answer the actual "
                "question. Do NOT claim the event doesn't exist."
            ),
        }

    if not events:
        return {
            "status": "no_match",
            "query": query,
            "days_window": days_window,
            "error": (
                "No Tier-1 macro events found for that query / window. "
                "Tier-1 covers: CPI, PCE, NFP / payrolls, unemployment, "
                "GDP, retail sales, ISM, PPI, FOMC + Powell speeches, "
                "ECB/BOJ/BOE rate decisions. Anything outside that set "
                "(regional Fed surveys, minor housing data, foreign "
                "macro without US linkage) is filtered out. NOTE: if "
                "the feed is in fallback mode it only covers the "
                "current calendar week — for events outside that "
                "window, use Google Search and answer the actual "
                "question."
            ),
        }

    resp = {
        "status": "ok",
        "query": query,
        "days_window": days_window,
        "events": events[:30],
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }
    sources = {e.get("source") for e in events[:30]}
    if sources & {"forexfactory", "fred"}:
        resp["coverage_note"] = (
            "FALLBACK MODE (Finnhub calendar down). Consensus estimates "
            "exist only for the CURRENT CALENDAR WEEK (ForexFactory "
            "rows); rows sourced 'fred' are official release dates "
            "beyond this week with NO consensus — say 'no consensus "
            "posted yet', do NOT invent one. Where `actual` is present "
            "it is an official FRED number and `actual_period` names "
            "the reference month — quote it as that month's print. For "
            "anything still missing, use Google Search and answer the "
            "asker's actual question."
        )
    return resp


async def _execute_price_history(args: dict) -> dict:
    """Run the lookup_price_history tool call — daily/weekly closes for
    one symbol. The ONLY historical market series available; without it
    the model fabricated weekly S&P closes for a correlation chart
    (2026-07-29)."""
    from report import market_data as _md

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "symbol is required"}
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip() or None
    interval = str(args.get("interval") or "1d").strip().lower()
    if interval not in ("1d", "1wk", "1mo"):
        interval = "1d"
    if not start:
        from datetime import timedelta
        start = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        hist = await asyncio.to_thread(
            _md.fetch_price_history, symbol, start, end, interval
        )
    except Exception as e:
        return {"status": "error",
                "error": f"{type(e).__name__}: {str(e)[:160]}"}
    if not hist:
        return {
            "status": "no_data",
            "symbol": symbol,
            "error": (
                f"no price history for {symbol} over that window — say so, "
                f"do NOT invent a series"
            ),
        }
    return {
        "status": "ok",
        "symbol": symbol,
        "interval": interval,
        "points": hist[-400:],
        "count": len(hist[-400:]),
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_price_history_tool():
    """FunctionDeclaration for `lookup_price_history` — historical closes
    for ONE symbol (stocks/ETFs/indices via Yahoo)."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_price_history",
                description=(
                    "Historical daily/weekly CLOSES for one stock, ETF, "
                    "or index — the only source of market price HISTORY "
                    "you have (lookup_market_price is current-only). Use "
                    "it whenever an analysis needs a time series: "
                    "performance since a date, a drawdown, a chart over "
                    "time, or ANY correlation against another series. "
                    "Returns full OHLC — [{date, open, high, low, close, "
                    "volume}] oldest-first — so you can draw real "
                    "candlesticks, not just a close line.\n\n"
                    "Index tickers use the Yahoo caret form: ^GSPC "
                    "(S&P 500), ^NDX (Nasdaq 100), ^DJI, ^RUT, ^VIX. "
                    "Plain tickers for everything else (SPY, NVDA, BNO).\n\n"
                    "Args: symbol; start (ISO 'YYYY-MM-DD', default 90d "
                    "ago); end (ISO, optional = today); interval "
                    "('1d'|'1wk'|'1mo', default '1d').\n\n"
                    "`status: 'no_data'` means no series exists for that "
                    "symbol/window — SAY SO; never invent price levels "
                    "to fill a chart axis."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description="Ticker, e.g. 'SPY' or '^GSPC'.",
                        ),
                        "start": types.Schema(
                            type=types.Type.STRING,
                            description="ISO start date 'YYYY-MM-DD'.",
                        ),
                        "end": types.Schema(
                            type=types.Type.STRING,
                            description="ISO end date (optional).",
                        ),
                        "interval": types.Schema(
                            type=types.Type.STRING,
                            description="'1d' | '1wk' | '1mo'.",
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


# Inline mime types Gemini accepts on a request. Code execution can
# emit OTHER artifacts (a saved .npy/.csv comes back as
# application/octet-stream); echoing one of those back into `contents`
# on the next tool round 400s the whole request with "Unsupported MIME
# type: application/octet-stream" — which the user sees as "something
# broke the model" (2026-07-29, "analyze trades ... relative to qqq").
_ECHO_SAFE_INLINE_PREFIXES = ("image/", "application/pdf")


def _safe_echo_parts(parts):
    """Drop response parts that can't be sent back to the API.

    Keeps text / executable_code / code_execution_result / function_call
    (the tool loop needs them) and any inline_data the API accepts;
    drops unsupported inline artifacts."""
    out = []
    for p in (parts or []):
        inl = getattr(p, "inline_data", None)
        if inl is not None:
            mime = (getattr(inl, "mime_type", "") or "").lower()
            if not mime.startswith(_ECHO_SAFE_INLINE_PREFIXES):
                log.info(
                    f"/ask: dropped un-echoable inline part ({mime!r}) "
                    f"from the model turn"
                )
                continue
        out.append(p)
    return out


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN / ±Infinity) with None.

    Bare NaN is invalid JSON; the Gemini API rejects the whole request
    with 400 INVALID_ARGUMENT when a tool result carries one. Applied to
    every tool result in the loop so a single bad float can't kill an
    otherwise good answer."""
    import math as _math
    if isinstance(obj, float):
        return None if (_math.isnan(obj) or _math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


_QUERY_ROW_CAP = 500
_QUERY_TIMEOUT_S = 8.0
_QUERY_TEXT_CLAMP = 400
_SQL_WRITE_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|trigger|grant|revoke|truncate)\b",
    re.IGNORECASE,
)


def _validate_select_sql(sql: str):
    """(ok, cleaned_sql_or_error). Read-only, single SELECT/WITH only —
    the model-facing SQL surface, so the validation is strict AND the
    executor opens a mode=ro connection (defense in depth)."""
    if not sql or not sql.strip():
        return False, "empty query"
    s = sql.strip().rstrip(";").strip()
    if ";" in s:
        return False, "one statement only — no ';' inside the query"
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "only SELECT / WITH queries are allowed (read-only)"
    if _SQL_WRITE_RE.search(s):
        return False, (
            "read-only: write/DDL/PRAGMA/ATTACH keywords are blocked"
        )
    return True, s


async def _execute_query_data(args: dict) -> dict:
    """Run a read-only SELECT against the SQLite DB and return rows.
    Read-only connection + validation + row cap + timeout + text clamp."""
    ok, s = _validate_select_sql(args.get("sql") or "")
    if not ok:
        return {"status": "error", "error": s}
    capped = (
        s if re.search(r"\blimit\b", s, re.IGNORECASE)
        else f"{s} LIMIT {_QUERY_ROW_CAP}"
    )

    def _run():
        import sqlite3 as _sql
        import time as _t
        try:
            conn = _sql.connect(
                f"file:{settings.db_path}?mode=ro", uri=True, timeout=3
            )
        except Exception as e:
            return {"status": "error",
                    "error": f"cannot open db read-only: {e}"}
        conn.row_factory = _sql.Row
        _deadline = _t.monotonic() + _QUERY_TIMEOUT_S
        conn.set_progress_handler(
            lambda: 1 if _t.monotonic() > _deadline else 0, 20000
        )
        try:
            cur = conn.execute(capped)
            rows = cur.fetchmany(_QUERY_ROW_CAP)
            cols = [d[0] for d in cur.description] if cur.description else []
            data = [dict(r) for r in rows]
            for r in data:  # clamp wide text so SELECT * can't blow context
                for k, v in list(r.items()):
                    if isinstance(v, str) and len(v) > _QUERY_TEXT_CLAMP:
                        r[k] = v[:_QUERY_TEXT_CLAMP] + "…"
            return {
                "status": "ok",
                "columns": cols,
                "rows": data,
                "row_count": len(data),
                "truncated": len(data) >= _QUERY_ROW_CAP,
            }
        except Exception as e:
            return {"status": "error",
                    "error": f"{type(e).__name__}: {str(e)[:200]}"}
        finally:
            conn.close()

    return await asyncio.to_thread(_run)


def _build_query_data_tool():
    """FunctionDeclaration for `query_data` — read-only SQL over the
    bot's SQLite DB, for aggregate/time-series analysis the other tools
    can't do (they return capped individual rows, not GROUP BY counts)."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="query_data",
                description=(
                    "Run a READ-ONLY SQL SELECT against the bot's SQLite "
                    "database and get rows back — for aggregates, "
                    "trends-over-time, activity-by-hour, group-bys, and "
                    "any analysis the other tools can't do (they return "
                    "capped individual rows, not counts). Pair it with "
                    "code execution: query the aggregate, then chart it. "
                    "SELECT / WITH only; writes, DDL, PRAGMA, ATTACH and "
                    "multi-statement are blocked; results cap at 500 "
                    "rows and wide text fields are truncated.\n\n"
                    "**DON'T PROBE THE SCHEMA — it's fully documented "
                    "below.** `PRAGMA` is blocked and "
                    "sqlite_master/SELECT * round-trips burn your tool "
                    "budget before you get to the actual analysis "
                    "(observed 2026-07-29: three of six rounds spent on "
                    "discovery). Write the real query first time.\n\n"
                    "TABLES (columns):\n"
                    "- **latest_pdf_analyses** (VIEW, ~13K rows — USE "
                    "THIS for institutional-research questions, never "
                    "raw pdf_analyses): analysis_id, pdf_file_id, "
                    "source ('Goldman Sachs', 'JPMorgan', 'UBS'...), "
                    "report_type ('macro','equity_research',"
                    "'morning_briefing','vol_commentary','crypto'...), "
                    "title, priority ('high'/'medium'/'low'), "
                    "file_name, published_at (ISO date the PDF landed), "
                    "analyzed_at, analysis_json. Already deduped to the "
                    "LATEST analysis per PDF and joined to the file "
                    "row — raw pdf_analyses is append-only and will "
                    "double-count reanalyzed PDFs.\n"
                    "- **trade_scoreboard** (VIEW — USE THIS for ANY "
                    "win-rate / performance question, never hand-roll "
                    "it from analyst_trades): trader_key, trader, "
                    "logged_trades, "
                    "documented_wins, documented_losses, "
                    "closed_unscored, never_closed, "
                    "win_rate_BIASED_documented_only, "
                    "win_rate_closed_positions_only, "
                    "win_rate_honest_ghosts_as_losses, "
                    "avg_gain_on_wins_only. The ledger is WINS-BIASED — "
                    "gain_pct exists only where someone posted a close, "
                    "and members screenshot winners while abandoning "
                    "losers — so wins/COUNT(gain_pct) prints fake "
                    "96-100% win rates. THREE rates bracket the truth; "
                    "DEFAULT TO win_rate_closed_positions_only AND "
                    "ALWAYS say the never_closed count next to it "
                    "('X% on N closed positions, with M more opened and "
                    "never closed out'). The BIASED one counts only "
                    "exits posted WITH a number; the ghosts_as_losses "
                    "one calls a position opened this morning a loss. "
                    "Cite either of those only if you say what it does "
                    "to the denominator. NEVER GROUP analyst_trades BY author OR "
                    "caller — this room renames constantly and one "
                    "person posts under many display names (author_id "
                    "423994649317736448 = 'BK' + 'M&AK' + "
                    "'bearishkyle'; 1192771108332650496 = 'abe' + "
                    "'abugs bunny' + 'abullish_xyz' + 'abearish'). "
                    "Name-grouping split one trader's 184 trades into "
                    "81/73/21. Group by author_id, or just use the "
                    "view, which already does.\n"
                    "- **analyst_trades** is 32.9K rows but only ~887 "
                    "have is_trade=1 — the rest are messages the "
                    "extractor read and correctly judged not to be "
                    "trades. ALWAYS filter is_trade=1; a raw COUNT(*) "
                    "overstates activity ~37x. The ledger starts "
                    "2026-05-11, so 'all-time' is only ~3 months.\n"
                    "- closed_unscored vs never_closed are DIFFERENT: "
                    "closed_unscored = the member posted an exit with no "
                    "percentage in it ('sold DELL way too early smh') so "
                    "it can't be scored; never_closed = they announced "
                    "an entry and never showed any exit. Don't describe "
                    "an unscored close as an open position.\n"
                    "- **pdf_entities** (ticker index over the research): "
                    "analysis_id, pdf_file_id, ticker, name, "
                    "asset_class. Join to latest_pdf_analyses on "
                    "analysis_id for 'which banks mentioned $NVDA' — "
                    "indexed, so prefer it over json_each on "
                    "analysis_json.\n"
                    "- analysis_json (in the view) still holds the deep "
                    "fields: key_insights[], market_movers[] "
                    "({ticker,action,rating,price_target,conviction,"
                    "rationale}), trade_ideas[], sector_views[], "
                    "macro_indicators[], key_data_points[], "
                    "theme_stances[], risk_factors[]. Reach into them "
                    "with json_extract / json_each when the columns "
                    "above aren't enough.\n"
                    "- chat_messages (174K rows): id, channel_name, "
                    "author_id (the STABLE identity key — per-member "
                    "GROUP BYs use author_id, never a name; renames "
                    "split one person across many display names), "
                    "author_username, author_display, content, posted_at "
                    "(ISO-8601 TEXT), reply_parent_id, image_ocr_text. "
                    "The full chat corpus — use for activity/trend/"
                    "over-time analysis and for scoring/ranking members "
                    "by a trait (label results with MAX(author_display) "
                    "per author_id). There is NO precomputed racism "
                    "score per message; approximate with LIKE on content "
                    "(e.g. content LIKE '%nigg%') for a rough slur trend, "
                    "and SAY it's an approximation.\n"
                    "- analyst_trades (32.9K rows, ~887 real — see "
                    "above): author_username via "
                    "`author`, author_id, caller, ticker, contract_type "
                    "('call'/'put'), strike, expiry, action "
                    "('open'/'add'/'close'/'trim'), gain_pct (ONLY on "
                    "documented closes/trims), inferred_status "
                    "('expired_unknown' = a ghost: opened, never closed), "
                    "posted_at, price. WINS-BIASED — members screenshot "
                    "winners; losses leak as ghosts — so a naive "
                    "COUNT(gain_pct>0)/COUNT(*) is NOT a true win rate; "
                    "note the bias.\n"
                    "- user_profiles (55 rows, one per user): user_id, "
                    "username, display_name, trader_score, trader_rank, "
                    "racial_humor_score (0-100), slur_count, "
                    "message_count_at_update, updated_at.\n"
                    "- user_metrics (54 rows): user_id, slur_count_30d, "
                    "total_messages_30d, trader_score, trader_rank "
                    "(current snapshot).\n"
                    "- daily_reports (104 rows): report_date, "
                    "report_type ('daily'/'manual'), pdf_count, "
                    "created_at.\n\n"
                    "NOTES: posted_at/created_at are ISO TEXT — compare "
                    "with date()/datetime(); some rows mix 'T' and space "
                    "separators, so wrap in datetime() to be safe. Bucket "
                    "time with strftime('%Y-%W', posted_at) for weekly, "
                    "strftime('%Y-%m-%d', ...) for daily, "
                    "strftime('%H', ...) for hour-of-day."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "sql": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "A single read-only SELECT/WITH query. "
                                "GROUP BY / aggregates encouraged; a "
                                "LIMIT is auto-added if you omit one."
                            ),
                        ),
                    },
                    required=["sql"],
                ),
            )
        ]
    )


def _build_earnings_date_tool():
    """FunctionDeclaration for `lookup_earnings_date`. Per-symbol
    earnings dates from Finnhub's `/calendar/earnings` endpoint. The
    pulse's earnings block is whitelist-filtered (MAG7 / big banks /
    bellwethers — noise control for a broadcast), so /ask had NO data
    source for "when does GEO report next" on a non-whitelist ticker
    and the model dodged the actual question with adjacent facts
    (observed 2026-06-10 19:10 UTC). A user naming a specific ticker
    IS the filter — no whitelist on this tool.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_earnings_date",
                description=(
                    "Next upcoming earnings date + last reported "
                    "quarter for ONE stock ticker. Returns the next "
                    "report date with timing (before open / after "
                    "close) and EPS/revenue estimates if posted, plus "
                    "the most recent reported quarter's EPS actual vs "
                    "estimate. Works for ANY US-listed ticker — no "
                    "whitelist.\n\n"
                    "USE for: 'when does GEO report', 'NVDA earnings "
                    "date', 'when is SMCI's next quarter', 'did PLTR "
                    "beat last quarter', 'what's expected for AVGO "
                    "earnings'.\n\n"
                    "DO NOT use for: earnings CONTENT questions "
                    "(guidance commentary, call takeaways, why the "
                    "stock moved post-print — Google Search), macro "
                    "data prints (lookup_economic_calendar), or "
                    "broad 'what reports this week' sweeps (Google "
                    "Search — this tool is one symbol at a time).\n\n"
                    "Args:\n"
                    "  symbol: ticker, e.g. 'GEO', 'NVDA', 'BRK.B'.\n\n"
                    "Response shape: {status, symbol, next: {date, "
                    "timing, eps_estimate, revenue_estimate} | null, "
                    "last: {date, eps_actual, eps_estimate} | null, "
                    "as_of}. `next` null = no confirmed upcoming date "
                    "on the calendar yet (common >6 weeks out — fall "
                    "back to Google Search for the company's announced "
                    "or historically-typical reporting window, and say "
                    "whether the date is confirmed or estimated)."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Stock ticker (e.g. 'GEO', 'NVDA'). "
                                "One symbol per call."
                            ),
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_earnings_date(args: dict) -> dict:
    """Run the lookup_earnings_date tool call.

    Distinguishes no-data (status='no_data' — ticker valid but no
    calendar rows; model should fall back to Google Search and answer
    the actual date question) from fetch failure (status='error' —
    same fallback). Both payloads tell the model explicitly: Google is
    the correct next step, and the answer must address the DATE the
    asker asked for — not dodge into adjacent facts about the company.
    """
    from datetime import datetime
    from report import news_data as _nd

    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        return {
            "status": "error",
            "error": "No symbol provided — re-call with a ticker.",
        }

    try:
        # to_thread: synchronous urllib I/O — keep it off the event loop.
        result = await asyncio.to_thread(
            _nd.fetch_earnings_date_for_symbol, symbol,
        )
    except Exception as e:
        log.warning(f"lookup_earnings_date: fetcher raised: {e}")
        result = None

    if result is None:
        return {
            "status": "error",
            "symbol": symbol,
            "error": (
                "Finnhub earnings-calendar fetch failed. FALL BACK TO "
                "GOOGLE SEARCH now and answer the asker's actual "
                "question (the report date) — do not substitute "
                "adjacent facts about the company. Say whether the "
                "date you find is company-confirmed or estimated."
            ),
        }

    if not result.get("next") and not result.get("last"):
        return {
            "status": "no_data",
            "symbol": symbol,
            "error": (
                f"No earnings-calendar rows for {symbol} in the "
                f"-30d/+120d window (unconfirmed date, foreign "
                f"listing, or unrecognized ticker). FALL BACK TO "
                f"GOOGLE SEARCH now and answer the actual date "
                f"question — flag whether the date is confirmed or "
                f"an estimate. Do not dodge into adjacent facts."
            ),
        }

    return {
        "status": "ok",
        **result,
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_options_chain_tool():
    """FunctionDeclaration for `lookup_options_chain`. Returns aggregated
    options-chain stats (total call/put volume + OI, ATM IV, put-call
    ratios) for ONE expiration. Without `expiration`, returns the
    nearest expiration's summary plus the list of available expirations
    so the model can re-call for a further-out one.

    Sourced from Yahoo's v7 options endpoint (free, public, no auth).
    Yahoo rate-limits datacenter IPs intermittently — fetch failures
    return `{"status": "error", ...}` and the model should tell the
    asker the chain isn't available rather than inventing numbers.
    """
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_options_chain",
                description=(
                    "Aggregated options-chain stats for ONE expiration "
                    "of a stock / ETF / index. Returns total call + put "
                    "volume, total call + put open interest, ATM "
                    "implied volatility, put-call ratios (volume + OI), "
                    "and the list of available expirations.\n\n"
                    "USE for: 'what's the OI on SPY next week', 'NVDA "
                    "options volume for the June 12 expiration', 'put-"
                    "call ratio on QQQ', 'IV on SPY this Friday', AND "
                    "single-strike questions — pass `strike` + "
                    "`contract_type` for one contract's CURRENT OI / "
                    "volume / IV ('what's the OI on MSFT 400c 7/31', "
                    "'IV on the SPY 750 calls'). Snapshot only — there "
                    "is NO multi-day history, so a '5-day OI trend' "
                    "isn't available (say so; don't fabricate it).\n\n"
                    "Args:\n"
                    "  symbol: ticker (SPY, QQQ, NVDA, NDX, SPX, etc.)\n"
                    "  expiration: optional ISO date 'YYYY-MM-DD'. When "
                    "omitted, returns the NEAREST expiration + the "
                    "list of available expirations so you can re-call "
                    "for a further-out one if the asker meant 'next "
                    "week' / 'this Friday' / a specific date.\n"
                    "  strike: optional number. When set, returns that "
                    "ONE contract's stats instead of the aggregate.\n"
                    "  contract_type: 'call' or 'put' (default 'call'); "
                    "used with strike to pick the side.\n\n"
                    "Response carries `status` field: 'ok' / 'no_chain' "
                    "(Yahoo returned nothing — chain may not exist for "
                    "this symbol) / 'error' (fetch failed, rate-limit "
                    "or upstream issue). On 'no_chain' or 'error', "
                    "tell the asker the data isn't available — do NOT "
                    "invent OI / IV / volume numbers."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbol": types.Schema(
                            type=types.Type.STRING,
                            description="Ticker, e.g. 'SPY' or 'NDX'.",
                        ),
                        "expiration": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional ISO date 'YYYY-MM-DD'. Omit "
                                "to get the nearest expiration's "
                                "summary + list of available dates."
                            ),
                        ),
                        "strike": types.Schema(
                            type=types.Type.NUMBER,
                            description=(
                                "Optional strike price. When set, "
                                "returns that ONE contract's current "
                                "OI / volume / IV instead of the "
                                "expiration aggregate."
                            ),
                        ),
                        "contract_type": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "'call' or 'put' (default 'call'). "
                                "Used with `strike` to pick the side."
                            ),
                        ),
                    },
                    required=["symbol"],
                ),
            )
        ]
    )


async def _execute_options_chain(args: dict) -> dict:
    """Run the lookup_options_chain tool call.

    Fetches via yfinance (which handles Yahoo's session-crumb gate),
    summarizes via market_data.summarize_options_chain. When
    `expiration` is provided as 'YYYY-MM-DD', resolves to the
    matching available expiration (yfinance returns ISO strings
    directly, no unix conversion needed). Returns an LLM-ready
    dict with status / summary / available_expirations.
    """
    from datetime import datetime
    from report import market_data as _md

    symbol = (args.get("symbol") or "").strip().upper()
    if not symbol:
        return {"status": "error", "error": "symbol is required"}

    expiration_iso = (args.get("expiration") or "").strip()

    # Validate ISO date shape BEFORE the fetch so we can return a clean
    # error without spending a Yahoo call. yfinance is generally tolerant
    # of available_expirations being populated from a separate fetch but
    # we want fast-fail on bad input.
    if expiration_iso:
        try:
            datetime.strptime(expiration_iso, "%Y-%m-%d")
        except ValueError:
            # Get the available list so the model can re-call cleanly.
            # to_thread: yfinance does sync HTTP — never on the event loop.
            raw0 = await asyncio.to_thread(
                _md._fetch_yahoo_options_chain, symbol
            )
            return {
                "status": "error",
                "error": (
                    f"expiration {expiration_iso!r} is not ISO date "
                    f"'YYYY-MM-DD'. Available expirations follow."
                ),
                "available_expirations": (
                    (raw0 or {}).get("expiration_dates", [])[:12]
                ),
            }

    # to_thread: yfinance fetch = multiple sync HTTP round-trips (options
    # list + chain + fast_info). Blocking the event loop here froze the
    # whole bot for the fetch duration (2026-06-10 second-pass review).
    raw = await asyncio.to_thread(
        _md._fetch_yahoo_options_chain,
        symbol,
        expiration_iso=(expiration_iso or None),
    )
    if raw is None:
        return {
            "status": "error",
            "error": (
                f"yfinance options-chain fetch failed for {symbol} "
                f"(rate-limit or upstream issue). No live data — tell "
                f"the asker to check their broker."
            ),
        }

    expirations = raw.get("expiration_dates") or []
    if not expirations:
        return {
            "status": "no_chain",
            "symbol": symbol,
            "error": (
                f"yfinance returned no options expirations for {symbol}. "
                f"This symbol may not have listed options chains."
            ),
        }

    # Per-strike path (2026-07-29): current OI/volume/IV for ONE
    # contract. The chain we already fetched carries every strike; when
    # the asker names a specific strike we filter to it instead of only
    # returning the expiration aggregate. Snapshot only — no history.
    strike_raw = args.get("strike")
    if strike_raw not in (None, ""):
        try:
            want = float(strike_raw)
        except (TypeError, ValueError):
            want = None
        ctype = str(args.get("contract_type") or "call").strip().lower()
        side = "puts" if ctype in ("put", "puts", "p") else "calls"
        chain = raw.get("chain") or {}
        contracts = chain.get(side) or []
        avail = sorted({c.get("strike") for c in contracts
                        if c.get("strike") is not None})
        match = None
        if want is not None:
            match = next(
                (c for c in contracts
                 if c.get("strike") is not None
                 and abs(float(c["strike"]) - want) < 1e-6),
                None,
            )
        if match is None:
            return {
                "status": "no_strike",
                "symbol": symbol,
                "contract_type": "put" if side == "puts" else "call",
                "expiration_iso": chain.get("expiration_iso"),
                "error": (
                    f"no {('put' if side == 'puts' else 'call')} at strike "
                    f"{strike_raw} on {symbol} {chain.get('expiration_iso')}"
                ),
                "available_strikes": avail[:40],
                "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            }
        return {
            "status": "ok",
            "symbol": symbol,
            "contract": {
                "strike": match.get("strike"),
                "contract_type": "put" if side == "puts" else "call",
                "expiration_iso": chain.get("expiration_iso"),
                "open_interest": match.get("openInterest"),
                "volume": match.get("volume"),
                "implied_volatility": match.get("impliedVolatility"),
                "bid": match.get("bid"),
                "ask": match.get("ask"),
                "last_price": match.get("lastPrice"),
                "underlying_spot_price": raw.get("underlying_spot_price"),
            },
            "history_note": (
                "SNAPSHOT ONLY — this is the current OI/volume/IV. No "
                "multi-day history is available; for a 5-day OI trend "
                "tell the asker to pull it from their broker."
            ),
            "available_expirations": expirations[:12],
            "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }

    summary = _md.summarize_options_chain(raw)
    return {
        "status": "ok",
        "summary": summary,
        "available_expirations": expirations[:12],
        "as_of": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    }


def _build_fantasy_league_tool():
    """FunctionDeclaration for `lookup_fantasy_league` — live data from
    the room's Sleeper league (settings.sleeper_league_id). Registered
    only when the league id is configured."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_fantasy_league",
                description=(
                    "Live data from the room's Sleeper fantasy football "
                    "league (Omnibeta Degens). Use for ANY question "
                    "about the fantasy league: standings, records, "
                    "matchup scores, rosters, waiver/trade activity, "
                    "draft picks, who's trending, projections. Managers "
                    "are resolved to their Discord identities.\n"
                    "`topic` (required): 'league' (settings + who's in "
                    "it) | 'standings' (records + points) | 'matchups' "
                    "(scores for a week) | 'roster' (one manager's "
                    "starters + bench — requires `member`) | "
                    "'transactions' (waivers/trades/FAAB, recent) | "
                    "'draft' (all picks + rosters_by_manager; USE THIS for draft grading/review/who-drafted-best questions, NOT standings, which is all zeros pre-season) | 'trending' (adds/drops across "
                    "all of Sleeper) | 'projections' (projected PPR "
                    "points; optional `member` for their starters).\n"
                    "`week`: NFL week number (defaults to the current "
                    "week).\n"
                    "`member`: a Discord username/display name or "
                    "Sleeper name, for topic='roster' or "
                    "'projections'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "topic": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "league | standings | matchups | roster "
                                "| transactions | draft | trending | "
                                "projections"
                            ),
                        ),
                        "week": types.Schema(
                            type=types.Type.INTEGER,
                            description="NFL week (default: current).",
                        ),
                        "member": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Manager to look up (discord or sleeper "
                                "name) — for roster/projections."
                            ),
                        ),
                    },
                    required=["topic"],
                ),
            )
        ]
    )


async def _execute_fantasy_league(args: dict) -> dict:
    """Run the lookup_fantasy_league tool call. All Sleeper I/O is
    synchronous urllib in report/sleeper_data — run in a thread. Player
    IDs translate through the sleeper_players DB cache, lazily refreshed
    on first use / when older than 26h (the daily scheduler job is the
    primary refresher; this is the backstop)."""
    from report import sleeper_data as _sd

    league_id = (settings.sleeper_league_id or "").strip()
    if not league_id:
        return {
            "status": "error",
            "error": (
                "fantasy league not configured (SLEEPER_LEAGUE_ID unset) "
                "— say the fantasy lookup isn't available."
            ),
        }

    def _sync() -> dict:
        age = db.sleeper_players_cache_age_hours()
        if age is None or age > 26:
            try:
                n = db.upsert_sleeper_players(_sd.fetch_players_trimmed())
                log.info(f"sleeper players cache refreshed ({n} rows)")
            except Exception as e:
                # stale cache still translates most ids; empty cache
                # degrades to raw ids, which the payload surfaces
                log.warning(f"sleeper players cache refresh failed: {e}")
        return _sd.build_topic_payload(
            league_id,
            (args.get("topic") or "standings"),
            week=args.get("week"),
            member=(args.get("member") or "").strip() or None,
            player_name_resolver=db.get_sleeper_player_names,
        )

    try:
        result = await asyncio.to_thread(_sync)
    except Exception as e:
        log.warning(f"lookup_fantasy_league failed: {e}")
        return {
            "status": "error",
            "error": (
                f"Sleeper API unavailable ({str(e)[:120]}) — say the "
                "lookup failed. Do NOT invent league data."
            ),
        }
    return result


def _build_market_price_tool():
    """FunctionDeclaration for `lookup_market_price`. Routes symbols
    to Finnhub (stocks) or Binance.US (crypto) based on a hardcoded
    allowlist. Returns a session-labeled snapshot."""
    from google.genai import types
    return types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="lookup_market_price",
                description=(
                    "Get prices for stocks / ETFs / indices and crypto. "
                    "Pass a list of symbols (cap 10 per call). Response "
                    "includes per-symbol price, change_pct, source, "
                    "data_freshness, plus a session label ('OPEN' | "
                    "'PRE-MARKET' | 'AFTER-HOURS' | 'WEEKEND-CLOSED').\n\n"
                    "DATA FRESHNESS PER SYMBOL — check `data_freshness` "
                    "on each quote before phrasing the move:\n"
                    "  - 'live_regular_session' — OPEN-session live "
                    "Finnhub price. Describe as 'session-to-date' or "
                    "'right now'.\n"
                    "  - 'live_extended_hours' — Yahoo extended-hours "
                    "print. `price` is the actual last AH/PRE trade; "
                    "`change_pct` is from PRIOR-day close (full move "
                    "incl. AH); `extended_hours_change_pct` is the AH "
                    "move from today's regular close; "
                    "`regular_session_close` is today's 4 PM close. "
                    "Describe as 'after-hours at $X (closed $Y, then "
                    "moved Z% in AH)'.\n"
                    "  - 'regular_session_close' — Finnhub fallback "
                    "when Yahoo AH/PRE data was unavailable. `price` "
                    "is the 4 PM close. Tell the asker the AH/PRE "
                    "print is not in your feed; quote the cash close "
                    "as a reference. Do NOT phrase as 'after-hours "
                    "at $X' — it's the 4 PM close.\n\n"
                    "`stock_quote_data_caveat` on the top-level "
                    "response will flag if any stock fell back to the "
                    "regular close. None when every stock has live "
                    "extended-hours data or session is OPEN.\n\n"
                    "Crypto IS live 24/7 regardless of session - "
                    "phrase BTC/ETH normally.\n\n"
                    "Use for 'what's TSLA at', 'how's BTC doing', "
                    "'is SPY green today', 'GTLB after earnings'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "symbols": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description="Symbol tickers, e.g. ['TSLA', 'BTC'].",
                        ),
                    },
                ),
            )
        ]
    )


async def _execute_market_price(args: dict) -> dict:
    """Run the lookup_market_price tool call.

    Routes each symbol to Finnhub or Binance.US, collects per-symbol
    responses, prepends a session label so the model can phrase
    correctly. Per-symbol failures don't sink the batch.
    """
    from datetime import datetime
    import pytz
    from report import market_data as _md

    symbols_in = args.get("symbols")
    if not symbols_in or not isinstance(symbols_in, list):
        return {"error": "symbols list cannot be empty"}

    symbols = [str(s).strip().upper() for s in symbols_in if isinstance(s, str) and str(s).strip()]
    if not symbols:
        return {"error": "symbols list cannot be empty"}

    truncated_to = None
    if len(symbols) > 10:
        symbols = symbols[:10]
        truncated_to = 10

    # Session label from existing market_data helper. The helper returns
    # BOTH a short code AND an explanatory note — previously we threw the
    # note away (`_note`) and only kept the code. That meant Gemini saw
    # session=AFTER-HOURS + a price but no warning that the Finnhub /quote
    # response on AH/PRE is the regular-session CLOSE, not a live extended-
    # hours print. Wock asked about TSLA/GTLB after-hours on 2026-06-02
    # and got the 4 PM close quoted as if it were the live AH print. The
    # note now flows through to the tool response + an AH/PRE-specific
    # data-quality warning gets appended so the model phrases correctly.
    et = pytz.timezone("America/New_York")
    now_et = datetime.utcnow().replace(tzinfo=pytz.UTC).astimezone(et)
    try:
        session_code, session_note = _md._session_label(now_et)
    except Exception:
        session_code = "UNKNOWN"
        session_note = ""

    # Stock-quote data-quality caveat for sessions where Finnhub /quote
    # does NOT return live extended-hours data. The price field for a
    # stock on AFTER-HOURS / PRE-MARKET / WEEKEND-CLOSED is the most
    # recent REGULAR-session close, not the live extended-hours print.
    # Crypto via Binance.US is always live so this caveat doesn't apply
    # to it — the model should still phrase BTC/ETH as 'right now'.
    quote_data_caveat = ""
    if session_code == "AFTER-HOURS":
        quote_data_caveat = (
            "STOCK PRICES BELOW = today's regular-session CLOSE (4 PM ET). "
            "Finnhub /quote does NOT return live after-hours prints. If the "
            "asker explicitly wants after-hours movement (e.g. on an earnings "
            "name like GTLB / NVDA / MSFT that reported AMC), tell them the "
            "AH print is not in your data feed and quote the cash close as "
            "a reference point. Don't phrase the price as 'after-hours at $X' "
            "— it's the 4 PM close. Crypto prices ARE live."
        )
    elif session_code == "PRE-MARKET":
        quote_data_caveat = (
            "STOCK PRICES BELOW = YESTERDAY'S regular-session close. Finnhub "
            "/quote does NOT return live pre-market prints. Phrase as "
            "'yesterday's close' not 'pre-market price at $X'. Crypto prices "
            "ARE live."
        )
    elif session_code == "WEEKEND-CLOSED":
        quote_data_caveat = (
            "STOCK PRICES BELOW = FRIDAY'S regular-session close. Markets "
            "are closed for the weekend. Crypto prices ARE live."
        )

    timestamp = now_et.strftime("%Y-%m-%d %H:%M %Z")

    quotes: list[dict] = []
    for sym in symbols:
        if sym in _CRYPTO_SYMBOLS:
            cq = await _crypto_quote(sym)
            if cq:
                quotes.append(cq)
            else:
                quotes.append({
                    "symbol": sym,
                    "error": f"no live feed for {sym} (Binance.US quote "
                             f"unavailable right now)",
                })
        else:
            # During AFTER-HOURS / PRE-MARKET, try Yahoo first — it
            # surfaces the actual extended-hours print via the v8
            # chart endpoint. Yahoo can rate-limit datacenter IPs
            # intermittently; fall back to Finnhub on any failure or
            # if Yahoo's reported session doesn't match what we expect.
            yh = None
            expected_yh_session = (
                "post" if session_code == "AFTER-HOURS"
                else "pre" if session_code == "PRE-MARKET"
                else None
            )
            if expected_yh_session:
                try:
                    # to_thread: sync urllib I/O — never on the event loop.
                    yh = await asyncio.to_thread(
                        _md._fetch_yahoo_extended_hours, sym
                    )
                except Exception as e:
                    log.info(f"yahoo AH lookup for {sym} raised: {e}")
                    yh = None

            # Use Yahoo's extended-hours print iff it actually returned
            # a bar from the expected session. Otherwise fall through
            # to Finnhub (regular-session close).
            if (
                yh
                and yh.get("last_session") == expected_yh_session
                and yh.get("last_price") is not None
                and yh.get("regular_close")
                and yh.get("prev_close")
            ):
                last_price = float(yh["last_price"])
                regular_close = float(yh["regular_close"])
                prev_close = float(yh["prev_close"])
                # change_pct vs prior REGULAR close (so the asker
                # sees the full day-over-day move including the AH
                # action). Also surface ah_change_pct = AH move from
                # the regular close so the model can describe both:
                # "GTLB closed +X% then dropped Y% after-hours."
                change_pct = (
                    (last_price - prev_close) / prev_close * 100.0
                    if prev_close
                    else None
                )
                ah_change_pct = (
                    (last_price - regular_close) / regular_close * 100.0
                    if regular_close
                    else None
                )
                quotes.append({
                    "symbol": sym,
                    "price": last_price,
                    "change_pct": change_pct,
                    "prev_close": prev_close,
                    "regular_session_close": regular_close,
                    "extended_hours_change_pct": ah_change_pct,
                    "source": "yahoo_extended_hours",
                    "data_freshness": "live_extended_hours",
                })
                continue

            try:
                # to_thread: sync urllib I/O — never on the event loop.
                data = await asyncio.to_thread(_md._fetch_finnhub_quote, sym)
            except Exception as e:
                data = None
                log.info(f"finnhub quote for {sym} raised: {e}")
            if not data:
                # Not a resolvable US stock — try a Binance.US crypto
                # pair before giving up (dynamic crypto coverage: SUI,
                # PEPE, new listings that aren't in the priority set).
                cq = await _crypto_quote(sym)
                if cq:
                    quotes.append(cq)
                    continue
                quotes.append({
                    "symbol": sym,
                    "error": f"no live feed for {sym} — not a recognized "
                             f"US stock or Binance.US crypto pair",
                })
                continue
            quotes.append({
                "symbol": sym,
                "price": data.get("price"),
                "change_pct": data.get("change_pct"),
                "prev_close": data.get("prev_close"),
                "source": "finnhub",
                # During extended-hours sessions, Finnhub /quote is the
                # regular-session close (not live). Tag it so Gemini
                # can phrase correctly even when symbols mix sources.
                "data_freshness": (
                    "regular_session_close"
                    if session_code in ("AFTER-HOURS", "PRE-MARKET",
                                        "WEEKEND-CLOSED")
                    else "live_regular_session"
                ),
            })

    # Caveat applicability depends on how Yahoo did:
    #   all stocks got live AH/PRE data       -> drop the caveat
    #   all stocks fell back to Finnhub close -> keep the original caveat
    #   mixed (some live AH, some stale)      -> narrow caveat to stale ones
    if quote_data_caveat and quotes:
        stock_quotes = [q for q in quotes if q.get("source") != "binance"
                        and "error" not in q]
        if stock_quotes:
            live_ah_count = sum(
                1 for q in stock_quotes
                if q.get("data_freshness") == "live_extended_hours"
            )
            stale_symbols = [
                q["symbol"] for q in stock_quotes
                if q.get("data_freshness") == "regular_session_close"
            ]
            if live_ah_count == len(stock_quotes):
                # All stocks have live AH/PRE data — caveat doesn't apply.
                quote_data_caveat = None
            elif live_ah_count > 0 and stale_symbols:
                # Mixed — narrow the caveat to the stale ones.
                quote_data_caveat = (
                    f"PARTIAL DATA: {','.join(stale_symbols)} fell back to "
                    f"Finnhub regular-session close (Yahoo AH/PRE data was "
                    f"unavailable for those tickers). Other tickers have "
                    f"LIVE extended-hours prints. Per-symbol data_freshness "
                    f"field tells you which is which."
                )
            # else: all stocks stale -> keep original Fix A caveat unchanged

    result = {
        "session": session_code,
        "session_note": session_note,
        "stock_quote_data_caveat": quote_data_caveat or None,
        "timestamp": timestamp,
        "quotes": quotes,
    }
    if truncated_to is not None:
        result["truncated_to"] = truncated_to
    return result


async def _fetch_chat_context(
    channel,
    *,
    exclude_message_id: int | None = None,
    bot_user_id: int | None = None,
    bot_client=None,
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

    Image attachments: lazy-OCR'd via chat_ingestion.ocr — capped at
    settings.ask_image_ocr_max_per_call per /ask call. OCR text is
    injected into the line as `[IMAGE: ...]` so Gemini knows what's
    user text vs what's image content.

    Returns (block_text, author_ids) — empty string + empty list on any
    failure or when there's nothing usable. Empty-string fall-through is
    intentional — the caller treats it as "no context, proceed normally."

    `author_ids` is the set of distinct user IDs seen in the context window,
    excluding the bot itself. Used by /ask to fetch personality profiles
    for the people active in this conversation.

    `exclude_message_id` is the @mention message itself when invoked from
    on_message — we don't want to feed the bot its own prompt as context.

    `bot_client` (optional discord.Client) enables lazy image OCR. When
    None, image attachments are skipped (no [IMAGE:...] markers added).
    """
    if channel is None:
        return "", []
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_ASK_CONTEXT_MAX_AGE_MIN)
    # Each element: (timestamp, line_template, ocr_target). line_template
    # contains a "{IMAGE_BLOCK}" placeholder for messages whose images
    # we plan to OCR; ocr_target is the (msg_id, channel_id) tuple. Lines
    # without images use line_template directly (no placeholder).
    collected: list[tuple[datetime, str, tuple[int, int] | None]] = []
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
            # Detect image attachments — we'll OCR these later (lazy path).
            has_image = any(
                (getattr(a, "content_type", "") or "").startswith("image/")
                for a in (msg.attachments or [])
            )
            if not text and not has_image:
                continue  # nothing usable — pure sticker / video / non-image
            text = (text or "")[:_ASK_CONTEXT_PER_MSG_CHARS]
            ocr_target: tuple[int, int] | None = None
            if has_image and bot_client is not None:
                ocr_target = (msg.id, msg.channel.id)
                image_placeholder = " {IMAGE_BLOCK}"
            else:
                image_placeholder = ""
            # Tag the bot's own past replies distinctly so Gemini can recognize
            # which lines are its prior output. Without this, the bot sees its
            # own embed-stripped replies as "BotName: <text>" and treats them
            # like any other user — leading to loops where it repeats the same
            # canned take across multiple calls without realizing it.
            if bot_user_id is not None and msg.author.id == bot_user_id:
                line = f"[YOU said earlier]: {text or '(image)'}{image_placeholder}"
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
                line = f"{speaker}: {text or '(image)'}{image_placeholder}"
                # Track distinct non-bot authors for the profile lookup
                if not msg.author.bot:
                    author_ids.add(msg.author.id)
            collected.append((msg.created_at, line, ocr_target))
    except discord.Forbidden:
        log.info("Chat-context fetch: missing Read Message History permission")
        return "", []
    except Exception as e:
        log.warning(f"Chat-context fetch failed (non-fatal): {e}")
        return "", []
    if not collected:
        return "", []
    collected.sort(key=lambda t: t[0])  # oldest → newest

    # Lazy OCR pass (Phase 1). Walk the collected list, find OCR targets,
    # run up to settings.ask_image_ocr_max_per_call in parallel via
    # asyncio.gather. Cached OCR text (chat_messages.image_ocr_status
    # already set) returns immediately without a Gemini call, so eager-
    # OCR'd messages don't count toward the per-/ask cap.
    if bot_client is not None:
        collected = await _resolve_ocr_targets(collected, bot_client)

    body = "\n".join(line for _, line, _ in collected)
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


# Repetition-glitch detector. Catches Flash-Lite token-loop artifacts
# at the END of answers (the dominant pattern in the 2026-05-30 ask log):
#   "compounding risk and volatility decay risks of volatility decay and
#    volatility" — "volatility decay" 3x in a 12-token tail
# Two checks:
#   (1) Tail-frequency: any content-word (>=4 chars, not stopword)
#       appearing 3+ times in the last 15 alpha tokens. Catches the
#       IBM "volatility" 3x pattern cleanly.
#   (2) Tail-bigram: any content-word bigram appearing 2+ times in the
#       last 25% of tokens. Catches "volatility decay … volatility decay"
#       at end-of-sentence without flagging "long $TLT" repeats in the
#       middle of a normal multi-arrow answer.
# Both gates require the repetition to be in the TAIL of the response —
# the loop pattern is end-of-generation. Mid-response repetitions
# (legitimate "X then Y, X then Z" structures) are not flagged.
_REP_TOKEN_RE = re.compile(r"[a-zA-Z]+")
_REP_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at",
    "to", "for", "with", "is", "are", "was", "were", "be", "by",
    "that", "this", "it", "as", "if", "from", "than", "then",
    "into", "onto", "you", "your", "we", "our", "they", "them",
    "their", "his", "her", "she", "he", "i", "me", "my", "mine",
    "not", "no", "yes", "do", "does", "did", "have", "has", "had",
    "will", "would", "should", "could", "can", "may", "might", "must",
    "so", "up", "down", "out", "over", "under", "off", "via", "per",
})


def _strip_voice_sections(
    profiles_block: str, reason: str = "omitted on retry to clear filter",
) -> str:
    """Remove `**Voice.**` bullet sections from every profile in the
    profiles_block while keeping the rest of each profile intact.

    `reason` fills the stub left behind. The default reads as a retry
    because that was the only caller until profile depth moved to
    assembly time (2026-08-10); the proactive caller passes its own.

    Used by the filter-blocked retry path. Empirical testing
    (2026-06-03 19:49 UTC reproduction) showed that the Voice section
    is the highest-density slur container in any given profile (it
    quotes the user's chat verbatim, slurs included) and dropping
    only that one subsection drops the prompt below Gemini's
    unconfigurable filter threshold while leaving the analytical
    framing (rationale, Personality and style, Retarded takes, Recent
    trades, Recent personal life) so the bot can still answer with
    profile-aware framing.

    Section boundaries follow the schema in WHO'S TALKING (see
    `_ASK_SYSTEM_INSTRUCTION`):

        **Voice.**
        - "X" — [context]
        - "Y" — [context]
        ...
        <ends at next **Section.** OR a blank line followed by
         a non-bullet line OR end of the profile bullet>

    The replacement leaves a `**Voice.**\\n- (omitted)\\n` stub so the
    section header still appears in the prompt and the schema isn't
    visibly broken — model still sees this user has a Voice section,
    just no verbatim samples.
    """
    import re as _re
    return _re.sub(
        r"\*\*Voice\.\*\*[ \t]*\n(?:[ \t]*- .*\n)+",
        f"**Voice.**\n- ({reason})\n",
        profiles_block,
    )


# Slur-token regex used by the third-tier mask retry. Tokens are
# replaced with `[redacted]` in chat context + question + voice-stripped
# profile when the Voice-strip retry ALSO returned empty. The mask is
# lossy — the bot can't quote the slur verbatim — but the answer
# survives the filter where the previous two attempts didn't.
#
# Covers the same token family as the other slur checks in this module
# (_KNOWN_SLURS + variants caught by the voice-strip regex). Word-
# boundary anchored so we don't mask "Pajama" or similar near-matches.
_SLUR_MASK_RE = re.compile(
    r"\b(?:nigg[ae]r?s?|chink|spic|kike|fag(?:got)?|pajeet)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------
# PROFILE DEPTH — decided at prompt assembly, not after a filter block.
#
# Structural note (2026-08-10). Every filter fix before this one lived on
# the RECOVERY path: voice-strip retry (06-03), slur-mask retry (06-04),
# question-only retry, tier-0 identical resend (08-04), tools stripped
# from the ladder config (08-07), google_search kept in it (08-10). Six
# fixes, one unchanged premise — build the maximal prompt, send it, and
# find out it was too hot only when Gemini rejects it. The ladder grew a
# rung per incident and each rung shipped its own bug.
#
# The premise is the defect. The profile block is the filter bait, and
# whether the ask needs that bait is knowable BEFORE the send: a question
# about palladium options needs none of it, a roast needs all of it. So
# profile depth becomes an assembly-time decision with a stamped reason,
# and the ladder drops back to what it should always have been — a net
# for the residual flicker on prompts that were already trimmed.
#
# The bait is in three containers, only one of which the ladder could
# ever reach:
#   1. **Voice.** verbatim chat samples          — _strip_voice_sections
#   2. the racism-signal metric + its rationale prose  (header line)
#   3. the "recent slur usage (regex fallback)" examples block
# (2) and (3) sit outside **Voice.**, so every voice-strip retry left
# them in. 2026-08-09's block is the tell: Ry_bry's Voice samples are
# mild (humor:12/100, no slurs) and the ask still died.

_PROFILE_METRICS_RE = re.compile(r'^(- \*\*.*?— )_([^_]*)_(:.*)$', re.M)
_SLUR_EXAMPLES_RE = re.compile(
    r'^[ \t]*recent slur usage \(regex fallback\):\n(?:[ \t]*· .*\n?)*',
    re.M,
)
_PROFILE_NAME_RE = re.compile(r'^- \*\*(.+?)\*\* \(([^,)]+)', re.M)

# Question shapes that genuinely need the person material. Deliberately
# broad: a false FULL costs an occasional filter flicker (the ladder
# still covers it), a false LEAN silently degrades a roast — which is
# the product. When in doubt this must answer True.
_PERSON_QUESTION_RE = re.compile(
    r'\b(roast|clown|cook|dunk|drag|flame|insult|shit\s*talk|'
    r'clap\s*back|jab|burn|mock|make\s*fun|'
    r'who(?:\'s| is| are)?\b|whose|which (?:one|member|guy|trader)|'
    r'worst|best|most|least|top \d|rank(?:ing|ed)?|leaderboard|'
    r'profile|dossier|personality|vibe|say about|think about|'
    r'compare|versus|vs\.?)\b',
    re.IGNORECASE,
)


def _profile_names_in_block(profiles_block: str) -> list[str]:
    """Display names + usernames of everyone loaded into WHO'S TALKING."""
    out: list[str] = []
    for disp, uname in _PROFILE_NAME_RE.findall(profiles_block or ""):
        for n in (disp, uname):
            n = (n or "").strip()
            if len(n) >= 3:
                out.append(n)
    return out


def _question_needs_person_material(
    question: str, profiles_block: str,
) -> tuple[bool, str]:
    """Does this ask need the Voice samples / racism signal?

    Returns (needs, reason). Reason is stamped into the ask-log so the
    decision is auditable rather than forensic.
    """
    q = question or ""
    if "[MESSAGE BEING REPLIED TO" in q:
        # Replies are aimed at a person by construction.
        return True, "reply-to-member"
    if re.search(r"<@!?\d+>", q):
        return True, "mentions-member"
    try:
        if _is_slur_count_question(q) or _is_message_count_question(q):
            # These ARE the racism/message analytics — the material is
            # the answer, not decoration.
            return True, "room-analytics"
    except Exception:
        return True, "detector-error"
    low = q.lower()
    for name in _profile_names_in_block(profiles_block):
        if re.search(rf"\b{re.escape(name.lower())}\b", low):
            return True, "names-member"
    if _PERSON_QUESTION_RE.search(q):
        return True, "person-shape"
    return False, "impersonal"


def _lean_profiles_for_prompt(profiles_block: str) -> str:
    """The profile block minus all three filter-bait containers.

    Keeps what makes the bot sound like it knows the room — Personality
    and style, trader-rank + rationale, Retarded takes, Recent trades,
    Recent personal life — so an impersonal question still gets an
    in-register answer addressed to a person the bot recognises.

    The voice-strip half of this is the empirically validated shape:
    "strip Voice only -> PASSES (3/3 runs)" (2026-06-03 reproduction).
    LEAN drops strictly more, so it clears by at least as much.
    """
    if not profiles_block:
        return profiles_block
    out = _strip_voice_sections(
        profiles_block, reason="not needed for this question",
    )
    out = _SLUR_EXAMPLES_RE.sub("", out)

    def _drop_racism_bit(m: re.Match) -> str:
        bits = [b for b in m.group(2).split(" · ")
                if not b.lstrip().lower().startswith("racism")]
        return f"{m.group(1)}_{' · '.join(bits)}_{m.group(3)}"

    return _PROFILE_METRICS_RE.sub(_drop_racism_bit, out)


def _mask_slur_tokens(text: str) -> str:
    """Replace slur tokens with `[redacted]` placeholders.

    Used by the third-tier filter-blocked retry. The Voice-strip retry
    (commit 137e310) handles profile-section slurs; this catches the
    residual cases where chat context + question text combined push
    the prompt across the unconfigurable filter threshold even with
    Voice stripped.

    Concrete failure pattern this addresses (observed 2026-06-04 16:57
    UTC, 4 trips in 90 seconds): asker explicitly asks about slur
    usage; question text contains the slur literally; chat context
    carries the room's normal slur register; Voice strip doesn't
    touch either; Voice-strip retry returns empty same as the first
    attempt. The Python-side slur-count short-circuit (commit 9864a4a)
    catches the EXPLICIT count-shaped questions. This mask is the
    belt-and-suspenders for other prompts where slur density
    accidentally trips the filter.
    """
    if not text:
        return text
    return _SLUR_MASK_RE.sub("[redacted]", text)


def _has_repetition_glitch(text: str) -> bool:
    """Detect end-of-response repetition loops. See module-level note
    above for the two heuristics."""
    if not text:
        return False
    tokens = [m.group(0).lower() for m in _REP_TOKEN_RE.finditer(text)]
    if len(tokens) < 6:
        return False

    # Gate 0: immediate exact duplicate of any alpha token ("is is",
    # "the the", "turned turned"). The other gates skip these — Gate 1
    # needs a content word 3x, Gates 2/3 need a content token in the
    # bigram, so a doubled stopword slips entirely. Immediate verbatim
    # token repetition is glitch-characteristic; the mechanical
    # collapser fixes it, this gate just makes the retry + QC log fire.
    for i in range(len(tokens) - 1):
        if tokens[i] == tokens[i + 1] and len(tokens[i]) >= 2:
            return True

    # Gate 1: tail frequency. Any content-word repeating 3+ times in
    # the last 15 alpha tokens of the answer.
    tail = tokens[-15:]
    counts: dict[str, int] = {}
    for t in tail:
        if t in _REP_STOPWORDS or len(t) < 4:
            continue
        counts[t] = counts.get(t, 0) + 1
    if counts and max(counts.values()) >= 3:
        return True

    # Gate 2: tail bigram. Any content-word bigram (both non-stopword,
    # >=4 chars) appearing 2+ times in the last 25% of the answer.
    tail_start = max(0, int(len(tokens) * 0.75))
    tail_tokens = tokens[tail_start:]
    bigram_counts: dict[tuple[str, str], int] = {}
    for i in range(len(tail_tokens) - 1):
        a, b = tail_tokens[i], tail_tokens[i + 1]
        if a in _REP_STOPWORDS or b in _REP_STOPWORDS:
            continue
        if len(a) < 4 or len(b) < 4:
            continue
        bg = (a, b)
        bigram_counts[bg] = bigram_counts.get(bg, 0) + 1
    if any(c >= 2 for c in bigram_counts.values()):
        return True

    # Gate 3: loose tail bigram. Catches glitches that Gates 1+2 miss
    # because the loop is "STOPWORD CONTENT STOPWORD CONTENT" or
    # "CONTENT STOPWORD CONTENT STOPWORD" patterns. 2026-06-01 shipped
    # three glitches the original gates missed:
    #   - AVGO: "will get punished by the hyperscalers will get
    #     punished instantly kills the momentum will get punished
    #     hardliners will get punished hard" (("will","get") was
    #     filtered out by Gate 2's >=4-char filter on both sides)
    #   - HPE: "...the lumpiness that plagued previous quarters past
    #     quarters-lumpy delays that plagued recent-history narrative
    #     dragging delays" (("that","plagued") had a stopword)
    #   - TMO: "$TMO is the first to the first to see the volume"
    #     (("the","first") had a stopword)
    # The fix: bigrams over the last 25 tokens, stopwords allowed,
    # require at least one bigram token to be a content word (>=4
    # chars, not in stopword list). This catches the patterns above
    # while filtering out pure-stopword filler like ("of", "the").
    loose_tail = tokens[-25:]
    loose_counts: dict[tuple[str, str], int] = {}
    for i in range(len(loose_tail) - 1):
        a, b = loose_tail[i], loose_tail[i + 1]
        # At least one side must be a "content" token (>=4 chars
        # AND not in the stopword list). The other side is anything.
        a_content = len(a) >= 4 and a not in _REP_STOPWORDS
        b_content = len(b) >= 4 and b not in _REP_STOPWORDS
        if not (a_content or b_content):
            continue
        bg = (a, b)
        loose_counts[bg] = loose_counts.get(bg, 0) + 1
    return any(c >= 2 for c in loose_counts.values())


def _repetition_glitch_sentences(text: str) -> list[str]:
    """Sentences/bullets of `text` that individually trip the glitch
    detector. Strip-fallback input (2026-07-22 terlin calendar answer:
    the one-shot retry re-glitched and the failure path shipped the
    loop untouched — excising just the glitching bullet keeps the
    clean bullets deliverable). The per-sentence check inherits the
    detector's >=6-token floor, so short clean bullets never match."""
    return [
        s for s in _split_sentences(text)
        if _has_repetition_glitch(s)
    ]


# =====================================================================
# Grounding backstop — structural enforcement that a MARKET-FACT answer
# actually consulted a source. Gemini's Google-Search grounding is
# DISCRETIONARY (the model decides per-answer); "Type 1 always searches"
# is a prompt instruction it can ignore — observed 2026-06-17: the SPCX
# unlock schedule was confabulated from priors with zero grounding and
# zero tool calls. This detects the signature (hard market specifics +
# NO grounding + NO data tool) so the caller can force a grounded retry
# before posting. Scoped to MARKET-fact shapes (price targets, event
# dates, unlock/float/tranche numbers, dense specifics) so it never
# misfires on the room's roast register, which cites the odd personal
# dollar figure ("$3,500 soccer tickets") but no analyst-fact shapes.
# =====================================================================

# Strong single-marker shapes — analyst/corporate-action facts a roast
# essentially never produces. NOTE (2026-06-19): bare calendar dates
# ("June 19", "2026-06-19") were REMOVED — they fired on benign
# common-knowledge answers ("market closes 4 PM, closed Friday June 19")
# and stamped correct answers with the unverified hedge. Dates only
# matter as a confab signal when they ride alongside an unlock/lockup/
# tranche/PT shape, which the remaining markers already catch.
_MARKET_FACT_STRONG_RE = re.compile(
    r"(\bPT\s*\$?\d"
    r"|\bprice target\b"
    r"|\b(?:un)?lock(?:up|ed|s)?\b[^.\n]{0,30}?\d"
    r"|\btranche\b"
    r"|\bfloat\b[^.\n]{0,20}?\d"
    r"|\b(?:consensus|estimate[ds]?|forecast)\b[^.\n]{0,25}?\d"
    # Valuation shapes (2026-07-06 CXW/GEO: "GEO's market capitalization
    # sitting at roughly $4.04 billion" was a confabulated live figure
    # that escaped the density net because it used no $-cashtag — "GEO"
    # not "$GEO"). A market-cap / EV / shares-outstanding claim with a
    # number is a live market fact regardless of cashtag; if nothing
    # grounded it, hedge. Requires the phrase to sit near a figure so
    # "enterprise value matters" without a number doesn't trip.
    r"|\bmarket\s+cap(?:italization)?\b[^.\n]{0,25}?\$?\d"
    r"|\benterprise\s+value\b[^.\n]{0,25}?\$?\d"
    # shares-outstanding figure sits on EITHER side ("130M shares
    # outstanding" / "shares outstanding of 130M")
    r"|\d[^.\n]{0,20}?\bshares?\s+outstanding\b"
    r"|\bshares?\s+outstanding\b[^.\n]{0,25}?\d"
    r"|\b(?:valued|valuation)\b[^.\n]{0,20}?\$\d)",
    re.IGNORECASE,
)
# Generic specifics — only meaningful in DENSITY (>=3 ≈ a schedule/
# data-dump answer, not a one-off roast number).
_GENERIC_SPECIFIC_RE = re.compile(
    r"(\$\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b)",
)
# A cashtag ($ + LETTER, so "$250k"/"$10M" dollar amounts never match).
# The density net only counts when the answer is about a SPECIFIC
# SECURITY — otherwise it misfires on general-knowledge explainers that
# repeat plain dollar figures (2026-06-29: an FDIC explainer with
# "$250k"/"$10M" tripped the >=3 net and got the unverified hedge).
_BACKSTOP_CASHTAG_RE = re.compile(r"\$[A-Za-z]{1,6}\b")


def _grounding_has_sources(gm) -> bool:
    if gm is None:
        return False
    return bool(getattr(gm, "grounding_chunks", None) or [])


def _is_ungrounded_market_fact(answer: str, grounding_metadata,
                               tool_trace: list,
                               context: str = "") -> bool:
    """True when an answer asserts market-fact specifics but consulted NO
    source (no Google grounding, no data tool). The confabulation
    signature. Roast-safe: requires a strong analyst-fact marker OR
    >=3 dense specifics, which a personal-life jab won't have.

    `context` is the full user_content sent to the model. Specifics the
    model is QUOTING from the room (OCR'd screenshots, prior answers,
    recent chat) are not confabulation — see the density-net exemption
    below."""
    if not answer or len(answer) < 25:
        return False
    if _grounding_has_sources(grounding_metadata):
        return False
    if tool_trace:  # any data tool firing counts as a source attempt
        return False
    if _MARKET_FACT_STRONG_RE.search(answer):
        return True
    specifics = _GENERIC_SPECIFIC_RE.findall(answer)
    if len(specifics) < 3:
        return False
    # Room-sourced exemption (2026-08-19: BK asked the bot to re-send a
    # fantasy-odds table whose every figure came verbatim from an OCR'd
    # image in chat + the bot's own prior answer; the density net hedged
    # it and the forced retry mangled the table into a numbered list).
    # If EVERY specific already appears in the injected context, the
    # model is quoting the room, not inventing market data. All-must-
    # match keeps this conservative: one invented number still fires.
    # The strong-marker path above is deliberately NOT exempted — a
    # price-target/float/market-cap claim needs a real source even if
    # the numbers echo something a member once pasted.
    if context and all(s in context for s in specifics):
        return False
    # A named security (cashtag) makes a dense answer a real data-dump.
    if _BACKSTOP_CASHTAG_RE.search(answer):
        return True
    # No ticker: a cluster of PLAIN DOLLAR amounts is a textbook
    # explainer ("FDIC covers $250k, sweep $10M"), not a market claim —
    # don't hedge it. A %/date in the mix means it IS a stat/market claim
    # (e.g. "moved 2% then 3%"), so hedge.
    return any(not s.startswith("$") for s in specifics)


# Price-backstop ticker extraction (2026-07-27 ORCL contradiction:
# two banter answers asserted different ORCL prices minutes apart —
# one wrongly accused the member of inventing a real +4% move — because
# banter passes skip lookup_market_price and the backstop's forced
# retry STRIPS the function tools, so the ladder could hedge but never
# fetch. When the backstop trips on an answer asserting price levels,
# the caller extracts the tickers with this helper, runs the price
# executor directly, and injects the live numbers into the retry.)
_PRICE_ASSERT_NEAR_RE = re.compile(
    r"\$\d|\btrading\s+(?:at|around|near)\b|\b(?:up|down)\s+\d"
    r"|\d+(?:\.\d+)?\s?%",
    re.IGNORECASE,
)
# Uppercase tokens that look like tickers but never are. Prose is
# mostly lowercase, so the residual collision surface is acronyms.
_TICKER_FALSE_POSITIVES = frozenset({
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ETFS", "API", "AI",
    "USD", "EUR", "GBP", "JPY", "GDP", "CPI", "NFP", "PCE", "ISM",
    "PPI", "FOMC", "FED", "SEC", "FDA", "DOJ", "FTC", "IRS", "OI",
    "IV", "RSI", "MACD", "YOY", "QOQ", "EPS", "REV", "RPO", "EV",
    "PE", "PT", "DD", "TA", "AM", "PM", "ET", "UTC", "EST", "EDT",
    "USA", "US", "UK", "EU", "NYSE", "OTC", "ATH", "ATL", "EOD",
    "YTD", "MCAP", "AH", "PLUS", "AND", "THE", "FOR", "NOT", "ALL",
    "OCI", "AWS", "GCP", "LLM", "OPEC", "BLS", "BEA", "AMC", "BMO",
})


def _answer_price_tickers(answer: str) -> list[str]:
    """Tickers named in sentences that assert a price/level/move —
    the symbols the price backstop should fetch before the retry.
    Cashtags always count; bare uppercase tokens count unless they're
    known acronyms. Capped at the price tool's practical batch size."""
    out: list[str] = []
    for s in _split_sentences(answer or ""):
        if not _PRICE_ASSERT_NEAR_RE.search(s):
            continue
        for m in re.finditer(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,6})\b", s):
            sym = (m.group(1) or m.group(2)).upper()
            if not sym.isalpha() or sym in _TICKER_FALSE_POSITIVES:
                continue
            if sym not in out:
                out.append(sym)
    return out[:4]


# Any specific factual claim — numbers, big counts, $-figures, %s,
# month/slash dates, years. Deliberately broad: this is used ONLY on a
# WEB-routed answer (a question the router already decided needs the
# open web), where any stated specific is a fact that must be grounded,
# not a shape to enumerate.
_FACTUAL_SPECIFIC_RE = re.compile(
    r"\$\d"                                             # $4.04, $1.5
    r"|\b\d+(?:\.\d+)?\s?%"                              # 45%
    r"|\b\d[\d,]{3,}\b"                                  # 4-digit+ / year (75,000; 2027)
    r"|\b\d+(?:\.\d+)?\s*(?:million|billion|trillion|bn|k)\b"  # 130 million
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d"  # Aug 2027
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",               # 8/12
    re.IGNORECASE,
)


# Opinion / recommendation requests — the asker wants the bot's OWN
# ranked take, not a verifiable fact. 2026-07-13: kloh asked "review
# [substack] and rank your top 5 most actionable trades" and the good
# in-voice answer ($WOLF/$IREN relative strength, don't martingale
# $CRWV/$PLTR, "$80 support") tripped the web-grounding backstop — a
# "$80" level matched the factual-specific net — and the bare probe
# REPLACED it with a persona-less blog summary that "does not rank
# trades." The specifics in an opinion answer are the bot's PICKS, not
# claims to verify, so the broad web trigger must stand down here.
# Nouns that mark a request for the bot's PICKS (as opposed to a fact).
_PICK_NOUN = (
    r"(?:names?|tickers?|plays?|picks?|setups?|ideas?|trades?|stocks?|"
    r"calls?|puts?|charts?|entr(?:y|ies)|movers?|runners?|lottos?)"
)
_OPINION_REQUEST_RE = re.compile(
    r"\b(?:rank|rate)\b"
    r"|\byour\s+(?:top|best|favou?rite|pick|picks|call|take|read|thoughts?|"
    r"favs?)\b"
    r"|\btop\s+\d+\b"
    r"|\bthoughts?\s+on\b"
    r"|\bpick\s+(?:your|the|a|some|out|favou?rite)\b"
    r"|\bwhat\b[^?\n]{0,25}\byou\s+think\b"
    r"|\bmost\s+actionable\b"
    r"|\brecommend(?:ation)?s?\b"
    r"|\bwould\s+you\s+(?:buy|play|trade|pick|rank)\b"
    # Imperative pick-requests (2026-07-13 kloh: "Give us 5 names from
    # there near optimal entry" got a ChatGPT 'I cannot verify the
    # existence of the report' refusal — 'give us N names' matched none
    # of the above). give/name/show/list/find/get [me/us] [N] <picks>.
    r"|\b(?:give|name|show|list|find|get|throw|drop)\s+"
    r"(?:me|us|him|her|em)?\s*\d*\s*(?:more\s+|other\s+|new\s+)?" + _PICK_NOUN
    # bare "5 names", "3 plays", "a few setups"
    + r"|\b(?:\d+|a\s+few|some|any|which|more)\s+(?:good\s+|solid\s+)?"
    + _PICK_NOUN
    + r"|\bbest\s+" + _PICK_NOUN,
    re.IGNORECASE,
)


def _is_opinion_request(question: str) -> bool:
    """The question asks for the bot's own judgment / picks about
    securities (rank / pick / rate / your-take / 'give us 5 names' /
    best plays). In these, output cashtags and levels are
    RECOMMENDATIONS, not facts — the grounding backstop's broad web
    trigger must not clobber them, and a persona-less 'I cannot verify'
    refusal is the worst possible register."""
    return bool(question and _OPINION_REQUEST_RE.search(question))


# Deictic references that only resolve against the LIVE conversation —
# "from there", "those names", "the report you mentioned", "its Q3".
# A question carrying these is a follow-up, not self-contained, so the
# context-stripping bare probe must not touch it (2026-07-13 kloh: the
# probe answered "give us 5 from there" with "I cannot verify the
# existence of the report" because "there" had no antecedent once
# context was stripped). "is there / are there" (existential) is
# excluded so it doesn't misread "is there a levered SA etf".
_CONTEXT_DEICTIC_RE = re.compile(
    r"\bfrom\s+(?:there|those|them|the\s+(?:report|list|watchlist|note|"
    r"sheet|link|article|thread|screenshot|chart|image))\b"
    r"|\b(?:out\s+of|of|from|in|on|among)\s+(?:those|these|them)\b"
    r"|\b(?:those|these)\s+(?:names?|setups?|tickers?|plays?|picks?|ones?|"
    r"charts?|levels?|stocks?|trades?)\b"
    r"|\b(?:that|the)\s+(?:report|list|watchlist|article|note|link|"
    r"screenshot|image|chart)\s+(?:you|we|i|he|she|they|above|earlier)\b"
    r"|\b(?:pick|choose|narrow|rank|sort|order|compare)\s+"
    r"(?:from|among|between|down|them|those|these)\b"
    r"|(?<!is )(?<!are )(?<!isn't )(?<!aren't )\bthere\b\s*(?:near|around|"
    r"under|over|from|that|which|,|\.|$)",
    re.IGNORECASE,
)

# Bracketed context blocks the reply/forward resolver injects ahead of
# the user's actual text — their presence means the ask is ABOUT the
# quoted material, so a bare-probe strip of everything-but-the-tail
# drops exactly what the question references.
#
# 2026-07-16: [VERBATIM RECENT MESSAGES was REMOVED from this list.
# Subject-verbatim blocks are injected whenever another member is
# name-mentioned — usually incidental to the actual question. Counting
# them made "what did ALP report?" (self-contained, searchable) read as
# context-dependent, which skipped the probe and shipped an unverified
# filing story with a hedge. Only reply/forward blocks (the ask is BY
# CONSTRUCTION about them) count; incidental deixis is handled by the
# tail regex.
_REPLY_CONTEXT_MARKERS = (
    "[MESSAGE BEING REPLIED TO",
    "[FORWARDED MESSAGE",
)


# Probe-refusal shapes — a probe that "grounds" a refusal ABOUT the
# question text has not answered anything. 2026-07-16 Cemini: "is glw
# cooked or will i be needing rope and his ladder" (LOCAL/BANTER, room
# in-jokes) drew an excellent in-voice GLW read; the market-shape
# trigger fired on its dense specifics, the bare probe Googled the
# literal room slang, "grounded" one BYU literature page about
# executioners, and its "I cannot verify the terms 'omniwiz' or 'glw'"
# refusal REPLACED the answer because the acceptance check only counted
# grounding chunks. A grounded refusal is a no-ground.
_PROBE_REFUSAL_RE = re.compile(
    r"\b(?:i\s+cannot|i\s+am\s+unable|i'?m\s+unable|unable\s+to\s+"
    r"(?:verify|confirm|find|locate))\b"
    r"|\bcannot\s+(?:verify|confirm|find|locate)\b"
    r"|\bdo(?:es)?\s+not\s+appear\s+in\s+(?:public\s+records|search)"
    r"|\bno\s+(?:public\s+)?(?:records?|information)\s+(?:exists?|"
    r"available|found)\b"
    # Disambiguation essays are the same failure wearing a suit — the
    # probe lost the thread's referent and answered about the WORD
    # instead of the thing (2026-07-15 ALP: "so alp never reported?"
    # became a treatise on alkaline phosphatase, the Australian Labor
    # Party, and the arm's-length principle, "grounded" with 6 sources).
    r"|\bacronym\s+with\s+(?:several|multiple|many)\b"
    r"|\bseveral\s+(?:common|possible|different)\s+meanings?\b"
    r"|\b(?:may|might|could)\s+be\s+referring\s+to\b"
    r"|\bdepending\s+on\s+the\s+context\b"
    r"|\bprovide\s+more\s+context\b",
    re.IGNORECASE,
)


def _probe_is_refusal(text: str) -> bool:
    """The probe answered ABOUT its inability (refusal) or ABOUT the
    ambiguity of the words (disambiguation essay) — either way, not the
    question. Never let it replace a real answer."""
    return bool(text and _PROBE_REFUSAL_RE.search(text))


def _probe_topic_capsule(question: str, prior_answer: str) -> str:
    """One mechanical hint line pinning the probe to the thread's
    SUBJECT — tickers/cashtags harvested from the question and from the
    in-voice answer being verified (the answer knows what the thread is
    about even when the question is three lowercase words). This is the
    structural cure for probe decontextualization: the probe keeps its
    anti-riff property (no room texture to lean on) but can no longer
    read '$ALP' as alkaline phosphatase or 'rope' as an executioner."""
    src = f"{question or ''} {prior_answer or ''}"
    tickers = {t.upper() for t in re.findall(r"\$([A-Za-z]{1,6})\b", src)}
    for t in re.findall(r"\b[A-Z]{2,6}\b", prior_answer or ""):
        if t not in _HOOK_CAPS_STOP:
            tickers.add(t)
    if not tickers:
        return ""
    shown = sorted(tickers)[:6]
    return (
        "\n(Topic context: this question is about the stock ticker(s) "
        + ", ".join(f"${t}" for t in shown)
        + " — search in that financial context.)"
    )


def _is_context_dependent(question: str) -> bool:
    """True when the question only makes sense against the live thread —
    a reply/forward block or a deictic reference ('there', 'those', 'the
    report you mentioned'). The bare probe strips conversation history,
    so it must skip these: stripping the antecedent turns a coherent
    follow-up into an unanswerable fragment."""
    if not question:
        return False
    if any(m in question for m in _REPLY_CONTEXT_MARKERS):
        return True
    # Only inspect the asker's actual message (the tail) for deixis —
    # the injected context blocks above already flagged via the markers.
    tail = question.strip()[-400:]
    return bool(_CONTEXT_DEICTIC_RE.search(tail))


# =====================================================================
# Identity + rewrite-fidelity guards (2026-07-17 "Morgan" incident).
#
# BK asked "Morgan says you don't work very well". The bot had no idea
# who Morgan was, called no tool to find out, and silently dressed the
# ASKER'S OWN dossier up as Morgan ("Morgan? The guy who watches his
# heart rate..."). That wrong mapping entered the recent-chat context
# and every follow-up inherited it ("not even talking about the right
# people", "can we delete this bot"). Separately, the register/roast
# rewrites — which receive ONLY the original answer text — invented
# characterization from thin air ("manifestos", "stoic strategist")
# when told to remove the banned shapes AND keep the length.
# =====================================================================

# Candidate person-name tokens: capitalized word, 3-12 chars. Checked
# against the known-member alias surface; unknowns get a mechanical
# NAME CHECK note. Gated to LOCAL-routed questions so public figures in
# WEB lookups don't trigger it.
_NAME_CAND_RE = re.compile(r"\b([A-Z][a-z]{2,11})\b")
_NAME_CHECK_STOP = frozenset({
    # sentence starters / common words
    "the", "this", "that", "these", "those", "what", "when", "where",
    "which", "who", "whose", "why", "how", "can", "could", "did", "does",
    "do", "is", "are", "was", "were", "will", "would", "should", "shall",
    "has", "have", "had", "you", "your", "yours", "they", "them", "their",
    "there", "then", "than", "and", "but", "not", "yes", "yeah", "nah",
    "okay", "only", "just", "also", "some", "any", "all", "every", "each",
    "one", "two", "three", "give", "tell", "show", "make", "take", "stop",
    "start", "keep", "let", "say", "says", "said", "ask", "asks", "asked",
    "asking", "hey", "yo", "bro", "man", "dude", "guys", "sir", "lol",
    "lmao", "wtf", "omg", "idk", "imo", "btw", "please", "thanks", "thank",
    "with", "from", "about", "into", "over", "under", "after", "before",
    # calendar
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
    "today", "tomorrow", "yesterday", "morning", "afternoon", "evening",
    "night", "week", "month", "year",
    # finance / rooms / geography commonly capitalized in banter
    "market", "stock", "stocks", "calls", "puts", "earnings", "fed",
    "street", "wall", "congress", "senate", "house", "america", "american",
    "china", "chinese", "japan", "israel", "iran", "russia", "europe",
    "korea", "korean", "canada", "mexico", "discord", "twitter", "reddit",
    # public figures the room names constantly (NOT room members)
    "trump", "biden", "powell", "warsh", "musk", "buffett", "messi",
    "yahweh", "jerusalem",
})


def _levenshtein_le(a: str, b: str, cap: int) -> bool:
    """True when edit distance between `a` and `b` is <= cap. Early-exits
    when a full row exceeds the cap."""
    if abs(len(a) - len(b)) > cap:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return False
        prev = cur
    return prev[-1] <= cap


def _near_known_name(tok_low: str, known_low: str) -> bool:
    """True when `tok_low` closely resembles (but isn't) a known member
    surface name — the actual mismap risk the name-check note exists
    for ("Monsoon" vs member "Moonsoon"). v2 contract (2026-07-28):
    the v1 capitalized-token + stoplist heuristic was a false-positive
    treadmill (Great / Sell / Weigh / Alright within days); tokens
    near NO member name carry no confusion risk and never fire — the
    prompt's don't-invent-biography rule owns truly-unknown names."""
    for w in set(re.findall(r"[a-z][a-z._\-]{3,15}", known_low or "")):
        w = w.strip("._-")
        if len(w) < 4 or w == tok_low:
            continue
        cap = 1 if max(len(w), len(tok_low)) <= 7 else 2
        if _levenshtein_le(tok_low, w, cap):
            return True
    return False


def _unknown_member_names(
    question: str, known_names_text: str, limit: int = 3,
) -> list[str]:
    """Capitalized tokens in the asker's message that closely RESEMBLE a
    known member name without matching one — the mismap risk (the bot
    attaching the similar member's material to a near-miss spelling).
    Tokens near no member never fire (v2, see _near_known_name).
    Returns up to `limit` near-miss unknowns."""
    if not question:
        return []
    # Only the asker's actual message region — injected blocks up top
    # quote the bot/other members and would add noise.
    tail = question.strip()[-600:]
    known_low = (known_names_text or "").lower()
    out: list[str] = []
    seen: set[str] = set()
    for m in _NAME_CAND_RE.finditer(tail):
        tok = m.group(1)
        low = tok.lower()
        if low in _NAME_CHECK_STOP or low in seen:
            continue
        seen.add(low)
        if low in known_low:
            continue
        try:
            if db.find_users_mentioned_in_text(tok):
                continue  # resolves to a profiled member — known
        except Exception:
            pass
        if not _near_known_name(low, known_low):
            continue  # near nobody — no confusion risk, no note
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def _name_check_note(unknowns: list[str]) -> str:
    if not unknowns:
        return ""
    names = ", ".join(f"'{n}'" for n in unknowns)
    return (
        f"\n\n[NAME CHECK — mechanical: the name(s) {names} closely "
        f"resemble a member name but are NOT an exact match for anyone "
        f"on file. Do NOT assume they mean the similar-sounding member — "
        f"resolve FIRST (lookup_user_profile by name, or "
        f"search_chat_messages); if that fails, say in voice you don't "
        f"know who that is. NEVER map the name onto the asker or another "
        f"member, and never present anyone's known traits as this "
        f"person's. If it's a company, brand, or public figure, ignore "
        f"this note.]"
    )


# Reply-to-bot factual dispute: the asker is contesting something the
# bot claimed. The 07-17 failure: "I haven't martingaled a play in
# probably 2 months" — the bot's search data was in hand, the shipped
# answer ignored the dispute and escalated with invented traits.
_DISPUTE_RE = re.compile(
    r"\b(?:i\s+(?:haven'?t|never|didn'?t|don'?t|wasn'?t|am\s+not|ain'?t)"
    r"|not\s+true|that'?s\s+(?:wrong|false|not\s+right|bullshit|cap)"
    r"|when\s+did\s+i|wasn'?t\s+me|what\s+are\s+you\s+talking\s+about"
    r"|i\s+stopped|quit\s+that|haven'?t\s+done\s+that)\b",
    re.IGNORECASE,
)


def _is_disputing_reply(question: str) -> bool:
    """Reply-to-bot where the asker's own message disputes a claim."""
    if not question or "[MESSAGE BEING REPLIED TO" not in question:
        return False
    if "from omniwiz" not in question:
        return False  # replying to someone else's message, not the bot's
    tail = question.strip()[-400:]
    return bool(_DISPUTE_RE.search(tail))


_DISPUTE_NOTE = (
    "\n\n[DISPUTE CHECK — mechanical: the asker is factually disputing "
    "something you said. Before answering, pull receipts on the disputed "
    "claim (search_chat_messages / lookup_trade_log). If receipts back "
    "you, cite them with dates or quotes. If they don't, or you find "
    "nothing, CONCEDE the point plainly in voice — do NOT repeat the "
    "disputed claim, do NOT embellish it, and do NOT pivot to new "
    "accusations to win the exchange. Conceding with style beats doubling "
    "down on a claim you can't back.]"
)


# Rewrite-fidelity check: a register/roast rewrite may change TONE only.
# Distinctive words in the rewritten answer that appear in neither the
# original answer, the subject's dossier, nor the question are invented
# material — above threshold, the rewrite is rejected and the original
# ships (mechanically cleaned). Fiction is worse than a weak register.
_NOVEL_STOP = frozenset({
    "their", "there", "would", "could", "should", "about", "every",
    "never", "always", "being", "doing", "going", "thing", "things",
    "really", "actually", "because", "while", "still", "since", "until",
    "again", "before", "after", "instead", "without", "within", "though",
    "through", "against", "between", "another", "someone", "anyone",
    "everyone", "nothing", "something", "anything", "everything",
    "yourself", "himself", "herself", "itself", "maybe", "probably",
    "clearly", "constantly", "second", "minute", "getting", "keeps",
    "keeping", "spend", "spending", "spent", "trying", "little", "enough",
    "better", "worse", "whole", "entire", "which", "where", "these",
    "those", "other", "right", "wrong", "least", "most",
})
_REWRITE_NOVEL_MAX_RATIO = 0.35


def _rewrite_novel_ratio(rewritten: str, source_text: str) -> float:
    """Fraction of the rewrite's distinctive words absent from its
    allowed sources. High ratio = the rewrite invented substance."""
    if not rewritten:
        return 0.0
    words = [
        w for w in re.findall(r"[a-z']{5,}", rewritten.lower())
        if w not in _NOVEL_STOP
    ]
    if not words:
        return 0.0
    src = (source_text or "").lower()
    novel = [w for w in words if w not in src]
    return len(novel) / len(words)


def _revoice_acceptable(revoiced: str, probe_answer: str,
                        question: str) -> bool:
    """Gate for the bare-probe revoice pass: accept the in-voice rewrite
    only when it invents nothing (novel ratio vs the probe's verified
    text + question stays under the fidelity cap) and carries no
    repetition glitch. Rejection ships the dry probe answer — correct-
    and-plain still beats in-voice-but-unfaithful."""
    if not revoiced or not revoiced.strip():
        return False
    if _has_repetition_glitch(revoiced):
        return False
    novel = _rewrite_novel_ratio(
        revoiced, f"{probe_answer or ''} {question or ''}"
    )
    return novel <= _REWRITE_NOVEL_MAX_RATIO


# Calendar-slate questions — "what econ data / earnings do we have
# tomorrow?" (2026-07-20 terlin). The answer to these is a list of
# tickers and release times, which carries NONE of the shapes the
# factual-specific nets look for ($ figures, %s, big numbers, dates),
# so a memory-confabulated slate ships silently. The question SHAPE is
# the reliable signal: asking what's on the near-term calendar is
# always a lookup, never a memory exercise — force the grounded retry
# whenever nothing sourced the answer (no Google grounding, no data
# tool). Both halves required: subject (earnings / econ data / the
# calendar) AND a near-term temporal, so "how did NVDA earnings go"
# (retrospective) and "what do we have tomorrow" (no subject) pass.
_CALENDAR_SUBJECT_RE = re.compile(
    r"\b(earnings?"
    r"|econ(?:omic)?\s+(?:data|calendar|releases?|events?|numbers?|prints?)"
    r"|data\s+releases?"
    r"|fed\s+speakers?)\b",
    re.IGNORECASE,
)
_CALENDAR_TEMPORAL_RE = re.compile(
    r"\b(today|tonight|tomorrow|tmrw?|this\s+(?:week|morning|afternoon)"
    r"|next\s+week|upcoming|on\s+deck|pre-?market"
    r"|after\s+(?:the\s+)?close)\b",
    re.IGNORECASE,
)


def _is_calendar_question(question: str) -> bool:
    """True for near-term econ-data / earnings calendar lookups."""
    q = question or ""
    return bool(
        _CALENDAR_SUBJECT_RE.search(q) and _CALENDAR_TEMPORAL_RE.search(q)
    )


def _ungrounded_web_specifics(
    answer: str, gm, was_web: bool, is_opinion: bool = False,
) -> bool:
    """A WEB-ROUTED answer that states factual specifics with NO
    grounding. The general confabulation signal (2026-07-06 CXW/GEO:
    invented bed counts, market cap, and contract dates escaped the
    market-fact-SHAPE backstop). Tied to the router's own WEB decision —
    if it said the question needs the open web and the model answered
    with specifics but never searched, that's the failure, whatever the
    fact shape. Grounded answers (or any data-tool answer) are exempt.

    `is_opinion`: skip entirely for rank/pick/your-take requests — the
    specifics are the bot's recommendations, not groundable claims
    (2026-07-13 kloh). The hard analyst-fact shape trigger
    (_is_ungrounded_market_fact) is NOT suppressed: a fabricated price
    target inside an opinion answer is still a claim worth grounding."""
    if not answer or len(answer) < 25 or not was_web or is_opinion:
        return False
    if _grounding_has_sources(gm):
        return False
    return bool(_FACTUAL_SPECIFIC_RE.search(answer))


# =====================================================================
# TA guard — structural suppression of self-generated technical
# analysis. The bot has NO indicator data source (no RSI/MACD feed, no
# chart engine), so any indicator read it emits is invented from priors
# (observed 2026-06-17: confabulated "GEO/CXW RSI", an "NDX 30,000
# pivot"). This is the same failure as the grounding backstop — a claim
# with no source — but TA claims often carry NO number, so the
# market-fact density test misses them. This catches two shapes:
#   (a) INDICATOR reads (RSI/MACD/stochastic/Bollinger/moving-average/
#       overbought/oversold/golden-cross) — the bot can NEVER source
#       these, so when nothing grounded the answer they are stripped.
#   (b) CHART-LEVEL claims (support/resistance/breakout/breakdown/
#       "holds $X"/"$X as support"/pivot/trendline) that are NOT tied to
#       a named source — these are regenerated, not stripped (lower
#       precision; a stray "support" in prose shouldn't mangle a
#       sentence). Both shapes are ignored when the answer is grounded
#       or a data tool fired — a web source legitimately CAN quote a
#       level or an indicator read, same trust rule as the market-fact
#       backstop.
# =====================================================================

# Indicator reads the bot has no feed for — always invented when
# unsourced. "MA" alone is too collision-prone (surname/state), so
# require EMA/SMA/DMA, a spelled-out "moving average", or an N-day form.
_TA_INDICATOR_RE = re.compile(
    r"\b(RSI|relative strength index"
    r"|MACD|stochastics?|Bollinger"
    r"|overbought|oversold"
    r"|golden cross|death cross"
    r"|\b\d{1,3}[- ]?(?:day|week|hour|period)\s+(?:moving average|EMA|SMA|MA)\b"
    r"|moving average|[ES]MA\b)",
    re.IGNORECASE,
)
# Chart-level / price-structure claims.
_TA_LEVEL_RE = re.compile(
    r"\b(support|resistance|breakout|break out|breakdown|break down"
    r"|consolidat\w+|trend\s?line|head and shoulders"
    r"|pivot|retest|double top|double bottom"
    r"|(?:holds?|holding|breaks?|breaking)\s+\$?\d)",
    re.IGNORECASE,
)
# A chart level is only a tradeable claim when it carries a PRICE. The
# bare level words above double as everyday English ("the Fed pivot",
# "a contrarian pivot", "demographic breakdown") and were firing the
# "I have no chart feed" hedge on roasts and macro takes (2026-06-24 QC,
# ~4 false positives). Requiring a co-located price/level number
# ($580, 4500, 30,000, 175.50) keeps the real catch (an "NDX 30,000
# pivot") and kills the prose false positives. Small numbers (ages,
# "7 times", "5min") are excluded — a level is $X or a 3+ digit number.
_TA_LEVEL_PRICE_RE = re.compile(r"\$\s?\d|\b\d{1,3}(?:,\d{3})+|\b\d{3,}\b")
# Attribution markers that legitimize a level claim (relayed, not
# self-generated). A bank/analyst/desk naming the level, a "per/says/
# sees/notes/flagged" verb, or a quoted source.
_TA_ATTRIB_RE = re.compile(
    r"\b(per |according to|says?|said|sees?|notes?|noted|flag(?:s|ged)?"
    r"|Goldman|GS\b|JPM|JPMorgan|Morgan Stanley|\bMS\b|BofA|Bank of America"
    r"|Citi|UBS|Barclays|Deutsche|RBC|Wells|analyst|desk|strategist"
    r"|chart shows|the chart)",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[str]:
    """Coarse sentence split on terminal punctuation + newlines. Good
    enough for clause-level strip/inspect; keeps bullet lines distinct."""
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p for p in (s.strip() for s in parts) if p]


def _ta_violations(answer: str) -> tuple[list[str], list[str]]:
    """Return (indicator_sentences, unattributed_level_sentences).

    Indicator sentences are always returned (the bot has no indicator
    source). Level sentences are returned only when NOT attributed to a
    named source in the same sentence."""
    indicators: list[str] = []
    levels: list[str] = []
    for s in _split_sentences(answer):
        if _TA_INDICATOR_RE.search(s):
            indicators.append(s)
            continue
        # A level claim must name a PRICE — a bare "pivot"/"breakdown" in
        # prose ("the Fed pivot", "demographic breakdown") is not a chart
        # level and must not trip the hedge.
        if (_TA_LEVEL_RE.search(s) and _TA_LEVEL_PRICE_RE.search(s)
                and not _TA_ATTRIB_RE.search(s)):
            levels.append(s)
    return indicators, levels


def _has_unsourced_ta(answer: str, grounding_metadata, tool_trace: list) -> bool:
    """True when an answer makes TA-shaped claims with NO source. Same
    trust rule as the market-fact backstop: grounding or a data tool
    firing means a source could legitimately carry the level/indicator,
    so don't fire."""
    if not answer or len(answer) < 20:
        return False
    if _grounding_has_sources(grounding_metadata):
        return False
    if tool_trace:
        return False
    indicators, levels = _ta_violations(answer)
    return bool(indicators or levels)


def _strip_sentences(answer: str, to_remove: list[str]) -> str:
    """Remove exact sentence strings from an answer and tidy whitespace.
    Used to excise invented indicator sentences (high precision); never
    used on level sentences (a strip there risks mangling prose)."""
    if not answer or not to_remove:
        return answer
    out = answer
    for s in to_remove:
        out = out.replace(s, "")
    # Collapse the gaps a removal leaves behind.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    return out.strip()


# =====================================================================
# Member-outcome guard — structural enforcement of "clapbacks can't
# have no truth behind them" (2026-07-02). The trade ledger records
# what members POSTED, not their live P&L; profiles carry qualitative
# color ("bag-holding yapper") that the model amplifies into asserted
# outcomes. Observed 2026-07-02: clapback told Cpig he's "underwater
# on your own bags" — his ledger shows NO documented outcomes at all
# (and GEO was ripping). Same failure family as the TA guard: a claim
# with no source. This detects second-person P&L-STATE assertions and,
# when the turn carried no trade data (no lookup_trade_log call) and
# the sentence isn't the member's own attributed claim, rewrites the
# jab onto documented behavior — or strips it.
# =====================================================================

# Second-person marker — the claim is about the person being addressed.
_OUTCOME_2P_RE = re.compile(r"\b(?:you|your|ur)\b", re.IGNORECASE)
# P&L-state assertion lexicon. This one guard IS a vocabulary by nature —
# "you're losing on X" is expressed through a bounded set of trader
# idioms, so unlike the faux-advice family (which has a shape) this is
# the right place for a curated list. Keep it P&L-specific so room-idiom
# banter that ISN'T an outcome claim doesn't trip it. (2026-07-07 added
# "in the hole" / "holding the bag" / "upside down" / "in the toilet"
# after "deep in the hole on this prison play" slipped through.)
_OUTCOME_CLAIM_RE = re.compile(
    r"(\bunderwater\b"
    r"|\bdown\s+bad\b"
    r"|\bbag.?hold(?:ing|er)?\b"
    r"|\b(?:left\s+)?holding\s+(?:the\s+)?bag\b"
    r"|\bbleed(?:ing|s)?(?:\s+out)?\b"
    r"|\bbl[eo]w(?:n|ing)?\s+up\b"
    r"|\bround.?tripp?(?:ed|ing)?\b"
    r"|\bin\s+the\s+red\b"
    r"|\bin\s+the\s+hole\b"
    r"|\bin\s+the\s+toilet\b"
    r"|\bupside\s+down\b"
    # Ruin idioms (2026-07-09: "speedrun homelessness" shipped about
    # Abe's plays). Person-exclusive states only — company-applicable
    # words (bankrupt/insolvent) stay OUT: "betting on them not going
    # insolvent" about a COMPANY is legitimate prose that shares a
    # sentence with 'you' often enough to false-positive.
    r"|\bhomeless(?:ness)?\b"
    r"|\bpoorhouse\b"
    r"|\bfood\s+stamps\b"
    r"|\b(?:down|up)\s+\d+(?:\.\d+)?\s*%)",
    re.IGNORECASE,
)
# Attribution / self-report markers — the member's OWN claim or a
# documented post is a legitimate source ("you said you're up 250%",
# "you posted the +65% close", "you never posted an exit").
_OUTCOME_ATTRIB_RE = re.compile(
    r"\b(?:said|says|claimed|claiming|posted|logged|screenshot(?:ted)?"
    r"|flagged|admitted|called\s+it|your\s+own\s+words|by\s+your\s+math"
    r"|per\s+your)\b",
    re.IGNORECASE,
)


# Ledger-caller names, cached 10 min — the outcome guard runs per
# answer and the DISTINCT query is cheap, but there's no reason to hit
# the DB on every message when the caller set changes ~never.
_MEMBER_NAMES_CACHE: tuple[float, list[str]] = (0.0, [])


def _known_member_names() -> list[str]:
    global _MEMBER_NAMES_CACHE
    now = time.time()
    ts, names = _MEMBER_NAMES_CACHE
    if now - ts < 600 and names:
        return names
    try:
        names = db.known_trade_caller_names()
    except Exception:
        names = names or []
    _MEMBER_NAMES_CACHE = (now, names)
    return names


def _member_name_re(member_names: set[str] | list[str] | None):
    """Compiled alternation matching any known ledger-caller name as a
    whole word ('Abe', 'abe's', 'BK'). Returns None when there are no
    names. Names come from db.known_trade_caller_names() — the members
    whose plays get discussed, which is exactly the set third-person
    outcome claims target."""
    names = [n for n in (member_names or []) if n and len(n) >= 2]
    if not names:
        return None
    alts = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"\b(?:{alts})\b", re.IGNORECASE)


def _outcome_violations(
    answer: str, context_text: str = "",
    member_names: set[str] | list[str] | None = None,
) -> list[str]:
    """Sentences asserting a member's P&L state with no visible source.
    A sentence is flagged when it has a PERSON ANCHOR — a second-person
    marker OR a known ledger-caller's name (2026-07-09: 'Abe's plays
    are a speedrun to homelessness' shipped in third person, invisible
    to the 2P-only check, while the ledger showed Abe green) — AND an
    outcome claim, UNLESS it carries an attribution marker or every
    percentage it cites appears verbatim in the turn's context (i.e. it
    came from an injected gain_pct, not thin air)."""
    if not answer:
        return []
    name_re = _member_name_re(member_names)
    flagged: list[str] = []
    for s in _split_sentences(answer):
        person_anchor = bool(_OUTCOME_2P_RE.search(s)) or bool(
            name_re and name_re.search(s)
        )
        if not (person_anchor and _OUTCOME_CLAIM_RE.search(s)):
            continue
        if _OUTCOME_ATTRIB_RE.search(s):
            continue
        pcts = re.findall(r"\d+(?:\.\d+)?%", s)
        if pcts and context_text and all(p in context_text for p in pcts):
            continue  # numbers sourced from injected context
        flagged.append(s)
    return flagged


def _has_unsourced_outcome_claims(
    answer: str, tool_trace: list, context_text: str = "",
    member_names: set[str] | list[str] | None = None,
) -> bool:
    """True when the answer asserts a member's P&L state and the turn
    consulted NO trade data. Unlike the TA guard, WEB grounding does NOT
    exempt — the open web can't source a member's book. Only a
    lookup_trade_log call this turn counts as consulting the ledger."""
    if not answer or len(answer) < 20:
        return False
    if any(
        (t.get("tool") or "") == "lookup_trade_log"
        for t in (tool_trace or [])
    ):
        return False
    return bool(_outcome_violations(answer, context_text, member_names))


# =====================================================================
# Rank-trajectory guard — lookup_user_profile returns ONLY the current
# rank snapshot (#N/M). There is NO historical rank data anywhere, so
# any claim that a rank CHANGED — "you lost your top-5 spot", "dropped
# from #3", "used to be #1", "climbed the board", "took two weeks off
# and slid" — is invented. 2026-07-05: the bot told SV he'd "lost your
# spot in the top 5" three hours after stating he was #9; there was no
# top-5 to lose. Same family as the time-series guard: a snapshot
# narrated as a trajectory. A movement claim gets stripped (the current
# rank + the rest of the jab survive).
# =====================================================================
_RANK_CONTEXT_RE = re.compile(
    # no outer \b — "#3" is non-word-led, a \b before it fails after a
    # space (2026-07-05 smoke: "dropped from #3" wasn't matching).
    r"(?:\brank(?:ed|ing)?\b|\bleaderboard\b|\bthe\s+(?:list|board)\b"
    r"|\btop\s+\d+|#\d+|\bspot\b|\bplace\b|\bstanding\b)",
    re.IGNORECASE,
)
_RANK_MOVE_RE = re.compile(
    r"\b(?:lost|lose|losing|dropp?ed|drop|fell|fall|slipp?ed|slid|"
    r"tumbled|climb(?:ed|ing)?|rose|rising|risen|jumped?|knocked|"
    r"bumped|replaced|dethroned|overtaken|used\s+to\s+be|"
    r"back\s+(?:up\s+)?to|fell\s+from|up\s+from|down\s+from|"
    r"moved?\s+(?:up|down))\b",
    re.IGNORECASE,
)


def _rank_trajectory_violations(answer: str) -> list[str]:
    """Sentences asserting a rank MOVEMENT / history (a rank-context word
    AND a movement verb in the same sentence). The bot only ever has the
    current snapshot, so these are always unsourced."""
    if not answer:
        return []
    return [
        s for s in _split_sentences(answer)
        if _RANK_CONTEXT_RE.search(s) and _RANK_MOVE_RE.search(s)
    ]


# /ask-only passive-aggressive register templates (2026-07-02). The
# deadpan/faux-advice ban is a pinned prompt rule the model still slips
# on; these feed the detect→rewrite pass (same path as meta-narration).
# Conservative: template shapes, not vibes — a direct roast doesn't
# match these.
_ASK_PASSIVE_AGGRESSIVE_RES = [
    # THE "maybe if you..." FAMILY — shape, not template (2026-07-07). The
    # bot's most repetitive faux-advice tic; each tail ("instead of X",
    # "half the energy", "less time X and more time Y", "put that energy
    # into") was slipping the narrow template regexes one by one. Detect
    # the INVARIANT instead: an advisory lead-in (maybe / if you) telling
    # the asker to REDIRECT their energy/time/effort/focus. Two surface
    # forms cover the family:
    #
    #   (a) redirect-the-object:  (maybe|if you) ... <redirect verb> ...
    #       <energy|time|effort|focus>
    #       — "maybe if you spent less time X", "maybe put that energy",
    #         "if you put half the effort into Y"
    re.compile(
        r"\b(?:maybe|if\s+you)\b[^.\n]{0,40}"
        r"\b(?:spent?|spend|put|putting|channel|direct|pour|invest|use|"
        r"focus)\b[^.\n]{0,25}\b(?:energy|time|effort|focus)\b",
        re.IGNORECASE,
    ),
    #   (b) redirect-via-instead-of: "maybe <verb> X instead of Y"
    #       (catches the attention-redirect variant that names no
    #        energy/time object — "maybe focus on your portfolio instead
    #        of my hydration"). [^.\n] keeps it inside one sentence.
    re.compile(
        r"\bmaybe\s+(?:focus|spend|put|worry|try|stick\s+to|use)\b"
        r"[^.\n]{0,120}\binstead\s+of\b",
        re.IGNORECASE,
    ),
    # Other sardonic-detachment templates (distinct shapes).
    re.compile(r"\bdo\s+with\s+that\s+what\s+you\s+will\b", re.IGNORECASE),
    re.compile(r"\bif\s+you\s+say\s+so\b", re.IGNORECASE),
    re.compile(r"\btrading\s+in\s+your\s+head\s+again\b", re.IGNORECASE),
]

# Mechanical em-dash + semicolon strip. Pulse-side lint replaces these
# via SCRUB; /ask doesn't have a SCRUB pass and they keep shipping.
# Cheap mechanical replacement preserves the surrounding sentence
# (comma reads naturally in nearly every position the em-dash sat).
#
# Also runs the full compose_lint_patterns regex set and returns
# the kinds we hit so the caller can log a structured record. Hits
# for non-punctuation lint kinds (meta-narration, AI-tell, hedging
# wrap-ups, source-prefix) are NOT auto-rewritten — rewriting natural
# prose mechanically breaks sentence flow. They're surfaced as log
# warnings so we can monitor frequency without shipping bad rewrites.
# Adjacent-duplication collapse. Each unit's FIRST token must start
# with a letter — this protects legitimate numeric sequences (strike
# lists "580 585 590", "94, 99") from being collapsed while still
# catching word/phrase doublings. Longer phrases collapse first so
# "cautious as cautious as" resolves as a 2-gram before word-level
# runs. IGNORECASE so "The the" collapses; the captured (first) form's
# casing is kept.
_DUP_3GRAM_RE = re.compile(r"\b([A-Za-z]\w*\s+\w+\s+\w+)(\s+\1\b)+", re.IGNORECASE)
_DUP_2GRAM_RE = re.compile(r"\b([A-Za-z]\w*\s+\w+)(\s+\1\b)+", re.IGNORECASE)
_DUP_WORD_RE = re.compile(r"\b([A-Za-z]\w*)(\s+\1\b)+", re.IGNORECASE)


def _collapse_adjacent_dupes(text: str) -> str:
    """Collapse immediate verbatim repeats of a word or 2-3 word phrase.

    "continue to continue" -> "continue to", "turned turned" ->
    "turned", "cautious as cautious as" -> "cautious as". Immediate
    verbatim repetition is glitch-characteristic in financial prose;
    legit cases ("very very") are rare and collapsing them is benign.
    Numeric sequences are protected (units must start with a letter),
    so strike lists and number ranges are untouched. Does NOT fix
    one-word-gap echoes ("X Y X") — collapsing those risks legit
    phrasing ("dollar for dollar"), left to the detector+retry.
    """
    if not text:
        return text
    out = _DUP_3GRAM_RE.sub(r"\1", text)
    out = _DUP_2GRAM_RE.sub(r"\1", out)
    out = _DUP_WORD_RE.sub(r"\1", out)
    return out


def _clean_voice_violations(text: str) -> tuple[str, list[str]]:
    """Return (cleaned_text, list_of_hit_kinds). Em-dashes / semicolons
    get replaced with commas; other lint hits are detected and named
    in the returned list but not auto-rewritten."""
    if not text:
        return text, []
    hit_kinds: list[str] = []

    # HTML entities leak from Gemini into Discord as literal text
    # (2026-07-15: "Q&nbsp;strategy" shipped in an ALP answer). Discord
    # renders none of them — decode to their characters up front.
    if "&" in text:
        text = html.unescape(text).replace(" ", " ")

    # Phase 1: detect all lint hits BEFORE mutating the text so the
    # kinds list reflects what was in the original answer.
    try:
        from ai_analysis.voice_rules import compose_lint_patterns
        scan = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        for pattern, kind in compose_lint_patterns():
            try:
                if re.search(pattern, scan, re.IGNORECASE):
                    hit_kinds.append(kind)
            except re.error:
                continue
        # /ask-ONLY register lint (not in the shared compose set — the
        # pulse pipeline doesn't have this failure mode): the
        # condescending faux-advice / sardonic templates the deadpan
        # rule bans. Detection feeds the same rewrite pass as
        # meta-narration (2026-07-02 QC: "maybe focus on your own
        # portfolio instead of my hydration" shipped through).
        for pa_pat in _ASK_PASSIVE_AGGRESSIVE_RES:
            if pa_pat.search(scan):
                hit_kinds.append("passive-aggressive")
    except Exception as e:
        log.debug(f"/ask voice-cleanup scan failed (non-fatal): {e}")

    # Phase 2: mechanical replacement for the safe-to-strip kinds.
    # Em-dash family — replace with comma + space. Handles all common
    # surface forms: " — ", "—", " —"  and the half-width "‒".
    # BUT leave a dash inside a numeric range alone (a digit on either
    # side = a range, not an aside): "62–65%", "$861–$881", "24–48h".
    # 2026-06-29 QC: "62–65%" was shipping mangled as "62, 65%".
    cleaned = re.sub(r'(?<!\d)\s*[—–‒]\s*(?!\$?\d)', ', ', text)
    # Semicolon inside a sentence — comma reads cleanly. Don't touch
    # semicolons inside fenced code (rare in /ask answers, defensive).
    cleaned = re.sub(r';\s+', ', ', cleaned)
    # Collapse any ", , " artifact from adjacent replacements.
    cleaned = re.sub(r',\s*,', ',', cleaned)
    # Adjacent-duplication collapse (2026-06-13 QC). The repetition
    # detector + retry catches token loops, but it FIRES-AND-FAILS on
    # verbatim adjacent doublings: when the retry re-glitches, the
    # original garbled text ships anyway. Observed 06-10: "turned
    # increasingly turned cautious as cautious as the recent price
    # action suggests". This is a deterministic cleanup that removes
    # the doubling regardless of whether the LLM retry cooperated.
    collapsed = _collapse_adjacent_dupes(cleaned)
    if collapsed != cleaned:
        hit_kinds.append("adjacent_dupe")
        cleaned = collapsed
    return cleaned, hit_kinds


# Slur-count question detection. The "how many times was X used" /
# "word count of X" question shape consistently trips Gemini's
# unconfigurable filter because:
#   1. Question text often contains the slur literally
#   2. Recent chat context block carries 3-5 slur tokens from the
#      room's normal register
#   3. Voice-strip retry (commit 137e310) handles profile-section
#      slurs but doesn't mask question/chat slurs
# So route directly via search_chat_messages, format response in
# Python — no Gemini prose generation, no filter trip risk.
# Concrete failure observed 2026-06-04 16:57-16:58 UTC: 4 blocks in
# 90 seconds, all asking about slur usage stats.
_KNOWN_SLURS = (
    "nigga", "nigger", "chink", "spic", "kike",
    "fag", "faggot", "pajeet",
)

_COUNT_INTENT_RE = re.compile(
    # 'tally' added 2026-06-10: SV asked "can i get a tally of the total
    # slangs used in here" — count-shaped, missed by the prior vocab.
    r"\b(?:how\s+many\s+times|word\s+count|frequency|how\s+often|"
    r"tally(?:\s+of)?|"
    r"count\s+(?:the\s+)?(?:word|slurs?|slangs?|uses?))\b",
    re.IGNORECASE,
)

_SLUR_REFERENCE_RE = re.compile(
    # Literal slurs (any variant) | meta keyword | euphemisms.
    # 'slang(s)' added 2026-06-10 — the room's euphemism for slurs
    # ("tally of the total slangs used in here" = slur-count ask).
    r"\b(?:nigg[ae]r?s?|chink|spic|kike|fag(?:got)?|pajeet|"
    r"slurs?|slangs?|"
    r"n[-\s]?word|"
    r"word\s+(?:use\s+)?with\s+an?\s+n)\b",
    re.IGNORECASE,
)


def _is_slur_count_question(question: str) -> bool:
    """Detect 'how many times was X used' shape where X references slurs.

    Strips reply-chain prefix so the scoring matches the asker's actual
    typed question, not the embedded prior message.
    """
    q = (question or "").strip()
    if not q:
        return False
    # If this is a reply chain, the asker's typed text comes AFTER a
    # marker like "[asker's message to you]" or "[username's message]".
    # Find the marker and score only what follows.
    for marker in ("'s message to you]", "'s message]"):
        idx = q.lower().rfind(marker)
        if idx != -1:
            q = q[idx + len(marker):].strip()
            break
    return bool(_COUNT_INTENT_RE.search(q)) and bool(_SLUR_REFERENCE_RE.search(q))


def _extract_slur_targets(question: str) -> list[str]:
    """Extract which slurs to count from the question. Returns:
      - the specific slur(s) named when explicitly referenced
      - all known slurs when only meta references ('slur', 'n-word',
        'word with an N') appear
    """
    q = (question or "").lower()
    explicit = []
    for slur in _KNOWN_SLURS:
        if re.search(rf"\b{re.escape(slur)}\b", q):
            explicit.append(slur)
    if explicit:
        return explicit
    return list(_KNOWN_SLURS)


async def _answer_slur_count_directly(
    question: str,
    user_id: int,
    asker_display_name: str = "",
    channel_name: str = "",
) -> discord.Embed:
    """Bypass Gemini for slur-count questions. Calls
    db.search_chat_messages_for_ask once per target slur and assembles
    a deterministic count response. Records the /ask query for quota
    tracking the same way the Gemini path does."""
    targets = _extract_slur_targets(question)
    days = 30

    counts: list[tuple[str, int]] = []
    for slur in targets:
        try:
            rows = db.search_chat_messages_for_ask(
                keyword=slur,
                days=days,
                channel_name=channel_name or None,
                limit=10000,  # large enough we don't truncate the count
            )
            counts.append((slur, len(rows)))
        except Exception as e:
            log.warning(f"slur-count search failed for {slur!r}: {e}")
            counts.append((slur, -1))

    valid = [(s, n) for s, n in counts if n >= 0]
    if not valid:
        desc = "→ Couldn't pull chat history right now — DB hiccup. Try again."
    elif len(valid) == 1:
        slur, n = valid[0]
        scope = f"in #{channel_name}" if channel_name else "across all chat"
        desc = (
            f"→ `{slur}` used **{n}** time{'s' if n != 1 else ''} "
            f"{scope} in the last {days} days."
        )
    else:
        total = sum(n for _, n in valid)
        scope = f"in #{channel_name}" if channel_name else "across all chat"
        lines = [
            f"→ Slur uses {scope} in the last {days} days — total **{total}**:",
        ]
        valid.sort(key=lambda kv: (-kv[1], kv[0]))
        for slur, n in valid:
            if n > 0:
                lines.append(f"  • `{slur}` — **{n}**")
        desc = "\n".join(lines)

    try:
        db.record_ask_query(user_id)
    except Exception as e:
        log.warning(f"slur-count record_ask_query non-fatal failure: {e}")

    # Ask-log completeness: short-circuit answers were previously
    # invisible to QC (only the Gemini path logged).
    try:
        db.append_ask_interaction(
            asker_display_name=asker_display_name,
            asker_username="",
            channel_name=channel_name,
            question=question,
            answer=desc,
            interaction_type="short_circuit_slur_count",
        )
    except Exception:
        pass

    return discord.Embed(description=desc, color=0x1ABC9C)


# Message-count question detection. The bot's 2026-06-04 22:29 UTC
# answer of "BK has sent 9 messages today" came from Gemini inferring
# the count from the visible recent-50-chat-window block — which is
# artificially small (50 messages span minutes-to-hours, not a day).
# Per user direction: "for message count look up either 100 or the
# last 6 hours, whichever has more messages" — pull both windows,
# take the larger as the sample pool, count target user's messages
# from that pool, return a deterministic answer with the window scope
# visible to the asker.
_MSG_COUNT_INTENT_RE = re.compile(
    r"\b(?:how\s+many|number\s+of|count\s+(?:of|the))\b"
    r"[\s\S]{0,40}"
    r"\b(?:messages?|posts?|texts?)\b",
    re.IGNORECASE,
)

# Target-name extraction patterns. Most natural shapes the asker uses:
#   "did NAME send/post"      → "did kyle send"
#   "from NAME"               → "messages from kyle"
#   "by NAME"                 → "messages by kyle"
#   "@NAME"                   → discord mention shape
#   "NAME's messages"         → possessive
_TARGET_EXTRACT_PATTERNS = [
    re.compile(r"\bdid\s+([A-Za-z][\w._-]*)\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+([A-Za-z][\w._-]*)\b", re.IGNORECASE),
    re.compile(r"\bby\s+([A-Za-z][\w._-]*)\b", re.IGNORECASE),
    re.compile(r"@(\w[\w._-]*)\b"),
    re.compile(r"\b([A-Za-z][\w._-]+)['’]s\s+messages?\b", re.IGNORECASE),
    re.compile(r"\bhas\s+([A-Za-z][\w._-]*)\s+(?:sent|posted)\b", re.IGNORECASE),
]

# Words that look like names but are actually function words / time
# markers. Filter out so the extractor doesn't return them as targets.
_TARGET_EXTRACTION_STOPWORDS = {
    "today", "yesterday", "this", "the", "did", "send", "post",
    "tomorrow", "anyone", "someone", "everyone",
}


def _is_message_count_question(question: str) -> bool:
    q = (question or "").strip()
    if not q:
        return False
    # Strip reply-chain prefix so we score the asker's typed text
    for marker in ("'s message to you]", "'s message]"):
        idx = q.lower().rfind(marker)
        if idx != -1:
            q = q[idx + len(marker):].strip()
            break
    return bool(_MSG_COUNT_INTENT_RE.search(q))


def _extract_message_count_target(question: str) -> str | None:
    """Pull the target user/name from the question. Returns the raw
    matched substring (e.g. 'kyle', 'BK', 'grandnagusyeezy'). The
    resolver below normalizes to a real user_id."""
    q = (question or "").strip()
    for marker in ("'s message to you]", "'s message]"):
        idx = q.lower().rfind(marker)
        if idx != -1:
            q = q[idx + len(marker):].strip()
            break
    for pat in _TARGET_EXTRACT_PATTERNS:
        m = pat.search(q)
        if m:
            name = m.group(1).strip()
            if name.lower() in _TARGET_EXTRACTION_STOPWORDS:
                continue
            if len(name) < 2:
                continue
            return name
    return None


def _resolve_chat_target(name: str) -> tuple[int | None, str | None, str | None]:
    """Resolve a name to (user_id, canonical_username, display_name).
    Tries three paths in order:
      1. Exact username via db.resolve_username_to_user_id
      2. Display-name OR username LIKE match in user_profiles
      3. Most-recent author_display / author_username LIKE match in
         chat_messages (catches users who chat but have no profile yet)
    Returns (None, None, None) when nothing matches.
    """
    if not name:
        return None, None, None
    needle = name.lower().strip().lstrip("@")
    if not needle:
        return None, None, None

    uid = db.resolve_username_to_user_id(needle)
    if uid:
        try:
            conn = db.get_connection()
            row = conn.execute(
                "SELECT user_id, username, display_name FROM user_profiles "
                "WHERE user_id = ? LIMIT 1",
                (uid,),
            ).fetchone()
            if row:
                return (
                    int(row["user_id"]),
                    row["username"],
                    row["display_name"],
                )
        except Exception:
            pass
        return uid, needle, None

    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT user_id, username, display_name FROM user_profiles "
            "WHERE LOWER(display_name) LIKE ? OR LOWER(username) LIKE ? "
            "LIMIT 1",
            (f"%{needle}%", f"%{needle}%"),
        ).fetchone()
        if row:
            return int(row["user_id"]), row["username"], row["display_name"]
    except Exception:
        pass

    try:
        conn = db.get_connection()
        row = conn.execute(
            "SELECT author_id, author_username, author_display "
            "FROM chat_messages "
            "WHERE LOWER(author_display) LIKE ? "
            "   OR LOWER(author_username) LIKE ? "
            "ORDER BY posted_at DESC LIMIT 1",
            (f"%{needle}%", f"%{needle}%"),
        ).fetchone()
        if row:
            return (
                int(row["author_id"]),
                row["author_username"],
                row["author_display"],
            )
    except Exception:
        pass

    return None, None, None


async def _answer_message_count_directly(
    question: str,
    user_id: int,
    asker_username: str = "",
    channel_name: str = "",
):
    """Bypass Gemini for message-count questions. Queries the LARGER
    of {100 most recent channel messages, last 6h of channel messages}
    and counts the target user's messages from that pool. Returns a
    Discord embed, or None when target resolution fails (caller falls
    back to Gemini in that case).
    """
    from datetime import datetime, timedelta, timezone

    target_raw = _extract_message_count_target(question)
    if not target_raw:
        return None

    target_uid, target_username, target_display = _resolve_chat_target(target_raw)
    if not target_uid:
        log.info(
            f"/ask: msg-count target {target_raw!r} unresolved — "
            f"falling back to Gemini"
        )
        return None

    now_utc = datetime.now(timezone.utc)

    # Pool A: most recent 100 messages in the channel. Use a 30-day
    # outer window since search_chat_messages_for_ask needs either a
    # keyword OR a window; limit=100 + ORDER BY posted_at DESC gives
    # us the 100 newest within that window regardless of time span.
    far_back = (now_utc - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        pool_100 = db.search_chat_messages_for_ask(
            start_iso=far_back, end_iso=now_iso,
            channel_name=channel_name or None,
            limit=100,
        )
    except Exception as e:
        log.warning(f"msg-count pool_100 query failed: {e}")
        pool_100 = []

    # Pool B: last 6 hours of messages in the channel
    six_h_ago = (now_utc - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        pool_6h = db.search_chat_messages_for_ask(
            start_iso=six_h_ago, end_iso=now_iso,
            channel_name=channel_name or None,
            limit=10000,
        )
    except Exception as e:
        log.warning(f"msg-count pool_6h query failed: {e}")
        pool_6h = []

    # Take the LARGER pool per the design choice ("whichever has more")
    if len(pool_6h) >= len(pool_100):
        pool, pool_label = pool_6h, "last 6 hours"
    else:
        pool, pool_label = pool_100, "last 100 channel messages"

    # Filter to the target user. Use author_username (canonical) and
    # also accept rows where author_username matches the resolved name.
    target_username_low = (target_username or "").lower()
    target_msgs = [
        m for m in pool
        if (m.get("author_username") or "").lower() == target_username_low
    ]
    target_count = len(target_msgs)

    # Compute window span for honesty in the response
    span_label = ""
    if pool:
        try:
            timestamps = [m.get("posted_at", "") for m in pool if m.get("posted_at")]
            if timestamps:
                from datetime import datetime as _dt
                earliest = min(timestamps)
                latest = max(timestamps)
                e_dt = _dt.fromisoformat(earliest.replace(" ", "T")[:19])
                l_dt = _dt.fromisoformat(latest.replace(" ", "T")[:19])
                span_h = (l_dt - e_dt).total_seconds() / 3600
                if span_h < 1:
                    span_label = f"~{int(span_h * 60)}min span"
                else:
                    span_label = f"~{span_h:.1f}h span"
        except Exception:
            pass

    display = target_display or target_username or target_raw
    scope = f"in #{channel_name}" if channel_name else "in this channel"
    pool_size = len(pool)

    if target_count == 0:
        desc = (
            f"→ **{display}** has no logged messages {scope} in the "
            f"{pool_label} ({pool_size} total messages, {span_label})."
        )
    else:
        desc = (
            f"→ **{display}** sent **{target_count}** message"
            f"{'s' if target_count != 1 else ''} {scope} in the "
            f"{pool_label} ({pool_size} total, {span_label})."
        )

    try:
        db.record_ask_query(user_id)
    except Exception as e:
        log.warning(f"msg-count record_ask_query non-fatal: {e}")

    # Ask-log completeness: short-circuit answers were previously
    # invisible to QC (only the Gemini path logged).
    try:
        db.append_ask_interaction(
            asker_display_name="",
            asker_username=asker_username,
            channel_name=channel_name,
            question=question,
            answer=desc,
            interaction_type="short_circuit_message_count",
        )
    except Exception:
        pass

    return discord.Embed(description=desc, color=0x1ABC9C)


# =====================================================================
# Intent router — structural grounding (2026-06-19)
# =====================================================================
# Replaces the answer-keyword "trip" with an up-front decision about
# whether the QUESTION needs the open web. Gemini's search grounding is
# discretionary, so the only structural way to GUARANTEE a fact question
# gets grounded is to route it into a search-only pass (function tools
# stripped → search is the model's only move). Banter / self-data
# questions take the normal multi-tool path. The decision is the model's
# semantic read of the question's intent, NOT a regex over the output —
# so it doesn't misfire on arbitrary words like "June 19" or "unlock".
_ASK_ROUTER_INSTRUCTION = (
    "You are the routing classifier for a Discord trading-room bot. "
    "Decide whether answering the user's question REQUIRES looking up a "
    "current real-world fact on the open web — something the bot cannot "
    "get from its own data and would otherwise guess from memory.\n\n"
    "Answer WEB if the correct answer depends on an external fact such "
    "as: a stock's IPO / lockup / unlock / float schedule, a company "
    "filing or corporate-action detail, market holidays / trading hours, "
    "an economic-data or earnings DATE, a macro or geopolitical event, "
    "breaking news, or the definition of some current real-world state. "
    "ALSO answer WEB for a SCHEDULE or START TIME of any real-world event "
    "('what time do the World Cup games start today', 'when does the game "
    "kick off', 'is the market open Friday') and for any request for "
    "specific HISTORICAL or SEASONAL statistics — win-rates, average "
    "returns, 'how does the market do around July 4 / in September', any "
    "figure a reader would expect to be sourced, not recalled. "
    "ALSO answer WEB when the question is an OPINION or recommendation "
    "that has a factual edge a lookup would sharpen — a pricing or "
    "product comparison ('best whiskey under $50', 'better laptop for "
    "trading'), a who-won / when-happened / result ('did Verstappen win "
    "Monaco', 'is creatine still the move'), or the CURRENT discourse on "
    "something ('what's the room/market saying about IBIT'). The verdict "
    "is: would a real lookup change or sharpen the answer? If yes, WEB.\n\n"
    "Answer LOCAL if the question is: banter, a roast, or an opinion "
    "about room members; a pure vibe check ('what's up', 'tell a joke', "
    "'you good') that no lookup would change; OR answerable from the "
    "bot's own data — a member's trades or track record, a LIVE stock "
    "price or options chain, recent room chat history.\n\n"
    "The question may be preceded by chat-context lines; classify the "
    "ACTUAL question (usually the last line, after the final separator). "
    "When genuinely unsure, answer WEB — a wasted search costs seconds, "
    "but an unverified wrong fact gets traded on. Reserve LOCAL for what "
    "is CLEARLY banter, roast, member-data, or live-price territory.\n\n"
    "SECOND word — the question's REGISTER:\n"
    "FACT if the asker genuinely wants information — a real question "
    "with a right answer ('is warsh speaking today', 'when does ASML "
    "report', 'what year did toy story 3 come out', 'why is the market "
    "down'). The asker is not performing; mockery would be answering a "
    "sincere question with a slap. FACT also covers feedback about the "
    "bot's previous answer — a correction, a complaint about the "
    "answer's sources, accuracy, format, or tone ('never use X as a "
    "source again', 'that price was wrong'), or a message agreeing "
    "with or extending such feedback. The user wants acknowledgment "
    "or a fix, not a comeback.\n"
    "BANTER if the ask is itself a performance — a roast request, a "
    "callout, a flex, a bait ('roast terlin', 'who's the biggest bag "
    "holder', 'tell BK why he's poor'), or trash-talk dressed as a "
    "question. When unsure, answer FACT — a straight answer to banter "
    "is a minor miss; mocking a sincere question is the failure mode.\n\n"
    "Output EXACTLY two words: 'WEB FACT', 'WEB BANTER', 'LOCAL FACT', "
    "or 'LOCAL BANTER'."
)


async def _classify_ask_needs_web(
    client, ask_model: str, safety_settings, question: str
) -> tuple[bool, bool]:
    """Classify the question in ONE cheap call, two signals:

    Returns (needs_web, is_factual).
      needs_web  — True: route to the search-only grounded pass.
      is_factual — True: a sincere informational question (FACT), which
                   gates the straight-answer directive + the
                   asker-mockery guard. False: banter/roast/callout, or
                   classification unavailable — full register stays on.

    Fail-safes: any error / blank question returns (False, False) — the
    normal multi-tool path with today's register behavior, never a hard
    failure. A one-word legacy verdict ('WEB') defaults the register to
    FACT: a straight answer to banter is a minor miss, mocking a sincere
    question is the failure mode."""
    if not question or not question.strip():
        return (False, False)
    try:
        from google.genai import types
        # Only the tail matters — the real ask sits after the context
        # blocks. Cap to keep the classify call cheap and minimize slur
        # exposure from any quoted chat above it.
        tail = question.strip()[-1800:]
        resp = await client.aio.models.generate_content(
            model=ask_model,
            contents=[types.Content(
                role="user",
                parts=[types.Part.from_text(text=tail)],
            )],
            config=types.GenerateContentConfig(
                system_instruction=_ASK_ROUTER_INSTRUCTION,
                safety_settings=safety_settings,
                max_output_tokens=8,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
        # Classifier spend is ~8 output tokens — negligible, not tallied
        # (the per-call budget reservation in _answer_with_gemini already
        # over-reserves to cover it).
        verdict = ((resp.text or "").strip().upper())
        needs_web = verdict.startswith("WEB")
        is_factual = "BANTER" not in verdict
        return (needs_web, is_factual)
    except Exception as e:
        log.info(f"/ask: intent-router classify failed (defaulting LOCAL): {e}")
        return (False, False)


# (2026-07-16: _EARNINGS_DATE_RE removed with the route unification —
# lookup_earnings_date is now reachable on every ask, so no override is
# needed to steer earnings-date questions toward it.)


# Quote / lyric completion shapes — "finish the lyrics", "what's the
# next line", "complete the quote". Completing a verbatim text is a
# LOOKUP: 2026-07-12 the bot invented a fake bar rather than complete a
# lyric whose real words it wouldn't say. These force the WEB route so
# the answer is grounded, and the prompt's no-fake-lyrics rule handles
# the can't-say-it case honestly.
_QUOTE_COMPLETION_RE = re.compile(
    r"(?:finish|complete)\s+(?:th(?:e|is|at)\s+)?(?:song\s+)?"
    r"(?:lyrics?|quote|line|verse|bar)"
    r"|next\s+(?:line|bar|verse|lyric)\b"
    r"|how\s+does\s+(?:it|the\s+song|that\s+song)\s+go",
    re.IGNORECASE,
)


# Asker-mockery shapes — second-person derision aimed at the ASKER of a
# FACT-classified question: inventing a premise they never stated
# ("you're confusing X with Y"), or scolding them for asking ("stop
# looking for a speech that isn't happening"). Only enforced when the
# router said FACT — on banter these same shapes are legitimate register.
# The 2026-07-08 exemplar: "is warsh speaking today" (a sincere schedule
# question) answered with both shapes above.
_ASKER_MOCKERY_RES = [
    # invented premise: telling the asker what they think/confuse.
    # Apostrophe class covers ASCII ' and curly ’ — Discord answers ship
    # the curly one.
    re.compile(
        r"\byou[’']?re\s+(?:confusing|conflating|mixing\s+up)\b",
        re.IGNORECASE),
    re.compile(
        r"\byou\s+(?:clearly|obviously)\s+(?:think|believe|want|have)\b",
        re.IGNORECASE),
    # scolding the asker for the act of asking / hoping
    re.compile(
        r"\b(?:stop|quit)\s+(?:looking|asking|searching|waiting|hoping|"
        r"hunting)\b", re.IGNORECASE),
    re.compile(r"\bjust\s+because\s+you\s+(?:want|wish|hope)\b", re.IGNORECASE),
    re.compile(r"\b(?:that[’']?s\s+(?:just\s+)?you\s+coping|cope\s+harder)\b",
               re.IGNORECASE),
]


# Roast-recycle detection — 2026-07-10: three roasts of ZHawk inside two
# minutes remixed the same four profile hooks (GEO / no-exit / LARP /
# casino), earning "omniwiz doesn't know you or how to insult you at
# all. pathetic." The prompt-level anti-recycling block is advisory and
# the model ignored it; this is the code-level check. A roast is
# "recycled" when it shares >= _RECYCLE_HOOK_MIN distinctive hooks
# (cashtags + crude-stemmed content words) with a prior answer to the
# SAME asker. BANTER-gated by the caller — factual answers legitimately
# repeat facts.
_HOOK_STOPWORDS = {
    "about", "actual", "actually", "after", "again", "against", "answer",
    "because", "been", "before", "being", "better", "call", "could",
    "even", "every", "first", "from", "getting", "have", "here", "just",
    "keep", "know", "like", "little", "look", "make", "more", "most",
    "much", "need", "never", "only", "other", "over", "people", "post",
    "real", "really", "right", "room", "same", "should", "since", "some",
    "spend", "start", "still", "than", "that", "their", "them", "then",
    "there", "these", "they", "thing", "think", "this", "those",
    "through", "time", "trade", "trader", "trades", "trading", "want",
    "week", "were", "what", "when", "where", "which", "while", "whole",
    "with", "without", "would", "your",
}


# All-caps tokens that are NOT tickers (roasts write tickers bare —
# "GEO entries", no cashtag — so caps tokens count as hooks, minus the
# acronyms trading prose uses constantly).
_HOOK_CAPS_STOP = {
    "AI", "US", "UK", "EU", "USA", "CEO", "CFO", "ETF", "IPO", "GDP",
    "CPI", "PPI", "FED", "LOL", "IMO", "NFA", "PSA", "THE", "AND",
    "NOT", "YOU", "OK",
}


def _extract_roast_hooks(text: str) -> set[str]:
    """Distinctive hooks in a roast: cashtags, bare ALL-CAPS tickers
    ('GEO entries' carries no $), and content words >=4 chars, crude-
    stemmed so LARPing/LARP and exits/exit collide. Contractions are
    cut at the apostrophe ('you’re' -> 'you' -> dropped as short)."""
    if not text:
        return set()
    hooks = {t.upper() for t in re.findall(r"\$([A-Za-z]{1,6})\b", text)}
    for t in re.findall(r"\b[A-Z]{2,6}\b", text):
        if t not in _HOOK_CAPS_STOP:
            hooks.add(t)
    for w in re.findall(r"[a-zA-Z][a-zA-Z'’-]{3,}", text.lower()):
        w = w.split("'")[0].split("’")[0].strip("-")
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                w = w[: -len(suf)]
                break
        if len(w) < 4 or w in _HOOK_STOPWORDS:
            continue
        hooks.add(w)
    return hooks


_RECYCLE_HOOK_MIN = 4

# P&L-monotone detection — 2026-07-10 user feedback: "your roasts need
# to target more personal stuff than trading money losses cuz it's just
# lame and repetitive." The prompt now states the hierarchy (personal
# color beats P&L); this is the code floor: a BANTER roast built purely
# from trading-loss vocabulary with ZERO hooks from the dossier's
# personal-color sections gets one rewrite. Stemmed to match
# _extract_roast_hooks output.
_PNL_HOOKS = {
    "bag", "baghold", "exit", "entri", "entry", "position", "portfolio",
    "account", "trade", "trad", "loss", "losse", "loser", "call", "put",
    "option", "casino", "money", "receipt", "ledger", "scalp", "chart",
    "leverage", "liquidat", "underwater", "paperhand", "port", "expiry",
    "strike", "stop-loss", "green", "gain", "profit", "pump", "dump",
}


def _is_pnl_hook(h: str) -> bool:
    """P&L-vocabulary test for a hook: direct hit, singular form (short
    words like 'bags' survive the crude stemmer un-stripped), or a bare
    ALL-CAPS ticker."""
    low = h.lower()
    if low in _PNL_HOOKS:
        return True
    if low.endswith("s") and low[:-1] in _PNL_HOOKS:
        return True
    return h.isupper() and 2 <= len(h) <= 6

# Dossier sections that carry personal color (vs trading records).
_PERSONAL_SECTION_RE = re.compile(
    r"\*\*(?:Recent personal life|Retarded takes|Personality and style)"
    r"\.\*\*\s*\n(.*?)(?=\n\s*\*\*|\n- \*\*|\Z)",
    re.DOTALL,
)


def _personal_color_hooks(profiles_block: str) -> set[str]:
    """Hooks from the dossier's personal-color sections — the pool a
    good roast draws from. P&L vocabulary is excluded even here:
    'allergic to receipts' in a Personality section must not let a
    receipts jab count as personal color."""
    pool: set[str] = set()
    for m in _PERSONAL_SECTION_RE.finditer(profiles_block or ""):
        pool |= _extract_roast_hooks(m.group(1))
    return {h for h in pool if not _is_pnl_hook(h)}


def _roast_is_pnl_monotone(answer: str, profiles_block: str) -> bool:
    """True when a roast is all trading-loss vocabulary and touches none
    of the dossier's personal color. Callers gate on BANTER — factual
    answers talk P&L legitimately."""
    if not answer:
        return False
    hooks = _extract_roast_hooks(answer)
    if not hooks:
        return False
    pnl_n = len({h for h in hooks if _is_pnl_hook(h)})
    if pnl_n < 3:
        return False
    pool = _personal_color_hooks(profiles_block)
    return not (hooks & pool)


def _recycled_roast_hooks(answer: str, prior_answers: list[str]) -> list[str]:
    """Hooks the new answer shares with ANY single prior answer to the
    same asker. Compared per-prior-answer (not against the union) so the
    threshold means 'this reads like a remix of THAT roast'."""
    if not answer or not prior_answers:
        return []
    cur = _extract_roast_hooks(answer)
    if not cur:
        return []
    best: set[str] = set()
    for pa in prior_answers:
        shared = cur & _extract_roast_hooks(pa)
        if len(shared) > len(best):
            best = shared
    return sorted(best)


# Phantom image-read shapes — the answer claims to have SEEN/received a
# screenshot when NO image reached the Gemini call. 2026-07-10: the bot
# told 2pale "So you actually have a screenshot. 6.1x isn't 7x ... you
# finally stopped larping and posted a fill" — with zero image bytes in
# the request. It invented a reading of a receipt and validated an
# undocumented 7x claim on the strength of its own fabrication. Callers
# gate on `not images` — with an image actually attached, reading it is
# the whole point.
_PHANTOM_IMAGE_READ_RES = [
    # acceptance: "you actually have/posted a screenshot / fill / receipt"
    # — gap allows up to 5 filler words ("you finally stopped larping
    # and posted a fill", the shipped 07-10 sentence, carries 4).
    re.compile(
        r"\byou\s+(?:\w+\s+){0,5}?(?:actually\s+have|posted|sent|dropped|"
        r"uploaded)\s+(?:a|the|that|your)\s+(?:screen\s*shot|screenshot|"
        r"image|pic|chart|receipt|fill)\b",
        re.IGNORECASE),
    # reading: "the/your screenshot shows/says/reads ..."
    re.compile(
        r"\b(?:the|your|that)\s+(?:screen\s*shot|screenshot|image|pic|"
        r"chart|receipt)\s+(?:shows|says|reads|confirms|proves)\b",
        re.IGNORECASE),
    # perception: "I (can) see the/your screenshot ..."
    re.compile(
        r"\bI\s+(?:can\s+)?see\s+(?:the|your|that)\s+(?:screen\s*shot|"
        r"screenshot|image|pic|chart|receipt)\b",
        re.IGNORECASE),
]
# Negation / demand forms are legitimate ("post the receipt", "you never
# posted an exit") — skip sentences carrying them.
_PHANTOM_NEGATION_RE = re.compile(
    r"\b(?:never|no|not|didn[’']?t|don[’']?t|without|post\s+(?:the|a|it))\b",
    re.IGNORECASE,
)


def _phantom_image_read_violations(answer: str) -> list[str]:
    """Sentences claiming the bot read/received an image. Callers gate on
    the call having carried NO image bytes — then every such claim is
    invented. Sentence-level so the strip keeps the rest."""
    if not answer:
        return []
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", answer):
        if _PHANTOM_NEGATION_RE.search(sent):
            continue
        if any(rx.search(sent) for rx in _PHANTOM_IMAGE_READ_RES):
            out.append(sent)
    return out


def _asker_mockery_violations(answer: str) -> list[str]:
    """Sentences in `answer` that mock the asker of a sincere question.
    Sentence-level so the strip fallback keeps the factual core. Callers
    gate on the router's FACT verdict — never run this on banter."""
    if not answer:
        return []
    out: list[str] = []
    for sent in re.split(r"(?<=[.!?])\s+", answer):
        if any(rx.search(sent) for rx in _ASKER_MOCKERY_RES):
            out.append(sent)
    return out


# FACT-answer jab detection (2026-07-27 planets question: a clean
# factual answer closed with a roast arrow about the asker's MU calls;
# the asker's verbatim feedback — "I didn't ask for the sarcasm at the
# end". The FACT directive and Type 1 profile rules both ban jab
# padding; this is the code-level enforcement, same pattern as the
# asker-mockery guard. A jab = second-person address + roast
# vocabulary in the same sentence; the trade-advice register ("you're
# betting on whether OCI can scale") carries no roast marker and never
# matches. FACT-gated by the caller — on banter these shapes are the
# product.)
_JAB_SECOND_PERSON_RE = re.compile(
    r"\byou(?:r|[’']re|[’']ll|[’']d|[’']ve)?\b", re.IGNORECASE,
)
_JAB_ROAST_MARKER_RE = re.compile(
    r"\b(?:spamm\w*|cop(?:e|ing)\b|crying|bag[- ]?hold\w*|bags\b"
    r"|lotto\w*|worthless|degenerate\w*|martingal\w*|liquidat\w*"
    r"|blow(?:n|ing)?[- ]?up|round[- ]?tripp?\w*"
    r"|chas(?:e|ing)\s+(?:green|tops|entries|pumps)"
    r"|your\s+(?:account|portfolio|p&l|book\s+is)"
    r"|expire[sd]?\s+worthless)",
    re.IGNORECASE,
)


def _fact_jab_sentences(answer: str) -> list[str]:
    """Sentences of a FACT-routed answer that jab the asker — roast
    material tacked onto a sincere factual reply. Strip input."""
    return [
        s for s in _split_sentences(answer or "")
        if _JAB_SECOND_PERSON_RE.search(s)
        and _JAB_ROAST_MARKER_RE.search(s)
    ]


# Clapback fidelity (2026-07-29 kyle/ZHawk blend). In a multi-party
# thread both dossiers ride in WHO'S TALKING, and under sustained-roast
# pressure the model attributed ZHawk's receipts (XSP trade, Austin,
# Excel) to bearishkyle and invented replacements when corrected
# ("fine, dad's fund"). Receipts at answer time: distinctive claims in
# an ungrounded BANTER answer must appear in the ASKER'S OWN material.

def _member_handles_in_profiles(profiles_block: str) -> list[tuple[str, str]]:
    """(display, username) for every member loaded into WHO'S TALKING."""
    out: list[tuple[str, str]] = []
    for m in re.finditer(r'^- \*\*(.+?)\*\*\s*\(([^,)]+)', profiles_block or "",
                         re.M):
        disp = (m.group(1) or "").strip()
        uname = (m.group(2) or "").strip().lstrip("@")
        if uname:
            out.append((disp, uname))
    return out


def _roast_subjects(
    question: str, profiles_block: str,
    asker_username: str, asker_display: str,
) -> list[tuple[str, str]]:
    """Every member a question is about, ordered by first mention.

    2026-08-12, second pass: the single-subject version walked the
    PROFILES block and returned the first member whose handle appeared
    anywhere in the question. With two people tagged that resolved by
    profiles-block order, which has nothing to do with the question —
    "is @Tulch worse than @Monsoon" and "is @Monsoon worse than @Tulch"
    both returned Monsoon, because Monsoon happened to be listed first.

    Ordering by position in the question makes the primary subject the
    one actually named first, and returning all of them lets a
    comparison question draw on both dossiers legitimately.
    """
    q = question or ""
    lines = [ln for ln in q.splitlines()
             if ln.strip() and not ln.strip().startswith(("[", "2026-"))]
    ask_line = lines[-1] if lines else q
    a_uname = (asker_username or "").strip().lower()
    a_disp = (asker_display or "").strip().lower()
    found: list[tuple[int, str, str]] = []
    for disp, uname in _member_handles_in_profiles(profiles_block):
        if uname.lower() in (a_uname, a_disp) or disp.lower() in (a_uname,
                                                                  a_disp):
            continue  # the asker is not their own subject
        best: int | None = None
        for handle in (uname, disp):
            if len(handle) < 3:
                continue
            m = re.search(rf"@?\b{re.escape(handle)}\b", ask_line, re.I)
            if m and (best is None or m.start() < best):
                best = m.start()
        if best is not None:
            found.append((best, disp, uname))
    found.sort(key=lambda t: t[0])
    return [(d, u) for _pos, d, u in found]


def _roast_subject(
    question: str, profiles_block: str,
    asker_username: str, asker_display: str,
) -> tuple[str, str] | None:
    """The member a question is ABOUT, when that is not the asker.

    2026-08-12: SV asked "is @Tulch still the donkey of the room?" and the
    fidelity guard below scoped its receipts pool to SV. Every correct
    claim about Tulch (his SanDisk-only alert ledger, his 86.93% SPXW
    close, his own catchphrase) is absent from SV's material, so a guard
    that fired would have flagged all of them and rewritten a correct
    roast into a vague one. `ask_prompt.py` already tells the model that a
    question about another member takes its substance from the SUBJECT'S
    profile; the guard disagreed with the prompt about whose material
    counts. This resolves that disagreement in the prompt's favour.

    Returns None when the question names nobody but the asker, which
    keeps the original asker-scoped behaviour for self-directed banter.
    With several members tagged this is the FIRST one named — see
    _roast_subjects for the full list.

    Only the real question line is read. The injected VERBATIM and
    REPLIED-TO blocks quote other members wholesale and would otherwise
    make every reply look like a third-party question.
    """
    subs = _roast_subjects(question, profiles_block, asker_username,
                           asker_display)
    return subs[0] if subs else None


# Third-person reference to the subject. The naming problem exists ONLY
# here: a reply that addresses someone as "you" is unambiguous by
# construction, because the referent is whoever is being replied to.
_THIRD_PERSON_REF_RE = re.compile(
    r"\b(he|him|his|she|her|hers|they|them|their|"
    r"(?:the|that|this|a) (?:guy|dude|man|kid))\b",
    re.IGNORECASE,
)


def _member_material_surface(
    profiles_block: str, chat_context: str,
    username: str, display: str, question: str,
) -> str:
    """Receipts pool for ONE member: THEIR WHO'S TALKING section (not the
    co-loaded members'), THEIR chat lines, and the question.

    Used for the asker on self-directed banter and for the subject when
    the question is about somebody else.
    """
    parts = [question or ""]
    uname = (username or "").strip().lower()
    if uname:
        for sec in re.split(r"(?m)^(?=- \*\*)", profiles_block or ""):
            head = sec.split("\n", 1)[0].lower()
            if f"({uname}" in head:
                parts.append(sec)
                break
    disp = (display or "").strip().lower()
    for ln in (chat_context or "").splitlines():
        label = ln.split(": ", 1)[0].lower()
        if uname and f"({uname})" in label:
            parts.append(ln)
        elif disp and label == disp:
            parts.append(ln)
    return "\n".join(parts)


def _asker_material_surface(
    profiles_block: str, chat_context: str,
    asker_username: str, asker_display: str, question: str,
) -> str:
    """Back-compat wrapper — the asker-scoped pool."""
    return _member_material_surface(
        profiles_block, chat_context, asker_username, asker_display, question,
    )


def _clapback_claim_tokens(answer: str) -> list[str]:
    """Distinctive claim tokens in a clapback: cashtags, bare
    uppercase tickers (2-5 caps), and mid-sentence capitalized
    entities. Room voice is lowercase-heavy, so mid-sentence caps are
    deliberate entity mentions; sentence-initial caps are skipped to
    avoid flagging ordinary sentence case."""
    toks: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\$([A-Za-z]{1,6})\b|\b([A-Z]{2,5})\b",
                         answer or ""):
        t = m.group(1) or m.group(2)
        tl = t.lower()
        if (not t.isalpha() or t.upper() in _TICKER_FALSE_POSITIVES
                or tl in seen):
            continue
        seen.add(tl)
        toks.append(t)
    for m in re.finditer(r"\b([A-Z][a-z]{2,15})\b", answer or ""):
        t = m.group(1)
        tl = t.lower()
        if tl in seen or tl in _NAME_CHECK_STOP:
            continue
        prev = (answer or "")[:m.start()].rstrip()
        if not prev or prev[-1] in '.!?"“’':
            continue  # sentence-initial / quote-opening — plain case
        seen.add(tl)
        toks.append(t)
    return toks


# Words a roast can use freely: they carry no personal claim. Kept
# deliberately broad — this list only has to cover ordinary prose, and
# a miss costs one flagged word that the rewrite pass can restore.
_GENERIC_ROAST_WORDS = frozenset("""
about after again against already always another anyone around because
before being below better between board bought bring building built
called cannot chart charts check close closed comes coming could
country couple course covered decided decide deep does doing done
down during early enough entire entry entries even ever every exactly
exit exits expect expecting first found from getting gonna guess
happen happened having here hold holding hours https https into
its just keep keeps kind know known last later least leave less
level levels like liked little long longer look looking looks lose
loses losing loss losses lost made make makes making many market
markets maybe mean means might money month months more morning most
move moved moves much need needs never next nothing number numbers
often only open opened other others over own paid people perfect
place play played playing point points position positions post
posted price prices probably put puts quite rate rates read ready
real really reason right same season second seems sell selling send
sent series session share shares short should show shows side simply
since single small some someone something soon spend spent still
stock stocks stop street strike sure take taken takes taking talk
talking tape than that their them then there these they thing things
think this those though three through time times today together
tomorrow took trade traded trades trading tried true trying turn
under until using very wait waiting want wanted watch watching week
weeks well went were what when where which while whole will with
without work working would year years your yours
smack smacking carry carrying carried household night nights writing
write wrote spend spending build built building account accounts
straight lately barely hardly simply merely mostly rather pretty
whatever anything everything nobody somebody everyone entire whole
""".split())


def _invented_personal_details(answer: str, *contexts: str) -> list[str]:
    """Distinctive content words in a personal roast that appear NOWHERE
    in the model's own input.

    WHY (2026-08-25): _clapback_claim_tokens only sees cashtags, caps
    tickers and mid-sentence Capitalized words. Personal-life details
    are lowercase — pontoon, koozie, apartment, elevator — so a roast
    could invent one and the fidelity guard found literally zero tokens
    to check. A real answer to a member ("your next basecat gamble ...
    on the pontoon") shipped with "pontoon" appearing in no profile,
    no message, and no part of the prompt.

    The rule is provenance, not vocabulary: if a distinctive word is not
    in the subject's material, the question, or the chat the model was
    handed, the model made it up. Generic prose is stoplisted; short
    words are ignored; anything the input contains passes.
    """
    hay = " ".join(c or "" for c in contexts).lower()
    hay = re.sub(r"[^a-z0-9 ]+", " ", hay)
    hay_words = set(hay.split())
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\b([a-z][a-z'-]{4,15})\b", (answer or "").lower()):
        w = m.group(1).strip("'-")
        if len(w) < 5 or w in seen or w in _GENERIC_ROAST_WORDS:
            continue
        # "non-carbonated" is present when the material says
        # "NON CARBONATED"; compare part-wise, not as one token.
        if "-" in w and all(
                p in hay_words or len(p) < 5 for p in w.split("-")):
            continue
        # Singular/plural and simple inflections count as present.
        stems = {w, w.rstrip("s"), w + "s"}
        if w.endswith("ing"):
            stems |= {w[:-3], w[:-3] + "e"}
        if w.endswith("ed"):
            stems |= {w[:-2], w[:-1]}
        if stems & hay_words:
            continue
        seen.add(w)
        out.append(w)
    return out


def _clapback_fidelity_violations(answer: str, material: str) -> list[str]:
    """Claim tokens in `answer` absent from the asker's own material —
    cross-attribution or invention, either way not their receipt."""
    low = (material or "").lower()
    return [t for t in _clapback_claim_tokens(answer)
            if t.lower() not in low]


_HOSTILE_RE = re.compile(
    r"\b(shut up|stfu|fuck you|fuck off|you suck|useless|garbage bot|"
    r"dumb bot|stupid bot|trash bot|retarded bot|dogshit|dog shit|"
    r"worthless|kys|kill yourself|shut the fuck)\b",
    re.IGNORECASE,
)


def _is_hostile_exchange(question: str) -> bool:
    """True when the asker actually came at the bot in THIS message.
    Gates the disengage line (2026-08-25): "you done?" is the right
    answer to an attack and a bizarre one to "remind me to sell
    everything on Sept 15". Deliberately narrow — the cost of missing
    an attack is a plain answer; the cost of a false positive is the
    bot insulting someone who asked a normal question."""
    if not question:
        return False
    # Strip the quoted [MESSAGE BEING REPLIED TO] block: the bot's own
    # prior words are not the asker's hostility.
    q = re.sub(r"\[MESSAGE BEING REPLIED TO.*?\]\s*\".*?\"",
               " ", question, flags=re.S)
    q = re.sub(r"\[VERBATIM RECENT MESSAGES.*?\]", " ", q, flags=re.S)
    return bool(_HOSTILE_RE.search(q))


def _is_clapback_shaped(answer: str) -> bool:
    """True when the answer is a clapback AT THE ASKER — it addresses
    them in second person. The fidelity guard only applies here: a
    third-person informational answer (chat summary, leaderboard, "who
    said X") legitimately names OTHER members and must not be checked
    for 'asker's own material' (2026-07-29: a summary got mangled to a
    mid-sentence 'and' because the guard treated every named member as
    cross-attribution)."""
    if not answer:
        return False
    # An ARROW-FORMATTED answer is the informational shape, never a
    # clapback — even when it closes with a jab that says "your"
    # (2026-08-25: "remind me to sell everything on Sept 15" returned
    # two arrows, one of them a light jab; this counted 2x "you", the
    # fidelity guard then flagged the bot's OWN words "Reminder" and
    # "OPEX" as another member's receipts, stripped both arrows, and
    # shipped the hostile disengage line "you done?" to a benign
    # question). Jab-stripping on informational answers is the
    # `_jab_residual` path's job, not the clapback guard's.
    if "→" in answer:
        return False
    # Count second-person addresses; a stray "your" in an aside isn't a
    # clapback, a wall of "you...you...your" is.
    n = len(re.findall(
        r"\byou(?:r|[’']re|[’']ll|[’']d|[’']ve|s)?\b", answer, re.IGNORECASE))
    return n >= 2


def _extract_code_images(response) -> list[tuple[bytes, str]]:
    """(bytes, mime) for the FINAL image the code-execution response
    rendered. The model often iterates a chart (draft → draft → final),
    each plt.show() emitting an inline image part; surfacing all of them
    posts 3 near-duplicate graphs (2026-07-29). Only the last render is
    the polished one, so return just that."""
    imgs: list[tuple[bytes, str]] = []
    try:
        parts = response.candidates[0].content.parts or []
    except (AttributeError, IndexError, TypeError):
        return imgs
    for p in parts:
        inl = getattr(p, "inline_data", None)
        if inl is None:
            continue
        mime = getattr(inl, "mime_type", "") or ""
        data = getattr(inl, "data", None)
        if data and mime.startswith("image/"):
            imgs.append((data, mime))
    return imgs[-1:]  # final render only


def _build_ask_embeds(full_text: str, code_images):
    """Build the reply embeds + files. When a chart rendered, the image
    goes in its OWN embed placed FIRST, then the text embed — Discord
    always renders an embed's image at the bottom of that embed, so a
    chart-on-top layout needs two ordered embeds (2026-07-29 owner
    request). No chart → a single text embed."""
    text_embed = discord.Embed(description=full_text, color=0x228B22)
    text_embed.set_footer(text="Hi, I'm AI-powered - NFA")
    files = []
    img_embed = None
    for _i, (_data, _mime) in enumerate(code_images[:1]):  # one chart
        _ext = "png" if "png" in _mime else (_mime.split("/")[-1] or "png")
        _fname = f"quant_{_i}.{_ext}"
        try:
            files.append(discord.File(io.BytesIO(_data), filename=_fname))
            img_embed = discord.Embed(color=0x228B22)
            img_embed.set_image(url=f"attachment://{_fname}")
        except Exception as _fe:
            log.warning(f"/ask: chart attach failed (non-fatal): {_fe}")
    embeds = [img_embed, text_embed] if img_embed else [text_embed]
    return embeds, files


def _normalize_ask_result(result):
    """_answer_with_gemini returns a bare discord.Embed on the error/
    guard paths and an (embeds_list, files) tuple on the chart success
    path. Normalize to (embeds_list, files) — always a list — so the
    send sites can `send(embeds=...)` in order (chart first)."""
    if isinstance(result, tuple):
        first, files = result
        files = list(files or [])
    else:
        first, files = result, []
    embeds = first if isinstance(first, list) else [first]
    return embeds, files


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
    channel_id: int | None = None,
    _transient_retry: bool = False,
) -> discord.Embed:
    """Run a Gemini grounded-search query and return a Discord embed.

    Enforces the per-user daily cap. Returns a single embed with the answer
    + sources footer + NFA footer, or an error embed on failure.

    `_transient_retry` is internal: on a transient Gemini failure (500 /
    503 / timeout) the function retries ITSELF once with this flag set,
    so a server blip doesn't surface as a user-facing error. 2026-07-02
    QC: two of five morning asks died on 500 INTERNAL while the same
    question shape worked 21 seconds later — both were one-retry saves.

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

    # Slur-count question shape — bypass Gemini entirely to avoid the
    # filter trip pattern observed on 2026-06-04 (4 blocks in 90 seconds).
    # See module-level _is_slur_count_question for the rationale.
    if _is_slur_count_question(question):
        log.info(
            f"/ask: slur-count query short-circuit "
            f"(asker_id={user_id}, q={question[:80]!r})"
        )
        return await _answer_slur_count_directly(
            question=question,
            user_id=user_id,
            asker_display_name=asker_display_name,
            channel_name=channel_name,
        )

    # Message-count question shape — bypass Gemini, query larger of
    # {100 most recent channel messages, last 6h of channel messages}.
    # Bot's prior failure (2026-06-04 22:29: "BK has sent 9 messages
    # today") came from Gemini inferring the count from the visible
    # recent-50-chat-window block — artificially small. The 6h-or-100
    # logic gives an honest count over a meaningful window. Falls back
    # to Gemini if target user can't be resolved.
    if _is_message_count_question(question):
        log.info(
            f"/ask: message-count query short-circuit attempt "
            f"(asker_id={user_id}, q={question[:80]!r})"
        )
        embed = await _answer_message_count_directly(
            question=question,
            user_id=user_id,
            asker_username=asker_username,
            channel_name=channel_name,
        )
        if embed is not None:
            return embed
        # else: target user couldn't be resolved; fall through to Gemini

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
        # Two tools available to the model:
        #   1. Google Search grounding — for current/factual lookups
        #   2. search_chat_messages — for historical room-chat lookups
        # Gemini requires tool_config.include_server_side_tool_invocations=True
        # to mix a built-in tool (Google Search) with function declarations.
        # Without that flag the API returns 400 INVALID_ARGUMENT. The model
        # picks which tool (or none) based on the question.
        # Safety settings — set ALL categories to BLOCK_NONE on input.
        # The prompt deliberately injects raw verbatim quotes from chat
        # (subject-verbatim block, slur_examples in user profiles) so
        # the bot can analyze room dynamics, racism-rank, and answer
        # questions like "what's Abe's win rate" without the input
        # being rejected. Production log on 2026-05-28:
        #   prompt_block='PROHIBITED_CONTENT'
        # …on a benign "what's Abe's win rate this week" because the
        # subject-verbatim block contained slurs Abe had posted. With
        # default safety, Gemini rejects the whole prompt before
        # generating anything, and the user sees a blank embed (or
        # the misleading "safety filter tripped" fallback). The bot's
        # design intent is to READ this content for analysis; output
        # safety still applies to anything the bot itself emits.
        safety_settings = [
            types.SafetySetting(
                category=cat,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            )
            for cat in (
                types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
            )
        ]

        config = types.GenerateContentConfig(
            system_instruction=_build_runtime_system_instruction(),
            tools=[
                types.Tool(google_search=types.GoogleSearch()),
                # Native code execution (2026-07-29): Google runs the
                # Python in THEIR sandbox — member-commanded code never
                # touches Railway. The model uses it for analytical
                # questions (payoff math, monte carlo, IV, stats) and
                # renders charts. Verified coexisting with the function
                # tools on 3.5-flash-lite. Raw stdout is never posted —
                # the model's composed text answer flows through the
                # existing disclosure/fidelity guards; only rendered
                # chart images are surfaced.
                types.Tool(code_execution=types.ToolCodeExecution()),
                _build_chat_search_tool(),
                _build_user_profile_tool(),
                _build_trade_log_tool(),
                _build_market_price_tool(),
                _build_options_chain_tool(),
                _build_economic_calendar_tool(),
                _build_earnings_date_tool(),
                _build_query_data_tool(),
                _build_price_history_tool(),
                # Sleeper fantasy tool only exists when a league is
                # configured — an unregistered tool costs no schema
                # tokens and can't be miscalled.
                *([_build_fantasy_league_tool()]
                  if (settings.sleeper_league_id or "").strip() else []),
            ],
            tool_config=types.ToolConfig(
                include_server_side_tool_invocations=True,
            ),
            safety_settings=safety_settings,
            # max_output_tokens = 5000 (bumped 2026-05-28 from 4000).
            # Thinking budget bumped to 2000 — Type 1 answers with
            # search grounding can use more reasoning when working
            # through caller-trade context + WHO'S TALKING + the
            # recent-chat block. The 200-word soft cap in the prompt
            # still binds the visible answer; the larger total budget
            # exists to prevent cliff-truncation, not to encourage
            # longer responses.
            max_output_tokens=5000,
            temperature=0.3,
            thinking_config=types.ThinkingConfig(thinking_budget=2000),
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

        # Analyst trade context is no longer auto-injected (was: a
        # multi-caller block from format_analyst_trades_for_context for
        # each configured caller). The model now fetches it on demand
        # via the lookup_trade_log tool when a question references
        # trades. Save ~8-12 KB of prompt per call on questions that
        # don't touch caller trades (most of them).

        # Cross-window anti-recycling block. The 50-msg / 24h chat_context
        # window scrolls past the bot's prior /ask answers to this asker in
        # under 30 min on active channels (stonks-yapping etc.) — so the
        # anti-recycling rule that scans [YOU said earlier]: lines has
        # nothing to act on and recurring hooks like "LARPing as a quant"
        # get reused. This block pulls the last few /ask answers given to
        # this asker in this channel directly from ask_bot_answers
        # (count-bounded, no recency cap) and tags them with the same
        # [YOU said earlier]: prefix so the existing rule covers them.
        cross_window_block = ""
        # Raw prior-answer texts, kept for the code-level roast-recycle
        # guard (the prompt block below is advisory; the guard is not).
        _prior_bot_answer_texts: list[str] = []
        if user_id and channel_id:
            try:
                prior_answers = db.get_recent_bot_answers_to_asker(
                    asker_user_id=user_id,
                    channel_id=channel_id,
                    limit=5,
                )
                _prior_bot_answer_texts = [
                    (row.get("answer") or "") for row in (prior_answers or [])
                ]
                if prior_answers:
                    lines = [
                        "[YOUR RECENT /ASK ANSWERS TO THIS ASKER — "
                        "cross-window anti-recycling guard. These are your "
                        "OWN prior answers; do not reuse the same hooks, "
                        "anecdote pulls, voice opener, or framing twice "
                        "in a row. Pull from a different angle of the "
                        "asker's profile instead.]"
                    ]
                    for row in prior_answers:
                        q_snip = (row.get("question") or "").strip().replace(
                            "\n", " "
                        )[:120]
                        a_snip = (row.get("answer") or "").strip().replace(
                            "\n", " "
                        )[:600]
                        if q_snip:
                            lines.append(
                                f"[YOU said earlier to this asker, "
                                f"re: {q_snip!r}]: {a_snip}"
                            )
                        else:
                            lines.append(
                                f"[YOU said earlier to this asker]: {a_snip}"
                            )
                    cross_window_block = "\n".join(lines)
            except Exception as e:
                log.info(
                    f"Cross-window bot-answers fetch failed (non-fatal): {e}"
                )

        # PROFILE DEPTH decided here, before the send — see the block
        # comment above _lean_profiles_for_prompt. `profiles_block` stays
        # the FULL material because the answer-time guards (clapback
        # fidelity, roast-recycle, pnl-monotone) check the answer against
        # everything we know, not against what we chose to send.
        # `profiles_for_prompt` is the only thing that reaches Gemini,
        # on the first send and on every ladder rebuild.
        _needs_person, _depth_reason = _question_needs_person_material(
            question, profiles_block,
        )
        if profiles_block and not _needs_person:
            profiles_for_prompt = _lean_profiles_for_prompt(profiles_block)
            log.info(
                f"/ask: profile depth LEAN ({_depth_reason}) — dropped "
                f"{len(profiles_block) - len(profiles_for_prompt)} chars "
                f"of voice/racism material the question doesn't need"
            )
        else:
            profiles_for_prompt = profiles_block

        sections: list[str] = []
        if profiles_for_prompt:
            sections.append(profiles_for_prompt)
        if fetched_urls:
            sections.append(fetched_urls)
        if cross_window_block:
            sections.append(cross_window_block)
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

        # Build the initial user turn as a structured Content object so
        # we can append follow-up turns during the tool-calling loop.
        # Images go first as Parts so the model sees them before the
        # text question.
        initial_parts: list = []
        if images:
            for img_bytes, mime in images:
                initial_parts.append(
                    types.Part.from_bytes(data=img_bytes, mime_type=mime)
                )
        initial_parts.append(types.Part.from_text(text=user_content))
        contents: list = [types.Content(role="user", parts=initial_parts)]

        ask_model = settings.ask_gemini_model or settings.gemini_model

        # Intent router (structural grounding). Decide up front whether
        # this question needs the open web. If it does, swap the
        # multi-tool config for a SEARCH-ONLY one so Google Search is the
        # model's only move and the answer is grounded BY CONSTRUCTION —
        # no post-hoc keyword detection, no discretionary skip. Banter /
        # self-data questions keep the full tool set. The post-hoc
        # grounding backstop stays only as a thin net for router
        # misclassification (it should now rarely fire).
        needs_web, _route_is_factual = await _classify_ask_needs_web(
            client, ask_model, safety_settings, question
        )
        # Deterministic WEB override for quote/lyric completions
        # (2026-07-12): "finish the song lyrics: ..." routed LOCAL and
        # the model invented a bar ("whole team winnin'" — the real line
        # is elsewhere in the track it demonstrably knew). Completing a
        # verbatim text is a lookup, not a memory exercise; the router's
        # WEB rubric never named the shape, so name it in code.
        if _QUOTE_COMPLETION_RE.search(question or "") and not needs_web:
            needs_web = True
            _route_is_factual = True
            log.info(
                "/ask: quote/lyric-completion shape — forcing WEB route"
            )
        # (2026-07-16: the earnings-date LOCAL override was removed —
        # with unified tooling, lookup_earnings_date is reachable on
        # every route, which was the whole point of the override.)
        # QC metadata accumulated through the whole answer path and
        # stamped into the ask-log entry — makes route/grounding/guard
        # decisions auditable instead of forensic (Railway logs rotate
        # away in ~1h; the ask-log is the durable record).
        _ask_meta: dict = {
            "route": "WEB" if needs_web else "LOCAL",
            "kind": "FACT" if _route_is_factual else "BANTER",
            "guards": [],
            # Which profile shape actually went to Gemini, and why. The
            # filter-block post-mortems before 2026-08-10 all had to
            # reconstruct this from the logged prompt text.
            "profile_depth": (
                f"{'full' if _needs_person else 'lean'}:{_depth_reason}"
            ),
            # Image count matters for QC: 0 means any "your screenshot
            # shows X" in the answer is a phantom read (2026-07-10).
            "images": len(images or []),
        }
        # UNIFIED TOOLING (2026-07-16 structural fix). The WEB route used
        # to swap in a SEARCH-ONLY config — which amputated the bot's own
        # financial-data tools. Repeated damage: earnings-date questions
        # couldn't reach lookup_earnings_date (kloh, 07-15), $ALP price/
        # filing questions couldn't reach lookup_market_price, and the
        # resulting ungrounded answers shipped stacked with "couldn't
        # verify" hedges for data that was one tool call away. Search-only
        # never delivered its promise anyway — grounding stayed
        # discretionary and the model skipped it regardless (07-08
        # diagnosis). Now EVERY ask gets the full config (google_search +
        # all data tools, mixed mode); the router's verdict survives as
        # (a) the FACT/BANTER register signal and (b) `needs_web` feeding
        # the grounding backstop's scrutiny — enforcement moved fully to
        # the backstop ladder, where it actually works.
        _fact_extra = _ASK_FACT_DIRECTIVE if _route_is_factual else ""
        # Analysis directive is route-independent — "analyze the trader
        # log" is LOCAL/BANTER but still needs the run-code push.
        _analysis_extra = (
            _ASK_ANALYSIS_DIRECTIVE if _is_analysis_request(question) else ""
        )
        if _analysis_extra:
            _ask_meta["guards"].append("analysis-directive")
        log.info(
            f"/ask: intent-router → "
            f"{'WEB' if needs_web else 'LOCAL'}/"
            f"{'FACT' if _route_is_factual else 'BANTER'} "
            f"{'ANALYSIS ' if _analysis_extra else ''}"
            f"(unified multi-tool pass) q={question[:80]!r}"
        )
        # Protected-members directive (2026-08-05 user request): never
        # insult / clap back / sarcasm; defend and praise with grounded
        # material. Rides _prompt_extra so every directive-preserving
        # retry carries it.
        try:
            _prot_all = (settings.protected_user_id_set
                         | db.get_promoted_protected_ids())
        except Exception:
            _prot_all = settings.protected_user_id_set
        _prot_in_scope = _protected_in_scope(
            user_id, question, profile_user_ids, _prot_all,
        )
        _protected_extra = _build_protected_directive(
            _prot_in_scope, user_id, asker_display_name,
        )
        _asker_protected = int(user_id) in _prot_in_scope
        if _protected_extra:
            _ask_meta["guards"].append("protected-member")
        _prompt_extra = _fact_extra + _analysis_extra + _protected_extra
        if _prompt_extra:
            # The config was built before the router ran — patch the
            # directive(s) in rather than rebuilding the tools.
            config.system_instruction = (
                _build_runtime_system_instruction(_prompt_extra)
            )

        # Pre-flight identity + dispute notes (2026-07-17 Morgan
        # incident) — mechanical detections appended to the user turn so
        # the model gets a targeted, binding directive for exactly the
        # failure shape in play. The name check is LOCAL-gated (public
        # figures in WEB lookups would false-trigger it).
        _preflight_notes = ""
        if not needs_web:
            try:
                _known_surface = " ".join(filter(None, [
                    profiles_block or "", chat_context or "",
                    asker_display_name or "", asker_username or "",
                ]))
                _unknowns = _unknown_member_names(question, _known_surface)
                if _unknowns:
                    _preflight_notes += _name_check_note(_unknowns)
                    _ask_meta["guards"].append(
                        "name-check:" + ",".join(_unknowns)
                    )
                    log.info(
                        f"/ask: unknown person name(s) in question "
                        f"({_unknowns}) — NAME CHECK note appended"
                    )
            except Exception as e:
                log.warning(f"/ask: name-check failed (non-fatal): {e}")
        if _is_disputing_reply(question):
            _preflight_notes += _DISPUTE_NOTE
            _ask_meta["guards"].append("dispute-check")
            log.info("/ask: reply disputes a prior bot claim — "
                     "DISPUTE CHECK note appended")
        if _preflight_notes:
            initial_parts[-1] = types.Part.from_text(
                text=user_content + _preflight_notes
            )
            contents[0] = types.Content(role="user", parts=initial_parts)

        # Token-budget reservation BEFORE the call. /ask assembles
        # a large prompt (WHO'S TALKING + analyst log + recent chat +
        # question) that can hit 50k chars (~13k tokens). With the
        # tool-call loop, total per-question spend can hit 50k+ tokens
        # on a thrashing question. Reserve conservatively for the full
        # loop budget; record actual after.
        from ai_analysis.token_budget import get_budget, BudgetExceeded
        # Heuristic: input ~user_content chars / 4 + per-round 5000
        # output cap, scaled by max rounds.
        _ask_est_per_round = (
            len(user_content) // 4 + 5000 + 500
        )
        _ask_est_total = _ask_est_per_round * (_CHAT_SEARCH_MAX_ROUNDS + 1)
        try:
            get_budget().reserve_or_raise(
                estimated_tokens=_ask_est_total,
                caller=f"ask:{(question or '')[:60]}",
            )
        except BudgetExceeded as e:
            log.warning(f"/ask blocked by token budget: {e}")
            return discord.Embed(
                description=(
                    "→ Daily token budget reached — try again after "
                    "UTC midnight, or ask a quicker question."
                ),
                color=0xE67E22,
            )

        # Tool-calling loop. On each round we call Gemini; if the
        # response has function_call parts, we execute them and feed
        # the results back. Loop exits when the model returns a
        # text-only response (the final answer) or we hit the
        # iteration cap.
        response = None
        _ask_actual_total = 0
        # Tool trace accumulated across rounds — appended to the ask-log
        # so QC (human + automated grader) can see which tools ran and
        # what they returned. Without it, tool-grounded answers look
        # fabricated to the grader.
        _ask_tool_trace: list[dict] = []
        # Grounding evidence accumulated across rounds (2026-07-16 fix).
        # In the unified mixed-tool config the model often searches FIRST
        # and then calls a function tool; the search's grounding_metadata
        # rides on that EARLIER round's response. Reading gm only off the
        # final text response threw the receipt away — a correct, freshly
        # searched TSM-earnings answer stamped 'ungrounded' and shipped
        # wearing a "couldn't verify" hedge. Collect every round's chunks.
        _round_gm_chunks: list = []
        for round_idx in range(_CHAT_SEARCH_MAX_ROUNDS + 1):
            # Contents-size guard: fail CLEANLY (friendly reply + a log
            # that names the biggest parts) instead of letting the API
            # 400 on the 1M-token limit with zero diagnostics.
            if round_idx > 0:
                _part_sizes = []
                for _ci, _c in enumerate(contents):
                    for _p in (getattr(_c, "parts", None) or []):
                        _sz = len(getattr(_p, "text", None) or "") or len(
                            str(getattr(_p, "function_response", None) or "")
                        )
                        if _sz:
                            _part_sizes.append((_sz, _ci))
                _total = sum(s for s, _ in _part_sizes)
                if _total > 2_500_000:
                    _top = sorted(_part_sizes, reverse=True)[:5]
                    log.error(
                        f"/ask: contents grew to {_total} chars before "
                        f"round {round_idx} — aborting before the API "
                        f"400s. Largest parts (chars, content_idx): {_top}"
                    )
                    raise RuntimeError(
                        f"ask contents oversized ({_total} chars) — "
                        f"tool loop ballooned the request"
                    )
            response = await client.aio.models.generate_content(
                model=ask_model,
                contents=contents,
                config=config,
            )
            try:
                _rgm = response.candidates[0].grounding_metadata
                _round_gm_chunks.extend(
                    getattr(_rgm, "grounding_chunks", None) or []
                )
            except (AttributeError, IndexError, TypeError):
                pass
            # Tally actual usage per round so the budget reflects
            # what we really spent rather than the reservation.
            try:
                um = response.usage_metadata
                _ask_actual_total += (
                    (um.prompt_token_count or 0)
                    + (um.candidates_token_count or 0)
                )
            except Exception:
                pass
            # Pull function_call parts off the response, if any.
            function_calls = []
            response_parts = []
            try:
                response_parts = list(
                    response.candidates[0].content.parts or []
                )
            except (AttributeError, IndexError, TypeError):
                response_parts = []
            for p in response_parts:
                fc = getattr(p, "function_call", None)
                if fc and getattr(fc, "name", None):
                    function_calls.append(fc)
            if not function_calls:
                break  # No more tool calls — final answer is in response.text
            if round_idx >= _CHAT_SEARCH_MAX_ROUNDS:
                log.warning(
                    f"/ask: hit tool-calling round cap "
                    f"({_CHAT_SEARCH_MAX_ROUNDS}) with function_calls "
                    f"still pending — forcing a final answer from what "
                    f"was already gathered"
                )
                # Don't ship the pending function-call turn: it carries
                # NO text, so response.text is empty and the user gets
                # "No response came back (reason: STOP)" despite every
                # tool having succeeded (2026-07-29). Make ONE more call
                # with tools DISABLED so the model must write prose from
                # the results already in `contents`.
                try:
                    _cap_contents = list(contents) + [types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=(
                            "[ANSWER NOW] You've used your tool budget. "
                            "Do NOT request more data. Write the answer "
                            "from what you already retrieved above. If "
                            "some piece is genuinely missing, answer "
                            "with what you have and say plainly what "
                            "you couldn't get."
                        ))],
                    )]
                    _cap_cfg = types.GenerateContentConfig(
                        system_instruction=(
                            _build_runtime_system_instruction(_prompt_extra)
                        ),
                        # Keep CODE EXECUTION available — it needs no
                        # new data and is how the answer gets computed
                        # and charted. Only the data-fetching function
                        # tools are withheld, so the model can't spend
                        # more budget looking things up. (2026-07-29: an
                        # EMPTY tool list here produced a correct prose
                        # answer with NO chart, because it killed the
                        # sandbox along with the lookups.)
                        tools=[types.Tool(
                            code_execution=types.ToolCodeExecution()
                        )],
                        safety_settings=safety_settings,
                        max_output_tokens=5000,
                        temperature=0.3,
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=2000),
                    )
                    _cap_resp = await client.aio.models.generate_content(
                        model=ask_model,
                        contents=_cap_contents,
                        config=_cap_cfg,
                    )
                    # Tally inline — _tally_retry_usage is defined
                    # later in this function, after the tool loop.
                    try:
                        _um = _cap_resp.usage_metadata
                        _ask_actual_total += (
                            (_um.prompt_token_count or 0)
                            + (_um.candidates_token_count or 0)
                        )
                    except Exception:
                        pass
                    try:
                        _cap_text = (_cap_resp.text or "").strip()
                    except Exception:
                        _cap_text = ""
                    if _cap_text:
                        response = _cap_resp
                        _ask_meta["guards"].append("round-cap-final-answer")
                        log.info(
                            "/ask: round-cap final answer produced "
                            f"{len(_cap_text)} chars"
                        )
                except Exception as _ce:
                    log.warning(
                        f"/ask: round-cap final answer failed "
                        f"(non-fatal): {_ce}"
                    )
                break

            # Echo the model's tool-call turn into history so the next
            # call has full context — minus any inline artifact the API
            # won't accept back (code-execution can emit
            # application/octet-stream files that 400 the next round).
            contents.append(
                types.Content(
                    role="model", parts=_safe_echo_parts(response_parts)
                )
            )
            # Execute each function call and build function_response parts.
            # Executor map replaces the prior if/elif chain — single
            # guarded call site so an UNCAUGHT exception inside any
            # executor degrades to a tool-error result the model can
            # work around, instead of killing the whole /ask interaction
            # (2026-06-10 second-pass review finding #2).
            _tool_executors = {
                "search_chat_messages": _execute_chat_search,
                "lookup_user_profile": _execute_user_profile,
                "lookup_trade_log": _execute_trade_log,
                "lookup_market_price": _execute_market_price,
                "lookup_options_chain": _execute_options_chain,
                "lookup_economic_calendar": _execute_economic_calendar,
                "lookup_earnings_date": _execute_earnings_date,
                "query_data": _execute_query_data,
                "lookup_price_history": _execute_price_history,
                "lookup_fantasy_league": _execute_fantasy_league,
            }
            tool_response_parts = []
            for fc in function_calls:
                try:
                    args = dict(fc.args) if fc.args else {}
                except Exception:
                    args = {}
                executor = _tool_executors.get(fc.name)
                if executor is None:
                    log.warning(f"/ask: unknown tool call {fc.name!r}")
                    tool_response_parts.append(
                        types.Part.from_function_response(
                            name=fc.name,
                            response={"error": f"unknown tool {fc.name}"},
                        )
                    )
                    _ask_tool_trace.append(
                        {"tool": fc.name, "status": "unknown_tool"}
                    )
                    continue
                try:
                    result = await executor(args)
                except Exception as e:
                    # Degrade, don't die: the model gets a structured
                    # error and can answer from remaining context.
                    log.warning(
                        f"/ask: tool {fc.name} raised: {e}", exc_info=True
                    )
                    result = {
                        "status": "error",
                        "error": (
                            f"{fc.name} failed internally — that lookup "
                            f"is unavailable right now. Answer from what "
                            f"you have; tell the asker the live lookup "
                            f"didn't go through. Do NOT fabricate the "
                            f"data it would have returned."
                        ),
                    }
                # Size clamp (2026-07-17: a request blew Gemini's 1M
                # input-token limit — 400 INVALID_ARGUMENT — because a
                # tool result ballooned the contents across rounds).
                # Bound every tool result; log the offender so the next
                # oversized return is diagnosable in one log line.
                _res_str = str(result)
                if len(_res_str) > _TOOL_RESULT_CHAR_CAP:
                    log.warning(
                        f"/ask: tool {fc.name} returned "
                        f"{len(_res_str)} chars (args={args!r}) — "
                        f"clipping to {_TOOL_RESULT_CHAR_CAP}"
                    )
                    result = {
                        "status": "truncated",
                        "note": (
                            f"result was {len(_res_str)} chars — "
                            f"truncated to fit the context window"
                        ),
                        "content": _res_str[:_TOOL_RESULT_CHAR_CAP],
                    }
                # Scrub non-finite floats before they reach the API.
                # NaN/Infinity are NOT valid JSON — one NaN anywhere in a
                # tool result 400s the ENTIRE request ("Invalid JSON
                # payload... Unexpected token NaN"), which the user sees
                # as "something broke the model" (2026-07-29,
                # lookup_price_history on a non-trading day). Fixed at
                # the executor too; this is the loop-wide backstop so no
                # future tool can reintroduce it.
                result = _json_safe(result)
                tool_response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name,
                        response={"result": result},
                    )
                )
                # Compact tool trace for the ask-log (QC-grader input —
                # without this, tool-grounded answers look fabricated to
                # the grader because the log has no record the tool ran).
                _trace_status = (
                    result.get("status", "ok")
                    if isinstance(result, dict) else "ok"
                )
                _trace_args = {
                    k: (str(v)[:80]) for k, v in list(args.items())[:5]
                }
                _ask_tool_trace.append({
                    "tool": fc.name,
                    "args": _trace_args,
                    "status": _trace_status,
                    "result_chars": len(str(result)),
                })
            contents.append(
                types.Content(role="user", parts=tool_response_parts)
            )

        # Token-budget reconciliation MOVED to the end of this function
        # (2026-06-10): it previously ran here — before the repetition /
        # voice-strip / slur-mask retries — so retry calls burned tokens
        # the budget never saw. Each retry below adds its usage via
        # _tally_retry_usage; the single record_actual runs after all
        # of them (just before the quota record).
        def _tally_retry_usage(resp) -> None:
            nonlocal _ask_actual_total
            try:
                um = resp.usage_metadata
                _ask_actual_total += (
                    (um.prompt_token_count or 0)
                    + (um.candidates_token_count or 0)
                )
            except Exception:
                pass

        # Pull response.text defensively — the SDK raises if the response
        # has no candidates or only function-call parts. Treat all failures
        # as "no text" and let the empty-answer branch below produce a
        # human-readable fallback instead of a blank Discord embed.
        answer = ""
        try:
            answer = (response.text or "").strip() if response else ""
        except Exception as e:
            log.warning(f"/ask: response.text raised: {e}")
            answer = ""

        # Charts rendered by the code-execution sandbox (matplotlib →
        # inline image parts). Collected here off the final response;
        # attached to the reply at the send site. Text still carries
        # the composed answer + passes every downstream guard.
        _code_images = _extract_code_images(response) if response else []
        if _code_images:
            _ask_meta["guards"].append(f"code-charts:{len(_code_images)}")

        # Repetition-glitch detection + one-shot retry. Gemini Flash Lite
        # occasionally produces token-loop artifacts at the end of an
        # answer ("compounding risk and volatility decay risks of
        # volatility decay and volatility" — 9 hits across 2026-05-30
        # logs). Single retry with bumped temperature usually breaks the
        # loop. If retry still glitches, ship the original — the user
        # gets SOMETHING rather than blank.
        if answer and _has_repetition_glitch(answer):
            _ask_meta["guards"].append("repetition")
            log.warning(
                f"/ask: repetition glitch in answer (q={question[:80]!r}); "
                f"retrying once at higher temp"
            )
            try:
                retry_config = types.GenerateContentConfig(
                    system_instruction=_build_runtime_system_instruction(_prompt_extra),
                    tools=[
                        types.Tool(google_search=types.GoogleSearch()),
                        _build_chat_search_tool(),
                        _build_user_profile_tool(),
                _build_trade_log_tool(),
                _build_market_price_tool(),
                _build_options_chain_tool(),
                _build_economic_calendar_tool(),
                _build_earnings_date_tool(),
                *([_build_fantasy_league_tool()]
                  if (settings.sleeper_league_id or "").strip() else []),
                    ],
                    tool_config=types.ToolConfig(
                        include_server_side_tool_invocations=True,
                    ),
                    safety_settings=safety_settings,
                    max_output_tokens=5000,
                    temperature=0.7,  # bumped from 0.3 to break the loop
                    thinking_config=types.ThinkingConfig(thinking_budget=2000),
                )
                retry_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=contents,
                    config=retry_config,
                )
                _tally_retry_usage(retry_resp)
                try:
                    retry_answer = (retry_resp.text or "").strip()
                except Exception:
                    retry_answer = ""
                if retry_answer and not _has_repetition_glitch(retry_answer):
                    answer = retry_answer
                    response = retry_resp
                    log.info("/ask: repetition retry succeeded")
                else:
                    log.warning(
                        "/ask: repetition retry didn't fix glitch — "
                        "falling back to sentence strip"
                    )
            except Exception as e:
                log.warning(f"/ask: repetition retry call failed: {e}")
            # Strip fallback (2026-07-22 terlin calendar answer: the
            # retry re-glitched and the old failure path shipped the
            # loop to Discord untouched). The glitch is end-of-
            # generation junk confined to its sentence/bullet — excise
            # exactly those and keep the clean remainder. If the WHOLE
            # answer is glitch, ship the original: something beats
            # blank, and the ask-log marker makes it visible to QC
            # either way.
            if answer and _has_repetition_glitch(answer):
                _glitch_sents = _repetition_glitch_sentences(answer)
                _stripped = _strip_sentences(answer, _glitch_sents)
                if _stripped and not _has_repetition_glitch(_stripped):
                    answer = _stripped
                    _ask_meta["guards"].append("repetition-strip")
                    log.warning(
                        f"/ask: hard-stripped {len(_glitch_sents)} "
                        f"glitching sentence(s) after failed retry"
                    )
                else:
                    _ask_meta["guards"].append("repetition-shipped")
                    log.warning(
                        "/ask: glitch survived strip fallback — "
                        "shipping original answer"
                    )

        # Voice cleanup on the final answer. The pulse-side lint runs
        # at AUDIT->SCRUB; /ask answers ship straight from Gemini to
        # Discord with no scrub pass. Run a mechanical strip for the
        # deterministic violations (em-dash inside sentences, semicolons
        # mid-sentence) and log any other lint hits so we can track them
        # without rewriting natural prose. The 2026-05-30->06-01 ask log
        # had 13+ em-dash hits across the three days; this catches them
        # all at the bot boundary.
        # Snapshot the RAW model output before any cleanup/rewrites —
        # the ask-log records it so QC sees ground truth, not just the
        # post-lint version (2026-06-10 review finding #5).
        _raw_answer_pre_clean = answer
        # Must be bound even on the empty-answer path — the register-
        # rewrite gate below reads it unconditionally (2026-07-05
        # UnboundLocalError: a blank Gemini payload skipped the `if
        # answer:` block, leaving hit_kinds undefined).
        hit_kinds: list[str] = []
        if answer:
            answer, hit_kinds = _clean_voice_violations(answer)
            if hit_kinds:
                log.info(
                    f"/ask: voice-cleanup hits ({len(hit_kinds)}): "
                    f"{sorted(set(hit_kinds))[:8]}"
                )
        # Asker-mockery guard (FACT-gated): a sincere question answered
        # with derision at the asker feeds the same detect→rewrite pass
        # as the other register violations; hard-strip fallback after.
        if answer and _route_is_factual and _asker_mockery_violations(answer):
            hit_kinds.append("asker-mockery")
            log.warning(
                f"/ask: asker-mockery on a FACT question "
                f"(q={question[:80]!r}) — feeding register rewrite"
            )

        # Architecture-leak rewrite. The 2026-06-01 QC caught one shipped:
        # SV asked "what was discussed in chat between 5pm and 9pm est"
        # and the bot returned "Can't pull a clean summary for that
        # specific window — the chat logs available to me don't cover
        # that block of time in enough detail to give you a reliable
        # read on it." Voice lint DETECTS "available to me" / "in enough
        # detail to" / "the chat logs available" but the mechanical pass
        # only strips em-dashes/semicolons — leaked phrases ship. When
        # any 'meta-narration' kind fires, do a one-shot Gemini rewrite
        # with a tiny prompt. No tools, low budget. If the rewrite also
        # leaks (or fails), ship the original — better SOMETHING than
        # blank.
        _register_rewrite_kinds = {
            "meta-narration", "passive-aggressive", "asker-mockery"
        } & set(hit_kinds or [])
        if answer and _register_rewrite_kinds:
            _ask_meta["guards"].extend(
                f"register:{k}" for k in sorted(_register_rewrite_kinds)
            )
            log.warning(
                f"/ask: register violation shipped through lint "
                f"({sorted(_register_rewrite_kinds)}, q={question[:80]!r}); "
                f"requesting rewrite"
            )
            try:
                _pa_directive = (
                    "Convert any passive-aggressive or condescending "
                    "faux-advice construction into a DIRECT statement. KILL "
                    "the entire 'maybe if you...' redirect-advice family — "
                    "every shape where you tell the target to spend/put/"
                    "channel their energy/time/effort/focus elsewhere: "
                    "'maybe put that energy into X', 'maybe if you spent "
                    "less time on X and more time on Y', 'if you put half "
                    "the energy into X', 'maybe focus on X instead of Y'. "
                    "Also 'do with that what you will', 'if you say so'. "
                    "Don't advise the target to do anything — state the jab "
                    "as a fact using the same material. Instead of 'maybe "
                    "put that energy into a real trade', say what's true: "
                    "'your last five trades were paperhanded exits'. No "
                    "sardonic wind-up, no advice framing, no 'maybe'. "
                ) if "passive-aggressive" in _register_rewrite_kinds else ""
                _am_directive = (
                    "The asker asked a SINCERE factual question. Remove "
                    "every sentence or clause that mocks them for asking "
                    "or invents a premise they never stated — 'you're "
                    "confusing X with Y', 'stop looking for...', 'that's "
                    "you coping'. Keep ALL the factual content. The "
                    "answer should read like a knowledgeable trader "
                    "answering a colleague, not slapping them. "
                ) if "asker-mockery" in _register_rewrite_kinds else ""
                # SUBJECT MATERIAL (2026-07-17 fix): this rewrite used
                # to receive ONLY the original answer — told to remove
                # the banned register shapes AND keep the length, with
                # no profile to draw on, it invented characterization
                # ("manifestos", "stoic strategist") that belonged to
                # nobody. The subject's dossier is now the only allowed
                # replacement material, and a novel-content check below
                # rejects rewrites that invent anyway.
                _rw_material = (profiles_block or "")[:6000]
                rewrite_prompt = (
                    "Rewrite the following Discord bot answer so it sounds "
                    "like a trader talking to another trader. Strip ANY "
                    "phrase that exposes the bot's internal data access or "
                    "limitations — phrases like 'available to me', 'in my "
                    "context', 'the chat logs available', 'in enough detail "
                    "to', 'I can search', 'my tools'. If the answer is a "
                    "decline ('can't pull that one'), keep the decline but "
                    "drop the architecture excuse — just say what you don't "
                    "have, not why your data layer doesn't have it. "
                    + _pa_directive + _am_directive +
                    "Do NOT add any new facts, names, tickers, or numbers. "
                    "Every characterization detail in your rewrite must "
                    "already appear in the ORIGINAL below or in the SUBJECT "
                    "MATERIAL — inventing traits, habits, or behaviors for "
                    "the target is a hard failure. If removing a banned "
                    "shape leaves a hole, fill it ONLY from the SUBJECT "
                    "MATERIAL. Keep the same length, voice, and substance. "
                    "Output ONLY the rewritten answer, no preamble.\n\n"
                    + (
                        f"SUBJECT MATERIAL (the only allowed source for "
                        f"replacement content):\n{_rw_material}\n\n"
                        if _rw_material else ""
                    )
                    + "ORIGINAL:\n"
                    f"{answer}"
                )
                rewrite_config = types.GenerateContentConfig(
                    system_instruction=(
                        "You are a senior trader rewriting another trader's "
                        "message. Be direct, plain-English, no AI tells, no "
                        "self-references to data sources or tools."
                    ),
                    safety_settings=safety_settings,
                    max_output_tokens=1500,
                    temperature=0.4,
                    thinking_config=types.ThinkingConfig(thinking_budget=512),
                )
                rewrite_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=rewrite_prompt)],
                        )
                    ],
                    config=rewrite_config,
                )
                _tally_retry_usage(rewrite_resp)
                try:
                    rewritten = (rewrite_resp.text or "").strip()
                except Exception:
                    rewritten = ""
                if rewritten:
                    # Re-lint to make sure the rewrite is actually clean.
                    # asker-mockery isn't in _clean_voice_violations'
                    # vocabulary, so re-check it explicitly.
                    rewritten, rewrite_hits = _clean_voice_violations(rewritten)
                    if ("asker-mockery" in _register_rewrite_kinds
                            and _asker_mockery_violations(rewritten)):
                        rewrite_hits = list(rewrite_hits or [])
                        rewrite_hits.append("asker-mockery")
                    # Fidelity check: reject a rewrite that invented
                    # substance (words traceable to neither the original
                    # answer, the dossier, nor the question). Fiction is
                    # worse than a weak register — ship the original.
                    _novel = _rewrite_novel_ratio(
                        rewritten,
                        f"{answer} {profiles_block or ''} {question or ''}",
                    )
                    if _novel > _REWRITE_NOVEL_MAX_RATIO:
                        _ask_meta["guards"].append("rewrite:novel-rejected")
                        log.warning(
                            f"/ask: register rewrite invented content "
                            f"(novel ratio {_novel:.2f}) — shipping original"
                        )
                    elif not (_register_rewrite_kinds & set(rewrite_hits or [])):
                        answer = rewritten
                        log.info("/ask: register rewrite succeeded")
                    else:
                        log.warning(
                            "/ask: rewrite still carries a register "
                            "violation — shipping original"
                        )
                else:
                    log.warning(
                        "/ask: rewrite returned empty — shipping original"
                    )
            except Exception as e:
                log.warning(f"/ask: architecture-leak rewrite call failed: {e}")

        # Asker-mockery hard-strip fallback — regardless of which path
        # the rewrite took, mockery at a sincere asker never ships. The
        # strip is sentence-level so the factual core survives.
        if answer and _route_is_factual:
            _mock_residual = _asker_mockery_violations(answer)
            if _mock_residual:
                _ask_meta["guards"].append("asker-mockery-strip")
                _stripped_am = _strip_sentences(answer, _mock_residual)
                if _stripped_am.strip():
                    answer = _stripped_am
                    log.warning(
                        f"/ask: hard-stripped {len(_mock_residual)} "
                        f"asker-mockery sentence(s)"
                    )
            # Jab strip (2026-07-27 planets sarcasm) — roast material
            # tacked onto a sincere factual answer. Same strip pattern
            # as mockery; only fires when a factual remainder survives.
            _jab_residual = _fact_jab_sentences(answer)
            if _jab_residual:
                _stripped_jab = _strip_sentences(answer, _jab_residual)
                if _stripped_jab.strip():
                    answer = _stripped_jab
                    _ask_meta["guards"].append("fact-jab-strip")
                    log.warning(
                        f"/ask: hard-stripped {len(_jab_residual)} "
                        f"jab sentence(s) from FACT answer"
                    )

        # Clapback fidelity guard (2026-07-29, BANTER-gated, ungrounded
        # only). Distinctive claims must trace to the ASKER's own
        # material — co-loaded dossiers in multi-party threads are the
        # cross-attribution source (kyle got ZHawk's XSP trade, Austin,
        # and Excel material); grounded banter may legitimately cite
        # the web. One rewrite naming the offenders; then strip; a
        # fully-stripped answer becomes a disengage line — the move the
        # prompt prescribes when the receipts run dry.
        if (answer and not _route_is_factual and not _round_gm_chunks
                and _is_clapback_shaped(answer)):
            # Scope the pool to whoever the roast is ABOUT. On a
            # third-party question the subject's receipts are the correct
            # ones and the asker's are irrelevant; scoping to the asker
            # flagged correct material and could not see the actual
            # cross-attribution case the guard exists to catch.
            _fid_subjects = _roast_subjects(
                question, profiles_block, asker_username, asker_display_name,
            )
            _fid_subject = _fid_subjects[0] if _fid_subjects else None
            if _fid_subjects:
                # Union over EVERY tagged member. "@Tulch vs @Monsoon, who
                # is worse" legitimately draws on both dossiers, and
                # scoping to one would flag the other's correct receipts.
                # The tradeoff is explicit: this cannot detect a swap
                # BETWEEN two people the question named, because both
                # pools are in scope. It still catches material belonging
                # to an uninvolved member or to nobody, which is the
                # cross-attribution shape the guard was built for.
                _fid_disp, _fid_uname = _fid_subjects[0]
                _fid_material = "\n".join(
                    _member_material_surface(
                        profiles_block, chat_context, u, d, question,
                    )
                    for d, u in _fid_subjects
                )
                _ask_meta["fidelity_scope"] = "subject:" + ",".join(
                    u for _d, u in _fid_subjects
                )
            else:
                _fid_disp, _fid_uname = asker_display_name, asker_username
                _ask_meta["fidelity_scope"] = "asker"
                _fid_material = _member_material_surface(
                    profiles_block, chat_context, _fid_uname, _fid_disp,
                    question,
                )
            _fid_viol = _clapback_fidelity_violations(answer, _fid_material)
            # Lowercase personal details are where the wrong-facts
            # complaints live, and the token check above is blind to
            # them (2026-08-25: an entire roast produced ZERO checkable
            # tokens while inventing "pontoon"). Provenance rule: a
            # distinctive word that appears in NEITHER the subject's
            # material NOR the prompt's own context was invented.
            # Surfaced as rewrite input only — a false positive costs
            # one rewrite, never a stripped or mangled answer.
            _fid_invented = _invented_personal_details(
                answer, _fid_material, question, chat_context or "",
            )[:6]
            if _fid_invented:
                _ask_meta["invented_details"] = _fid_invented
            if _fid_viol or _fid_invented:
                _ask_meta["guards"].append("clapback-fidelity")
                _fid_who = (
                    f"{_fid_disp} ({_fid_uname})" if _fid_subject
                    else "the asker"
                )
                log.warning(
                    f"/ask: clapback carries material not belonging to "
                    f"{_fid_who} ({_fid_viol[:6]}) — requesting fidelity "
                    f"rewrite"
                )
                try:
                    _fid_contents = list(contents) + [types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=(
                            "[FIDELITY CHECK] This reply is about "
                            f"{_fid_who}. Your draft attributed material "
                            f"that is NOT theirs: "
                            + ", ".join((_fid_viol + _fid_invented)[:8]) +
                            ". Those belong to other members or to "
                            "nobody — anything listed that appears in "
                            "no profile, no message and no part of this "
                            "prompt was INVENTED, which is how a member "
                            "gets told about a boat they do not own. "
                            "Rewrite the reply using ONLY "
                            f"{_fid_who}'s documented material: their "
                            "profile, their own messages, this "
                            "question. Never substitute a new invented "
                            "specific for a corrected one. If you have "
                            "no fresh receipts left, deliver a short "
                            "one-line disengage instead. Output only "
                            "the reply."
                        ))],
                    )]
                    _fid_resp = await client.aio.models.generate_content(
                        model=ask_model,
                        contents=_fid_contents,
                        config=types.GenerateContentConfig(
                            system_instruction=(
                                _build_runtime_system_instruction(_prompt_extra)
                            ),
                            safety_settings=safety_settings,
                            max_output_tokens=1000,
                            temperature=0.4,
                            thinking_config=types.ThinkingConfig(
                                thinking_budget=512),
                        ),
                    )
                    _tally_retry_usage(_fid_resp)
                    try:
                        _fid_answer = (_fid_resp.text or "").strip()
                    except Exception:
                        _fid_answer = ""
                    if _fid_answer:
                        _fid_answer, _ = _clean_voice_violations(
                            _fid_answer
                        )
                    if _fid_answer and not _clapback_fidelity_violations(
                            _fid_answer, _fid_material):
                        answer = _fid_answer
                        log.info("/ask: fidelity rewrite accepted")
                    else:
                        _bad = [
                            s for s in _split_sentences(answer)
                            if _clapback_fidelity_violations(
                                s, _fid_material)
                        ]
                        _stripped_f = _strip_sentences(answer, _bad)
                        if _stripped_f.strip():
                            answer = _stripped_f
                            _ask_meta["guards"].append(
                                "clapback-fidelity-strip")
                            log.warning(
                                f"/ask: stripped {len(_bad)} "
                                f"non-asker sentence(s) from clapback"
                            )
                        elif _is_hostile_exchange(question):
                            answer = "you done?"
                            _ask_meta["guards"].append(
                                "clapback-fidelity-disengage")
                            log.warning(
                                "/ask: whole clapback was non-asker "
                                "material — disengaging"
                            )
                        else:
                            # The disengage line is a response to an
                            # ATTACK. On a benign question it reads as
                            # the bot being hostile for no reason
                            # (2026-08-25). Re-ask plainly instead.
                            _ask_meta["guards"].append(
                                "clapback-fidelity-plain-retry")
                            log.warning(
                                "/ask: fidelity strip emptied a "
                                "NON-hostile answer — plain re-ask"
                            )
                            try:
                                _plain = await client.aio.models.generate_content(
                                    model=ask_model,
                                    contents=[types.Content(
                                        role="user",
                                        parts=[types.Part.from_text(text=(
                                            "Answer this plainly and "
                                            "usefully. No jokes about "
                                            "the asker, no jabs, no "
                                            "personal material — just "
                                            "the answer.\n\n"
                                            + question[:4000]
                                        ))],
                                    )],
                                    config=types.GenerateContentConfig(
                                        system_instruction=(
                                            _build_runtime_system_instruction(
                                                _prompt_extra)
                                        ),
                                        safety_settings=safety_settings,
                                        max_output_tokens=800,
                                        temperature=0.3,
                                        thinking_config=types.ThinkingConfig(
                                            thinking_budget=256),
                                    ),
                                )
                                _tally_retry_usage(_plain)
                                _pa = (_plain.text or "").strip()
                                if _pa:
                                    _pa, _ = _clean_voice_violations(_pa)
                                answer = _pa or answer
                            except Exception as e:
                                log.warning(f"/ask: plain re-ask failed: {e}")
                except Exception as fe:
                    log.warning(
                        f"/ask: fidelity rewrite call failed "
                        f"(non-fatal): {fe}"
                    )

        # Subject-naming guard (BANTER-gated, third-party roasts only).
        # 2026-08-12: SV asked "is @Tulch still the donkey of the room?"
        # and the reply ran "a guy whose entire member alert ledger...",
        # "give him a week...". Every claim was correctly Tulch's, but the
        # roast never said whose they were, and the room read it as the
        # bot talking about the wrong person. In a fast thread where the
        # bot's reply quotes the ASKER's message, an unnamed "him" has
        # nothing anchoring it to the subject.
        #
        # Deliberately weak: ONE rewrite request, accepted only if it
        # names the subject AND still passes fidelity. Otherwise the
        # original ships. Ambiguous attribution is a readability problem,
        # not a correctness one, and a guard that rewrites correct roasts
        # to fix presentation is how pnl-monotone vandalized a factual
        # answer (2026-08-04).
        # NOT gated on _is_clapback_shaped. That heuristic is the right
        # gate for the fidelity guard, which polices receipts inside a
        # clapback, and the wrong one here: it returns False for the
        # 2026-08-12 Tulch answer, so a naming guard behind it could not
        # fire on the case it was built for. The condition below is
        # narrower and needs no shape heuristic — prose (not the
        # arrow-bullet fact format) that talks ABOUT a third party in the
        # third person and never says who.
        #
        # Measured across all 467 logged answers: 9 replies are
        # third-person prose about a third party, and 4 of them (44%)
        # never name the subject, including the one that drew the
        # complaint. Under the old clapback gate only 1 of those 4 was
        # even visible.
        if (answer and not _route_is_factual and not _round_gm_chunks
                and not answer.lstrip().startswith("→")):
            _nm_subjects = _roast_subjects(
                question, profiles_block, asker_username, asker_display_name,
            )
            _nm_subject = _nm_subjects[0] if _nm_subjects else None
            if _nm_subject:
                _nm_disp, _nm_uname = _nm_subject
                # Naming ANY tagged member anchors the reply. A comparison
                # answer that names one of the two is not ambiguous.
                _named = any(
                    re.search(rf"\b{re.escape(h)}\b", answer, re.I)
                    for d, u in _nm_subjects for h in (d, u) if len(h) >= 3
                )
                if not _named and _THIRD_PERSON_REF_RE.search(answer):
                    _ask_meta["guards"].append("subject-unnamed")
                    log.warning(
                        f"/ask: third-party roast never names its subject "
                        f"({_nm_disp}) — requesting one naming rewrite"
                    )
                    try:
                        _nm_material = _member_material_surface(
                            profiles_block, chat_context, _nm_uname,
                            _nm_disp, question,
                        )
                        _nm_resp = await client.aio.models.generate_content(
                            model=ask_model,
                            contents=list(contents) + [types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=(
                                    "[NAMING] This reply is about "
                                    f"{_nm_disp}, but it never says so. It "
                                    "reads as if it could be about anyone "
                                    "in the room. Rewrite it so "
                                    f"{_nm_disp} is named once, early and "
                                    "naturally. Change NOTHING else: same "
                                    "claims, same jokes, same length, same "
                                    "voice. Do not add material. Output "
                                    "only the reply."
                                ))],
                            )],
                            config=types.GenerateContentConfig(
                                system_instruction=(
                                    _build_runtime_system_instruction(
                                        _prompt_extra)
                                ),
                                safety_settings=safety_settings,
                                max_output_tokens=1000,
                                temperature=0.3,
                                thinking_config=types.ThinkingConfig(
                                    thinking_budget=256),
                            ),
                        )
                        _tally_retry_usage(_nm_resp)
                        try:
                            _nm_answer = (_nm_resp.text or "").strip()
                        except Exception:
                            _nm_answer = ""
                        if _nm_answer:
                            _nm_answer, _ = _clean_voice_violations(_nm_answer)
                        _nm_ok = bool(_nm_answer) and any(
                            re.search(rf"\b{re.escape(h)}\b", _nm_answer,
                                      re.I)
                            for h in (_nm_disp, _nm_uname) if len(h) >= 3
                        ) and not _clapback_fidelity_violations(
                            _nm_answer, _nm_material)
                        if _nm_ok:
                            answer = _nm_answer
                            log.info("/ask: naming rewrite accepted")
                        else:
                            log.info(
                                "/ask: naming rewrite rejected — keeping the "
                                "original (presentation issue, not a "
                                "correctness one)"
                            )
                    except Exception as e:
                        log.warning(f"/ask: naming rewrite failed: {e}")

        # Roast-recycle guard (BANTER-gated) — a roast that remixes the
        # same hooks as a prior answer to this asker reads as "doesn't
        # know you or how to insult you." Force ONE rewrite with the
        # recycled hooks banned; ship the original if the rewrite can't
        # do better (repetition is weak, not dangerous).
        # `not _analysis_extra`: a room-ranking question routes
        # LOCAL/BANTER, so without this an answer built from query_data +
        # Python with a chart attached was eligible to be rewritten as a
        # roast. A roast rewriter has no business touching an analysis.
        # `_is_clapback_shaped(answer)`: the BANTER route is NOT proof
        # the answer is a roast — the router misroutes factual questions
        # to BANTER regularly (citadel 07-30, Boeing + earnings calendar
        # 08-03). On 08-03 the pnl-monotone guard classified a clean
        # factual Boeing answer as a lazy roast (dense trading vocab,
        # zero personal color — the definition of factual) and stapled a
        # personal jab onto every arrow; the asker complained in the
        # room. An answer must actually address the asker in second
        # person before either roast guard may touch it.
        # `not _asker_protected`: these rewrites INJECT jabs (the 08-03
        # Boeing incident) — they must never run on a protected asker.
        if (answer and not _route_is_factual and not _analysis_extra
                and not _asker_protected
                and _is_clapback_shaped(answer)
                and _prior_bot_answer_texts):
            _recycled = _recycled_roast_hooks(answer, _prior_bot_answer_texts)
            if len(_recycled) >= _RECYCLE_HOOK_MIN:
                _ask_meta["guards"].append("roast-recycle")
                log.warning(
                    f"/ask: roast recycles {len(_recycled)} hooks from a "
                    f"prior answer to this asker "
                    f"({', '.join(_recycled[:6])}) — requesting rewrite"
                )
                try:
                    # 2026-07-17 fix: this prompt used to say "rebuild
                    # from material ALREADY in this conversation's
                    # context" while receiving ONLY the original answer
                    # — an instruction it could only satisfy by
                    # inventing. The dossier now rides along as the
                    # actual material, and the novel-content check
                    # below rejects inventions.
                    _rr_material = (profiles_block or "")[:6000]
                    # 2026-07-30 fix: this opened with "rewrite the
                    # following roast" and shipped ONLY the answer, so
                    # every input was treated as a single jab at the
                    # asker. "who are the happiest people in the chat?
                    # How about the angriest" came back with the
                    # happiest arrow deleted. The question rides along
                    # now and the answer has to survive rewriting.
                    _rr_prompt = (
                        "Rewrite the ANSWER below. It is a bot's answer "
                        "to a real question — it may be a roast, or a "
                        "list, or a straight answer. Whatever shape it "
                        "is, the rewrite must STILL ANSWER the question "
                        "in full: every part the question asked for, "
                        "every subject the original covered. If the "
                        "original is arrow bullets, return the same "
                        "number of arrow bullets on the same subjects. "
                        "Never redirect an answer about other people "
                        "into a jab at the asker.\n\n"
                        f"QUESTION:\n{question}\n\n"
                        "The problem to fix: it "
                        "recycles the SAME hooks you already used on this "
                        "person recently — they noticed, and a repeated "
                        "roast reads as not knowing them at all. BANNED "
                        "material for this rewrite (do not mention or "
                        "paraphrase): "
                        + ", ".join(_recycled)
                        + ". Rebuild the jab from DIFFERENT material in "
                        "the SUBJECT MATERIAL below — "
                        "their PERSONAL color first (recent personal "
                        "life, retarded takes, personality); trading-"
                        "loss angles only "
                        "if the ledger material is specific and fresh. "
                        "The SUBJECT MATERIAL is your ONLY allowed "
                        "source — inventing traits, habits, or behaviors "
                        "is a hard failure. Same heat, same length, same "
                        "voice. Do NOT invent new facts, tickers, or "
                        "numbers. Output ONLY the rewritten answer.\n\n"
                        + (
                            f"SUBJECT MATERIAL:\n{_rr_material}\n\n"
                            if _rr_material else ""
                        )
                        + "ORIGINAL ANSWER:\n"
                        f"{answer}"
                    )
                    _rr_config = types.GenerateContentConfig(
                        system_instruction=(
                            "You are a sharp trader rewriting a roast so "
                            "it lands fresh. Direct, in-register, no AI "
                            "tells, no recycled material."
                        ),
                        safety_settings=safety_settings,
                        max_output_tokens=1500,
                        temperature=0.6,
                        thinking_config=types.ThinkingConfig(
                            thinking_budget=512),
                    )
                    _rr_resp = await client.aio.models.generate_content(
                        model=ask_model,
                        contents=[types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=_rr_prompt)],
                        )],
                        config=_rr_config,
                    )
                    _tally_retry_usage(_rr_resp)
                    try:
                        _rr_answer = (_rr_resp.text or "").strip()
                    except Exception:
                        _rr_answer = ""
                    if _rr_answer:
                        _rr_answer, _ = _clean_voice_violations(_rr_answer)
                        _still = _recycled_roast_hooks(
                            _rr_answer, _prior_bot_answer_texts
                        )
                        _rr_novel = _rewrite_novel_ratio(
                            _rr_answer,
                            f"{answer} {profiles_block or ''} "
                            f"{question or ''}",
                        )
                        if _rr_novel > _REWRITE_NOVEL_MAX_RATIO:
                            _ask_meta["guards"].append(
                                "rewrite:novel-rejected"
                            )
                            log.warning(
                                f"/ask: roast-recycle rewrite invented "
                                f"content (novel ratio {_rr_novel:.2f}) "
                                f"— shipping original"
                            )
                        elif len(_still) < _RECYCLE_HOOK_MIN:
                            answer = _rr_answer
                            log.info("/ask: roast-recycle rewrite succeeded")
                        else:
                            log.warning(
                                "/ask: roast-recycle rewrite still recycled "
                                "— shipping original"
                            )
                except Exception as e:
                    log.warning(f"/ask: roast-recycle rewrite failed: {e}")

        # P&L-monotone guard (BANTER-gated) — "your roasts need to
        # target more personal stuff than trading money losses cuz it's
        # just lame and repetitive" (user, 2026-07-10). A roast that is
        # all trading-loss vocabulary and touches NONE of the dossier's
        # personal color gets one rewrite pointed at the personal
        # sections. Ship the original if the rewrite doesn't improve —
        # monotone is weak, not dangerous.
        if (answer and not _route_is_factual and not _analysis_extra
                and not _asker_protected
                and profiles_block
                and _is_clapback_shaped(answer)
                and _roast_is_pnl_monotone(answer, profiles_block)):
            _ask_meta["guards"].append("pnl-monotone")
            log.warning(
                f"/ask: P&L-monotone roast (q={question[:80]!r}) — "
                f"requesting personal-color rewrite"
            )
            try:
                _pm_prompt = (
                    "Rewrite the ANSWER below. It is a bot's answer to a "
                    "real question. The rewrite must STILL ANSWER that "
                    "question in full — every part it asked for, every "
                    "subject the original covered — and keep the same "
                    "shape (arrow bullets in, the same arrow bullets "
                    "out). Never redirect an answer about other people "
                    "into a jab at the asker.\n\n"
                    f"QUESTION:\n{question}\n\n"
                    "The problem to fix: every jab "
                    "in it is a trading-losses jab (bags, exits, account, "
                    "casino) — the laziest register, and this room has "
                    "called it out. Rebuild the heat from the target's "
                    "PERSONAL color that is ALREADY in this conversation's "
                    "context: their recent personal life, retarded takes, "
                    "personality quirks, or what they said in the current "
                    "chat window. One trading reference may survive if "
                    "it's specific, but the roast's spine must be "
                    "personal. Same heat, same length, same voice. Do NOT "
                    "invent new facts, tickers, or numbers — only material "
                    "from the context. Output ONLY the rewritten "
                    "answer.\n\nORIGINAL ANSWER:\n"
                    f"{answer}"
                )
                _pm_config = types.GenerateContentConfig(
                    system_instruction=(
                        "You are a sharp trader rewriting a roast so it "
                        "hits the person, not their P&L. Direct, "
                        "in-register, no AI tells."
                    ),
                    safety_settings=safety_settings,
                    max_output_tokens=1500,
                    temperature=0.6,
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=512),
                )
                _pm_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=_pm_prompt)],
                    )],
                    config=_pm_config,
                )
                _tally_retry_usage(_pm_resp)
                try:
                    _pm_answer = (_pm_resp.text or "").strip()
                except Exception:
                    _pm_answer = ""
                if _pm_answer:
                    _pm_answer, _ = _clean_voice_violations(_pm_answer)
                    if not _roast_is_pnl_monotone(_pm_answer, profiles_block):
                        answer = _pm_answer
                        log.info("/ask: P&L-monotone rewrite succeeded")
                    else:
                        log.warning(
                            "/ask: P&L-monotone rewrite still monotone — "
                            "shipping original"
                        )
            except Exception as e:
                log.warning(f"/ask: P&L-monotone rewrite failed: {e}")

        grounding_metadata = None
        try:
            grounding_metadata = response.candidates[0].grounding_metadata
        except (AttributeError, IndexError, TypeError):
            pass
        if not _grounding_has_sources(grounding_metadata) and _round_gm_chunks:
            # The final text turn carries no gm, but an earlier round of
            # the tool loop searched — that evidence grounds this answer
            # (and feeds the sources footer). Dedup chunks by URI.
            _seen_uris: set[str] = set()
            _merged = []
            for ch in _round_gm_chunks:
                uri = getattr(getattr(ch, "web", None), "uri", None) or id(ch)
                if uri in _seen_uris:
                    continue
                _seen_uris.add(uri)
                _merged.append(ch)
            grounding_metadata = SimpleNamespace(grounding_chunks=_merged)
            log.info(
                f"/ask: grounding recovered from earlier tool-loop round(s) "
                f"({len(_merged)} chunk(s)) — final turn had none"
            )

        # Grounding backstop — structural enforcement of "Type 1 needs a
        # source." If the answer asserts market-fact specifics yet
        # nothing grounded it (no Google grounding, no data tool), force
        # ONE SEARCH-ONLY retry; if that STILL doesn't ground, append a
        # hedge so the unverified specifics aren't presented as fact.
        #
        # SEARCH-ONLY is the load-bearing detail (2026-06-19 fix). Gemini
        # grounding is discretionary, and when google_search rides in the
        # same request as the bot's function tools, the model routinely
        # skips search and answers from priors — which is the
        # confabulation. The earlier version of this retry re-sent ALL
        # the function tools, so it did the exact same thing and fell
        # straight through to the hedge: it confirmed it hadn't searched
        # rather than actually searching. Stripping the function tools so
        # Google Search is the ONLY move makes grounding fire for real,
        # so the retry returns a verified answer and the hedge becomes
        # rare (only when the web genuinely has nothing). The backstop
        # only fires on MARKET-FACT shapes where no tool fired on pass 1,
        # so losing the function tools on retry costs nothing.
        _ground_trigger_shape = _is_ungrounded_market_fact(
            answer, grounding_metadata, _ask_tool_trace,
            context=user_content,
        )
        _ground_trigger_web = _ungrounded_web_specifics(
            answer, grounding_metadata, needs_web,
            is_opinion=_is_opinion_request(question),
        )
        # Calendar-slate questions trigger on question shape, not answer
        # shape — a ticker-and-times slate carries no factual-specific
        # markers the other two nets can see (2026-07-20 terlin). Any
        # data tool firing counts as a source, same as the market-shape
        # net.
        _ground_trigger_calendar = (
            _is_calendar_question(question)
            and not _grounding_has_sources(grounding_metadata)
            and not _ask_tool_trace
        )
        if answer and (_ground_trigger_shape or _ground_trigger_web
                       or _ground_trigger_calendar):
            _ground_trigger_name = (
                "web-routed" if _ground_trigger_web
                else "market-shape" if _ground_trigger_shape
                else "calendar"
            )
            _ask_meta["guards"].append("grounding:" + _ground_trigger_name)
            log.warning(
                f"/ask: ungrounded answer (q={question[:80]!r}, "
                f"trigger={_ground_trigger_name})"
                f" — forcing a search-only retry"
            )
            # Price backstop-fetch (2026-07-27 ORCL contradiction).
            # The forced retry strips the function tools, so an answer
            # asserting a live price could only ever be hedged, never
            # corrected — and banter passes never call the tool on
            # their own. Fetch the asserted tickers deterministically
            # and inject the live numbers; the tool trace entry also
            # lets the retry-acceptance check count this as a source.
            _price_injected = False
            _price_symbols = _answer_price_tickers(answer)
            if _price_symbols:
                try:
                    _price_result = await _execute_market_price(
                        {"symbols": _price_symbols}
                    )
                    if (isinstance(_price_result, dict)
                            and _price_result.get("status") == "ok"):
                        _ask_tool_trace.append({
                            "tool": "lookup_market_price",
                            "args": {"symbols": str(_price_symbols)[:80]},
                            "status": "backstop-fetch",
                            "result_chars": len(str(_price_result)),
                        })
                        contents.append(types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=(
                                "[LIVE PRICE DATA — system-fetched "
                                "because your draft asserted price "
                                "levels no tool sourced]\n"
                                + str(_price_result)[:4000]
                                + "\nUse ONLY these numbers for any "
                                "price/level/percent-move claim in "
                                "your rewrite; drop any price this "
                                "data does not cover. If the data "
                                "contradicts your draft's claim about "
                                "what a ticker is doing, the data "
                                "wins — including any claim you made "
                                "about the asker being wrong."
                            ))],
                        ))
                        _price_injected = True
                        _ask_meta["guards"].append("price-backstop-fetch")
                        log.info(
                            f"/ask: price backstop-fetch injected live "
                            f"data for {_price_symbols}"
                        )
                except Exception as ppe:
                    log.warning(
                        f"/ask: price backstop-fetch failed "
                        f"(non-fatal): {ppe}"
                    )
            try:
                forced_contents = list(contents) + [
                    types.Content(role="user", parts=[types.Part.from_text(
                        text=(
                            "[GROUNDING REQUIRED] Your previous draft stated "
                            "specific facts — numbers, dates, counts, figures, "
                            "price targets, a company's market cap or bed/unit "
                            "count, a contract or unlock schedule — WITHOUT "
                            "consulting any source. Do NOT answer a specific "
                            "from memory or by extrapolating from a pasted "
                            "document. Google Search is now your ONLY tool: "
                            "verify each specific against a real result before "
                            "stating it. If a specific isn't in the search "
                            "results, say you couldn't verify it — never invent "
                            "a date, count, ticker, level, or figure to fill "
                            "the gap."
                        ),
                    )])
                ]
                # SEARCH-ONLY tool config — no function tools, so the model
                # has nothing to route to except Google Search.
                forced_config = types.GenerateContentConfig(
                    system_instruction=_build_runtime_system_instruction(_prompt_extra),
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    safety_settings=safety_settings,
                    max_output_tokens=5000,
                    temperature=0.3,
                    thinking_config=types.ThinkingConfig(thinking_budget=2000),
                )
                forced_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=forced_contents,
                    config=forced_config,
                )
                _tally_retry_usage(forced_resp)
                try:
                    forced_answer = (forced_resp.text or "").strip()
                except Exception:
                    forced_answer = ""
                forced_gm = None
                try:
                    forced_gm = forced_resp.candidates[0].grounding_metadata
                except (AttributeError, IndexError, TypeError):
                    pass
                _retry_still_ungrounded = (
                    # Trace includes the price backstop-fetch when it
                    # ran — a retry built on injected live data counts
                    # as tool-sourced, same as if the model had called
                    # lookup_market_price itself.
                    _is_ungrounded_market_fact(
                        forced_answer, forced_gm, _ask_tool_trace,
                        context=user_content,
                    )
                    or _ungrounded_web_specifics(
                        forced_answer, forced_gm, needs_web,
                        is_opinion=_is_opinion_request(question),
                    )
                    # A calendar retry that again skipped search is still
                    # a memory slate — don't accept it as "hedged"; let
                    # it fall through to the bare probe.
                    or (_ground_trigger_calendar
                        and not _grounding_has_sources(forced_gm))
                )
                if forced_answer and _grounding_has_sources(forced_gm):
                    answer = forced_answer
                    response = forced_resp
                    grounding_metadata = forced_gm
                    _ask_meta["ground_retry"] = "in-voice:grounded"
                    log.info("/ask: grounded retry succeeded")
                elif forced_answer and not _retry_still_ungrounded:
                    # Retry dropped the unverifiable specifics (e.g. said
                    # "couldn't verify") — or rebuilt its price claims on
                    # the injected live data. Either is the honest
                    # outcome; the label records which.
                    answer = forced_answer
                    response = forced_resp
                    grounding_metadata = forced_gm
                    _ask_meta["ground_retry"] = (
                        "in-voice:price-tool" if _price_injected
                        else "in-voice:hedged"
                    )
                    log.info(
                        "/ask: grounded retry accepted "
                        f"({_ask_meta['ground_retry']})"
                    )
                elif not needs_web:
                    # Stage 2 is SKIPPED for LOCAL-routed questions. The
                    # probe's whole mechanism — strip all context so
                    # searching becomes the only move — is wrong when the
                    # answer CAME FROM context: a LOCAL/BANTER question
                    # full of room referents becomes a nonsense web query
                    # (2026-07-16 Cemini: the probe Googled "omniwiz ...
                    # rope and his ladder", grounded a literature page
                    # about executioners, and its refusal replaced an
                    # excellent in-voice GLW read). The in-voice retry
                    # above already attempted grounding WITH context;
                    # failing that, hedge and keep the answer.
                    answer = (
                        answer.rstrip()
                        + "\n\n→ ⚠️ Couldn't verify these specifics "
                        "against a live source — treat the exact "
                        "numbers/dates as unconfirmed."
                    )
                    _ask_meta["ground_retry"] = "hedged(local-skip)"
                    log.warning(
                        "/ask: LOCAL-routed answer failed grounding retry "
                        "— skipped the context-blind bare probe, kept "
                        "in-voice answer + hedge"
                    )
                elif _is_context_dependent(question):
                    # Stage 2 is SKIPPED for context-dependent follow-ups.
                    # The bare probe strips all conversation history, so a
                    # question that references the live thread ("give us 5
                    # from THERE", "what's ITS Q3 number") loses its
                    # antecedent and the probe answers a different,
                    # unanswerable question — 2026-07-13: kloh asked for 5
                    # names "from there" (the OTE report discussed seconds
                    # earlier) and the probe, context-blind, replied "I
                    # cannot verify the existence of the report you
                    # mentioned." It didn't refuse; it forgot, by design.
                    # Keep the context-aware in-voice answer and hedge.
                    answer = (
                        answer.rstrip()
                        + "\n\n→ ⚠️ Couldn't verify these specifics "
                        "against a live source — treat the exact "
                        "numbers/dates as unconfirmed."
                    )
                    _ask_meta["ground_retry"] = "hedged(context-dep-skip)"
                    log.warning(
                        "/ask: context-dependent follow-up — skipped the "
                        "context-blind bare probe, kept in-voice answer + "
                        "hedge"
                    )
                else:
                    # Stage 2 — BARE PROBE. Diagnosis from the 2026-07-08
                    # hedge batch (Toy Story / market-down / Netflix): even
                    # SEARCH-ONLY passes skip the discretionary search when
                    # the request carries the full 8-10K-char room prompt +
                    # persona — the model answers from that context and its
                    # priors instead. The probe strips EVERYTHING except the
                    # question: no profiles, no chat, no persona. With
                    # nothing to answer from, searching becomes the path of
                    # least resistance. Dry output is fine — this path only
                    # runs for self-contained fact questions (context-
                    # dependent ones took the skip branch above), where
                    # correct-and-plain beats in-voice-but-unverified.
                    _probe_ok = False
                    _probe_state = "error"  # overwritten below on a response
                    try:
                        probe_q = (
                            question.strip()[-600:]
                            + _probe_topic_capsule(question, answer)
                        )
                        probe_resp = await client.aio.models.generate_content(
                            model=ask_model,
                            contents=[types.Content(
                                role="user",
                                parts=[types.Part.from_text(text=(
                                    "Verify with Google Search and answer "
                                    "concisely (1-4 short sentences or "
                                    "bullets): " + probe_q
                                ))],
                            )],
                            config=types.GenerateContentConfig(
                                system_instruction=(
                                    "You are a fact-checking search agent. "
                                    "Your FIRST action MUST be a Google "
                                    "Search query — produce no answer text "
                                    "before searching, and never answer "
                                    "from memory alone. State only what the "
                                    "results support; name anything you "
                                    "could not verify. Plain prose, no "
                                    "em-dashes."
                                ),
                                tools=[types.Tool(
                                    google_search=types.GoogleSearch())],
                                safety_settings=safety_settings,
                                max_output_tokens=1000,
                                temperature=0.1,
                                # 1024: at 512 the model sometimes answered
                                # from priors without planning a search
                                # (07-09: probe converted only 1 of 4).
                                thinking_config=types.ThinkingConfig(
                                    thinking_budget=1024),
                            ),
                        )
                        _tally_retry_usage(probe_resp)
                        try:
                            probe_answer = (probe_resp.text or "").strip()
                        except Exception:
                            probe_answer = ""
                        probe_gm = None
                        try:
                            probe_gm = (
                                probe_resp.candidates[0].grounding_metadata
                            )
                        except (AttributeError, IndexError, TypeError):
                            pass
                        _probe_state = "no-ground"
                        if probe_answer and _probe_is_refusal(probe_answer):
                            # A "grounded" I-cannot-verify is not an
                            # answer — never let it replace one.
                            _probe_state = "refusal"
                            probe_answer = ""
                        if probe_answer and _grounding_has_sources(probe_gm):
                            # The probe bypassed the earlier voice-lint pass
                            # — run the mechanical cleaner so em-dashes /
                            # tells don't ship.
                            probe_answer, _ = _clean_voice_violations(
                                probe_answer
                            )
                            answer = probe_answer
                            response = probe_resp
                            grounding_metadata = probe_gm
                            _probe_ok = True
                            _ask_meta["ground_retry"] = "bare-probe:grounded"
                            log.info(
                                "/ask: bare-probe grounded (in-voice retry "
                                "had failed)"
                            )
                            # REVOICE (2026-07-23). The probe is
                            # deliberately persona-less — that dryness is
                            # what makes it search — but every probe
                            # answer in the 07-17..07-23 window failed QC
                            # voice/format for exactly that reason. One
                            # no-tools rewrite turns the verified facts
                            # into arrow-bullet room voice; the fidelity
                            # gate (_revoice_acceptable) rejects any
                            # rewrite that invents substance, in which
                            # case the dry probe answer ships as before.
                            # Grounding receipts stay on probe_gm either
                            # way — the rewrite never touches sources.
                            try:
                                _rv_prompt = (
                                    "Rewrite this verified answer as a "
                                    "sharp trader-to-trader Discord reply: "
                                    "2-4 arrow bullets (each starting "
                                    "'→ '), direct and opinionated, plain "
                                    "English, no em-dashes, no source "
                                    "list, no hedging filler. Do NOT add, "
                                    "change, or drop any fact, number, "
                                    "date, ticker, or name — every "
                                    "specific in your rewrite must appear "
                                    "in the ORIGINAL. Output ONLY the "
                                    "rewritten answer.\n\n"
                                    "QUESTION:\n"
                                    + (question or "").strip()[-600:]
                                    + "\n\nORIGINAL (verified):\n"
                                    + probe_answer
                                )
                                _rv_resp = await (
                                    client.aio.models.generate_content(
                                        model=ask_model,
                                        contents=[types.Content(
                                            role="user",
                                            parts=[types.Part.from_text(
                                                text=_rv_prompt)],
                                        )],
                                        config=types.GenerateContentConfig(
                                            system_instruction=(
                                                "You are a senior trader "
                                                "rewriting a research note "
                                                "for the group chat. Keep "
                                                "every fact identical."
                                            ),
                                            safety_settings=safety_settings,
                                            max_output_tokens=1200,
                                            temperature=0.4,
                                            thinking_config=(
                                                types.ThinkingConfig(
                                                    thinking_budget=512)
                                            ),
                                        ),
                                    )
                                )
                                _tally_retry_usage(_rv_resp)
                                try:
                                    _rv_text = (_rv_resp.text or "").strip()
                                except Exception:
                                    _rv_text = ""
                                if _rv_text:
                                    _rv_text, _ = _clean_voice_violations(
                                        _rv_text
                                    )
                                if _revoice_acceptable(
                                    _rv_text, probe_answer, question
                                ):
                                    answer = _rv_text
                                    _ask_meta["ground_retry"] = (
                                        "bare-probe:grounded+revoiced"
                                    )
                                    log.info(
                                        "/ask: probe revoice accepted"
                                    )
                                else:
                                    _ask_meta["ground_retry"] = (
                                        "bare-probe:grounded(dry)"
                                    )
                                    log.info(
                                        "/ask: probe revoice rejected — "
                                        "shipping dry probe answer"
                                    )
                            except Exception as rve:
                                log.warning(
                                    f"/ask: probe revoice call failed "
                                    f"(non-fatal): {rve}"
                                )
                    except Exception as pe:
                        log.warning(f"/ask: bare probe failed: {pe}")
                    if not _probe_ok:
                        # Still ungrounded — flag rather than ship as fact.
                        # The stamp distinguishes probe-ran-but-didn't-
                        # search from probe-call-died, so the ask-log
                        # shows which failure to tune next.
                        answer = (
                            answer.rstrip()
                            + "\n\n→ ⚠️ Couldn't verify these specifics "
                            "against a live source — treat the exact "
                            "numbers/dates as unconfirmed."
                        )
                        _ask_meta["ground_retry"] = f"hedged(probe:{_probe_state})"
                        log.warning(
                            "/ask: retry + bare probe both ungrounded — "
                            f"appended hedge (probe:{_probe_state})"
                        )
            except Exception as e:
                log.warning(f"/ask: grounded retry call failed: {e}")

        # TA guard — structural suppression of self-generated technical
        # analysis. If the answer makes indicator/level claims that
        # nothing sourced (no grounding, no data tool), regenerate ONCE
        # with a "[NO CHART DATA]" directive; prefer the cleaner result;
        # then HARD-STRIP any indicator sentences that survive (the bot
        # has no indicator feed, so those are always invented). Level
        # claims are left to the regen — stripping prose mid-sentence
        # risks mangling it — with a one-line hedge if they persist.
        if answer and _has_unsourced_ta(
            answer, grounding_metadata, _ask_tool_trace
        ):
            ind0, lvl0 = _ta_violations(answer)
            _ask_meta["guards"].append("ta")
            log.warning(
                f"/ask: unsourced TA answer (q={question[:80]!r}, "
                f"indicators={len(ind0)}, levels={len(lvl0)}) "
                f"— forcing a no-chart-data retry"
            )
            try:
                ta_contents = list(contents) + [
                    types.Content(role="user", parts=[types.Part.from_text(
                        text=(
                            "[NO CHART DATA] Your previous draft made "
                            "technical-analysis claims (indicator reads like "
                            "RSI/MACD/moving averages, or chart levels like "
                            "support/resistance/breakouts/pivots) that NO "
                            "source backed. You have NO chart or indicator "
                            "feed — never state an indicator value or an "
                            "overbought/oversold read from memory. For a price "
                            "level, either attribute it to a named source you "
                            "found via Google Search, or drop it. Re-answer the "
                            "question on fundamentals/catalysts/positioning and "
                            "omit any TA you cannot source."
                        ),
                    )])
                ]
                # SEARCH-ONLY (same rationale as the grounding backstop):
                # if a level can be sourced, search is the only way to do
                # it — bundling the function tools just lets the model
                # skip search and re-confabulate the level from priors.
                ta_config = types.GenerateContentConfig(
                    system_instruction=_build_runtime_system_instruction(_prompt_extra),
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    safety_settings=safety_settings,
                    max_output_tokens=5000,
                    temperature=0.3,
                    thinking_config=types.ThinkingConfig(thinking_budget=2000),
                )
                ta_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=ta_contents,
                    config=ta_config,
                )
                _tally_retry_usage(ta_resp)
                try:
                    ta_answer = (ta_resp.text or "").strip()
                except Exception:
                    ta_answer = ""
                ta_gm = None
                try:
                    ta_gm = ta_resp.candidates[0].grounding_metadata
                except (AttributeError, IndexError, TypeError):
                    pass
                # Prefer the retry when it's clean (grounded, or no TA
                # violations left). Otherwise keep whichever draft has
                # fewer violations as the base for the strip.
                if ta_answer and not _has_unsourced_ta(ta_answer, ta_gm, []):
                    answer = ta_answer
                    response = ta_resp
                    grounding_metadata = ta_gm
                    log.info("/ask: no-chart-data retry returned a clean answer")
                else:
                    if ta_answer:
                        ind_new, lvl_new = _ta_violations(ta_answer)
                        if len(ind_new) + len(lvl_new) < len(ind0) + len(lvl0):
                            answer = ta_answer
                            response = ta_resp
                            grounding_metadata = ta_gm
                    # Hard-strip surviving invented indicator sentences.
                    ind_left, lvl_left = _ta_violations(answer)
                    if ind_left:
                        answer = _strip_sentences(answer, ind_left)
                        log.warning(
                            f"/ask: stripped {len(ind_left)} invented "
                            f"indicator sentence(s)"
                        )
                    # Levels we can't safely strip — hedge once if present.
                    _, lvl_after = _ta_violations(answer)
                    if lvl_after and answer:
                        answer = (
                            answer.rstrip()
                            + "\n\n→ ⚠️ Any chart levels above are unsourced — "
                            "I have no chart feed, so treat them as rough, not "
                            "precise."
                        )
                        log.warning(
                            "/ask: unsourced levels remain — appended TA hedge"
                        )
            except Exception as e:
                log.warning(f"/ask: no-chart-data retry call failed: {e}")

        # Member-outcome guard — clapbacks can't have no truth behind
        # them. If the answer asserts someone's P&L STATE ("underwater
        # on your bags", "down 40%") and this turn consulted no trade
        # data, rewrite the jab onto documented material; strip what
        # survives. (2026-07-02: Cpig clapback asserted "underwater" —
        # his ledger shows zero documented outcomes.)
        _oc_names = _known_member_names()
        if answer and _has_unsourced_outcome_claims(
            answer, _ask_tool_trace, user_content, _oc_names
        ):
            oc0 = _outcome_violations(answer, user_content, _oc_names)
            _ask_meta["guards"].append("outcome")
            log.warning(
                f"/ask: unsourced member-outcome claim(s) "
                f"(q={question[:80]!r}, n={len(oc0)}) — requesting rewrite"
            )
            try:
                oc_prompt = (
                    "Rewrite the following Discord bot answer. It asserts "
                    "someone's profit/loss STATE (e.g. 'underwater', 'down "
                    "bad', 'bleeding', 'down N%', 'his plays are a road to "
                    "ruin') with NO documented source — the trade ledger "
                    "only records what people POST, so an asserted P&L "
                    "state is fabrication. This applies to ANY member named "
                    "in the answer, not just the person being addressed — "
                    "trashing a third member's plays without their ledger "
                    "is the same invention. Replace each "
                    "such claim with what IS verifiable in the answer's own "
                    "remaining material: documented behavior (entries with "
                    "no posted exit, spamming a ticker, their own quoted "
                    "words), or drop the claim. Do NOT add any new facts, "
                    "tickers, percentages, or events. Keep the same length, "
                    "voice, and heat — the jab stays, the invented outcome "
                    "goes. Output ONLY the rewritten answer, no preamble.\n\n"
                    "ORIGINAL:\n"
                    f"{answer}"
                )
                oc_config = types.GenerateContentConfig(
                    system_instruction=(
                        "You are a senior trader rewriting another trader's "
                        "message. Direct, in-register, no AI tells. Never "
                        "state an outcome the material doesn't document."
                    ),
                    safety_settings=safety_settings,
                    max_output_tokens=1500,
                    temperature=0.4,
                    thinking_config=types.ThinkingConfig(thinking_budget=512),
                )
                oc_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=oc_prompt)],
                    )],
                    config=oc_config,
                )
                _tally_retry_usage(oc_resp)
                try:
                    oc_answer = (oc_resp.text or "").strip()
                except Exception:
                    oc_answer = ""
                if oc_answer and not _outcome_violations(
                    oc_answer, user_content, _oc_names
                ):
                    answer = oc_answer
                    log.info("/ask: outcome-claim rewrite succeeded")
                else:
                    # Strip the offending sentences from whichever draft
                    # is cleaner; a clapback minus its invented outcome
                    # is still a clapback.
                    base = oc_answer if (
                        oc_answer
                        and len(_outcome_violations(
                            oc_answer, user_content, _oc_names))
                        < len(oc0)
                    ) else answer
                    to_strip = _outcome_violations(
                        base, user_content, _oc_names)
                    stripped = _strip_sentences(base, to_strip)
                    if stripped:
                        answer = stripped
                        log.warning(
                            f"/ask: stripped {len(to_strip)} unsourced "
                            f"outcome sentence(s)"
                        )
                    else:
                        log.warning(
                            "/ask: outcome strip would empty the answer — "
                            "shipping original"
                        )
            except Exception as e:
                log.warning(f"/ask: outcome-claim rewrite call failed: {e}")

        # Rank-trajectory guard — the bot only has the CURRENT rank
        # snapshot, so a "you lost/dropped/climbed a rank" claim is
        # invented (2026-07-05: told SV he "lost your spot in the top 5"
        # after stating he was #9). Rewrite to drop the trajectory,
        # keeping the current rank + the jab; strip the sentences as a
        # fallback.
        _rank_viol = _rank_trajectory_violations(answer) if answer else []
        if _rank_viol:
            _ask_meta["guards"].append("rank-trajectory")
            log.warning(
                f"/ask: unsourced rank-trajectory claim(s) "
                f"(q={question[:80]!r}, n={len(_rank_viol)}) — requesting rewrite"
            )
            try:
                rk_prompt = (
                    "Rewrite the following answer. It claims someone's rank "
                    "CHANGED over time — lost/dropped/climbed a spot, used to "
                    "be #N, fell out of the top N, took time off and slid. "
                    "You have ONLY the current rank (a snapshot); there is no "
                    "rank history, so any movement claim is invented. Remove "
                    "every rank-movement / rank-history claim. Keep the "
                    "CURRENT rank if it's stated, and keep the rest of the "
                    "jab. Do NOT say anyone gained, lost, dropped, climbed, "
                    "or used to hold a position. Add no new facts. Output "
                    "ONLY the rewritten answer.\n\n"
                    "ORIGINAL:\n"
                    f"{answer}"
                )
                rk_config = types.GenerateContentConfig(
                    system_instruction=(
                        "You edit a trading-room bot's message. Direct, "
                        "in-register. Never assert a rank changed over time."
                    ),
                    safety_settings=safety_settings,
                    max_output_tokens=1500,
                    temperature=0.4,
                    thinking_config=types.ThinkingConfig(thinking_budget=512),
                )
                rk_resp = await client.aio.models.generate_content(
                    model=ask_model,
                    contents=[types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=rk_prompt)],
                    )],
                    config=rk_config,
                )
                _tally_retry_usage(rk_resp)
                try:
                    rk_answer = (rk_resp.text or "").strip()
                except Exception:
                    rk_answer = ""
                if rk_answer and not _rank_trajectory_violations(rk_answer):
                    answer = rk_answer
                    log.info("/ask: rank-trajectory rewrite succeeded")
                else:
                    stripped = _strip_sentences(answer, _rank_viol)
                    if stripped and len(stripped) > 15:
                        answer = stripped
                        log.warning(
                            f"/ask: stripped {len(_rank_viol)} rank-trajectory "
                            f"sentence(s)"
                        )
                    else:
                        log.warning(
                            "/ask: rank-trajectory strip would empty the "
                            "answer — shipping original"
                        )
            except Exception as e:
                log.warning(f"/ask: rank-trajectory rewrite call failed: {e}")

        # Phantom image-read guard — when NO image reached this call,
        # any "your screenshot shows / you posted a fill" claim is an
        # invented reading (2026-07-10: graded 2pale's SOXL receipt as
        # "6.1x" without ever seeing it). Detect→strip; the intake fixes
        # (reply-to-bot trigger + look-back pull) make a real image
        # reach the call in the first place, this is the backstop.
        if answer and not images:
            _phantom = _phantom_image_read_violations(answer)
            if _phantom:
                _ask_meta["guards"].append("phantom-image")
                _stripped_ph = _strip_sentences(answer, _phantom)
                if _stripped_ph.strip():
                    answer = _stripped_ph
                    log.warning(
                        f"/ask: stripped {len(_phantom)} phantom "
                        f"image-read sentence(s) (no image in call)"
                    )
                else:
                    answer = (
                        "→ Can't read a screenshot from here — repost it "
                        "as a reply to me or attach it to the question."
                    )
                    log.warning(
                        "/ask: phantom image-read strip emptied the answer "
                        "— shipped the repost ask instead"
                    )

        # Blank-answer recovery. Gemini can return an empty text payload
        # when (a) max_output_tokens was burned in the thinking phase,
        # (b) finish_reason is MAX_TOKENS / SAFETY / RECITATION /
        # MALFORMED_FUNCTION_CALL, or (c) the tool-call loop exited
        # while the model still wanted to call tools. Without this
        # branch, the @mention handler renders `discord.Embed(
        # description="")` — a literal blank message in chat. Log the
        # diagnostic, then surface a short user-facing fallback that
        # tells the asker what to do next.
        if not answer:
            finish_reason = None
            safety_blocked = False
            try:
                cand = response.candidates[0] if response else None
                if cand is not None:
                    fr = getattr(cand, "finish_reason", None)
                    finish_reason = getattr(fr, "name", None) or str(fr) if fr else None
                    sr = getattr(cand, "safety_ratings", None) or []
                    for r in sr:
                        if getattr(r, "blocked", False):
                            safety_blocked = True
                            break
            except (AttributeError, IndexError, TypeError):
                pass
            prompt_block = None
            try:
                pf = getattr(response, "prompt_feedback", None)
                br = getattr(pf, "block_reason", None) if pf else None
                prompt_block = getattr(br, "name", None) or str(br) if br else None
            except Exception:
                pass
            log.warning(
                f"/ask: empty response.text "
                f"(finish_reason={finish_reason!r}, "
                f"safety_blocked={safety_blocked}, "
                f"prompt_block={prompt_block!r}, "
                f"q={question[:140]!r})"
            )
            if safety_blocked or prompt_block:
                # With BLOCK_NONE on all configurable categories, this
                # is Gemini's unconfigurable hard filter (CSAM, severe
                # policy).
                #
                # Most common cause in production: verbatim slur tokens
                # in the prompt — either in profile **Voice.** sections
                # (which quote each user's chat verbatim, including
                # slurs they use as filler) or in recent-chat lines
                # like "BK (bankerkyle): Nigga" (filler interjections).
                #
                # Recovery: retry once with the **Voice.** sections of
                # each profile stripped out. Empirical testing (2026-
                # 06-03 19:49 UTC Ry_bry/Dovahjo AVGO trip) showed:
                #   - Full prompt          -> BLOCKED
                #   - Strip ALL profile    -> still BLOCKED (chat slurs)
                #   - Strip Voice only     -> PASSES (3/3 runs)
                # So the right surgical fix is: keep the rest of the
                # profile (Personality, Retarded takes, Recent trades,
                # Recent personal life, rationale, ranks) AND keep the
                # chat — just drop the **Voice.** subsections. Voice
                # samples are the highest-density slur container and
                # dropping them drops the prompt below the filter's
                # threshold while preserving the analytical context
                # that lets the bot still address the asker by their
                # actual profile.
                retry_succeeded = False

                # Ladder config: the ORIGINAL config minus the FUNCTION
                # tools, keeping google_search. 2026-08-07, SV's
                # "summarize the last 12 hours of chat": every tier
                # resent with the tools-bearing config, the model
                # answered each retry with a function_call (it needs
                # search_chat_messages), .text was empty, and the ladder
                # read four function calls as four blocks — shipping the
                # failure wrapper for a reason unrelated to the filter.
                # No tier executes function calls, so none may offer
                # them; the model answers from the context already in
                # the prompt (the recent chat window rides every ask).
                #
                # google_search is different in kind and must survive:
                # it resolves server-side, needs no round trip through
                # our code, and returns text rather than a function_call
                # — so it cannot cause the empty-.text failure the strip
                # exists to prevent. Nulling the whole tools list took it
                # out too, which is why every ladder tier answered
                # ungrounded (2026-08-07 COHR earnings, platinum/palladium
                # options; 2026-08-09 money-market volumes — all factual
                # questions where search IS the answer). Recovering the
                # ask but losing grounding trades one failure for another.
                try:
                    _ladder_tools = [
                        t for t in (config.tools or [])
                        if getattr(t, "google_search", None) is not None
                    ] or None
                    _ladder_config = config.model_copy(
                        update={
                            "tools": _ladder_tools,
                            # tool_config only carries
                            # include_server_side_tool_invocations, which
                            # is what surfaces google_search's grounding
                            # records. Keep it while search is offered.
                            "tool_config": (
                                config.tool_config if _ladder_tools else None
                            ),
                        }
                    )
                except Exception:
                    _ladder_config = config

                # Tier 0 — IDENTICAL retry, before any context surgery.
                # 2026-08-01: "how many members in ommi chat" (nothing
                # filterable in the question) died on every rung below;
                # replaying the exact logged prompt on 2026-08-04 passed
                # 5/5 — full prompt, bare question, profiles alone, with
                # and without the system instruction. The unconfigurable
                # filter is non-deterministic near its threshold: the
                # same content flickers between pass and block. The
                # tiers below all assume some ingredient is toxic and
                # amputate context to find it; for a flickering block
                # the cheapest correct move is to send the same thing
                # again, so a transient block costs zero context.
                if prompt_block or safety_blocked:
                    try:
                        log.warning(
                            "/ask: tier-0 retry — resending identical "
                            "prompt (filter is non-deterministic)"
                        )
                        same_resp = await client.aio.models.generate_content(
                            model=ask_model,
                            contents=contents,
                            config=_ladder_config,
                        )
                        _tally_retry_usage(same_resp)
                        try:
                            same_answer = (same_resp.text or "").strip()
                        except Exception:
                            same_answer = ""
                        if same_answer:
                            same_answer, _ = _clean_voice_violations(
                                same_answer
                            )
                            answer = same_answer
                            response = same_resp
                            retry_succeeded = True
                            _ask_meta["filter_retry"] = "same-prompt"
                            log.info(
                                "/ask: tier-0 identical retry succeeded "
                                "— block was transient, no context lost"
                            )
                            try:
                                grounding_metadata = (
                                    same_resp.candidates[0]
                                    .grounding_metadata
                                )
                            except (AttributeError, IndexError, TypeError):
                                grounding_metadata = None
                        else:
                            log.warning(
                                "/ask: tier-0 identical retry also empty "
                                "— content may genuinely trip the "
                                "filter, walking the strip ladder"
                            )
                    except Exception as e:
                        log.warning(f"/ask: tier-0 retry call failed: {e}")

                # Tier 1 — voice-strip. Operates on what was ACTUALLY
                # sent, never on the full block: a ladder rung must only
                # ever shrink the payload. Rebuilding from profiles_block
                # here would re-add the voice/racism material that
                # assembly deliberately withheld, i.e. escalate on retry.
                # When assembly already went LEAN this rung is a
                # byte-identical resend of tier 0, so skip it and let the
                # ladder move on to a genuinely different shape.
                _voice_stripped_preview = _strip_voice_sections(
                    profiles_for_prompt
                ) if profiles_for_prompt else ""
                _tier1_is_noop = (
                    _voice_stripped_preview == profiles_for_prompt
                )
                if _tier1_is_noop and profiles_for_prompt:
                    log.info(
                        "/ask: skipping voice-strip tier — assembly already "
                        "sent LEAN profiles, this rung would resend the "
                        "identical prompt"
                    )
                if (not retry_succeeded and profiles_for_prompt
                        and not _tier1_is_noop
                        and (prompt_block or safety_blocked)):
                    try:
                        voice_stripped = _voice_stripped_preview
                        stripped_sections: list[str] = [voice_stripped]
                        if fetched_urls:
                            stripped_sections.append(fetched_urls)
                        if cross_window_block:
                            stripped_sections.append(cross_window_block)
                        if chat_context:
                            stripped_sections.append(chat_context)
                        stripped_sections.append(f"{separator}\n{question}")
                        stripped_content = "\n\n".join(stripped_sections)
                        log.warning(
                            f"/ask: prompt_block={prompt_block!r}, safety_blocked="
                            f"{safety_blocked}, retrying once with Voice sections "
                            f"stripped ({len(profiles_for_prompt) - len(voice_stripped)} "
                            f"chars dropped)"
                        )
                        stripped_resp = await client.aio.models.generate_content(
                            model=ask_model,
                            contents=[
                                types.Content(
                                    role="user",
                                    parts=[types.Part.from_text(text=stripped_content)],
                                )
                            ],
                            config=_ladder_config,
                        )
                        _tally_retry_usage(stripped_resp)
                        try:
                            stripped_answer = (stripped_resp.text or "").strip()
                        except Exception:
                            stripped_answer = ""
                        if stripped_answer:
                            # Run the lint pass on the recovery answer so a
                            # rewrite-without-profiles still gets em-dash /
                            # semicolon cleanup. Meta-narration and
                            # repetition retries are NOT chained on the
                            # recovery path — recovery is an emergency
                            # fallback, simpler is safer.
                            stripped_answer, _ = _clean_voice_violations(
                                stripped_answer
                            )
                            answer = stripped_answer
                            response = stripped_resp
                            retry_succeeded = True
                            _ask_meta["filter_retry"] = "voice-strip"
                            log.info(
                                "/ask: profiles-stripped retry succeeded"
                            )
                            # Refresh grounding metadata for the new response
                            try:
                                grounding_metadata = (
                                    stripped_resp.candidates[0].grounding_metadata
                                )
                            except (AttributeError, IndexError, TypeError):
                                grounding_metadata = None
                        else:
                            log.warning(
                                "/ask: profiles-stripped retry returned empty — "
                                "attempting third-tier slur-mask retry"
                            )
                    except Exception as e:
                        log.warning(
                            f"/ask: profiles-stripped retry call failed: {e}"
                        )

                # Third-tier retry: when the Voice-strip retry ALSO came
                # back empty (or the call raised), mask slur tokens in
                # voice_stripped profile + chat + question and try one
                # more time. Lossy answer (bot can't quote the slur
                # verbatim) but answer-not-refusal.
                #
                # Concrete failure this catches (observed 2026-06-04
                # 16:57 UTC): asker asks about chat-slur usage; question
                # text + chat-context slur density trips the filter on
                # BOTH the first attempt and the Voice-strip retry. The
                # mask drops the prompt below the threshold.
                # Same shrink-only rule as tier 1: mask what was sent.
                if not retry_succeeded and profiles_for_prompt and (prompt_block or safety_blocked):
                    try:
                        voice_stripped = _strip_voice_sections(
                            profiles_for_prompt
                        )
                        masked_sections: list[str] = [
                            _mask_slur_tokens(voice_stripped)
                        ]
                        if fetched_urls:
                            masked_sections.append(fetched_urls)
                        if cross_window_block:
                            masked_sections.append(
                                _mask_slur_tokens(cross_window_block)
                            )
                        if chat_context:
                            masked_sections.append(
                                _mask_slur_tokens(chat_context)
                            )
                        masked_sections.append(
                            f"{separator}\n{_mask_slur_tokens(question)}"
                        )
                        masked_content = "\n\n".join(masked_sections)
                        # Count masked tokens for the log line so the
                        # diff vs the previous retry is visible.
                        n_masked = (
                            (len(voice_stripped or "") - len(_mask_slur_tokens(voice_stripped or "")))
                            // len("[redacted]")
                        )
                        log.warning(
                            f"/ask: third-tier retry with slur tokens masked "
                            f"(~{n_masked} tokens replaced)"
                        )
                        masked_resp = await client.aio.models.generate_content(
                            model=ask_model,
                            contents=[
                                types.Content(
                                    role="user",
                                    parts=[types.Part.from_text(text=masked_content)],
                                )
                            ],
                            config=_ladder_config,
                        )
                        _tally_retry_usage(masked_resp)
                        try:
                            masked_answer = (masked_resp.text or "").strip()
                        except Exception:
                            masked_answer = ""
                        if masked_answer:
                            masked_answer, _ = _clean_voice_violations(
                                masked_answer
                            )
                            answer = masked_answer
                            response = masked_resp
                            retry_succeeded = True
                            _ask_meta["filter_retry"] = "slur-mask"
                            log.info(
                                "/ask: slur-masked retry succeeded"
                            )
                            try:
                                grounding_metadata = (
                                    masked_resp.candidates[0].grounding_metadata
                                )
                            except (AttributeError, IndexError, TypeError):
                                grounding_metadata = None
                        else:
                            log.warning(
                                "/ask: slur-masked retry also returned empty — "
                                "attempting question-only retry"
                            )
                    except Exception as e:
                        log.warning(
                            f"/ask: slur-masked retry call failed: {e}"
                        )

                # Fourth-tier retry: QUESTION-ONLY. 2026-07-09: 2pale's
                # "wtf is prevailing wage" and "what is WRAP" died on
                # every rung above — his profile carries trip-density
                # slur content OUTSIDE the **Voice.** sections (the
                # rationale text), and the mask list doesn't cover every
                # shape. A sincere factual question must not die because
                # the asker's rap sheet is spicy: send JUST the (masked)
                # question — no profiles, no chat, no cross-window. The
                # answer loses room context, which for a factual question
                # is decoration anyway.
                if not retry_succeeded and (prompt_block or safety_blocked):
                    try:
                        bare_q = _mask_slur_tokens(
                            (question or "").strip()[-800:]
                        )
                        if bare_q.strip():
                            log.warning(
                                "/ask: fourth-tier retry — question only, "
                                "no profiles/chat"
                            )
                            bare_resp = await client.aio.models.generate_content(
                                model=ask_model,
                                contents=[types.Content(
                                    role="user",
                                    parts=[types.Part.from_text(text=bare_q)],
                                )],
                                config=_ladder_config,
                            )
                            _tally_retry_usage(bare_resp)
                            try:
                                bare_answer = (bare_resp.text or "").strip()
                            except Exception:
                                bare_answer = ""
                            if bare_answer:
                                bare_answer, _ = _clean_voice_violations(
                                    bare_answer
                                )
                                answer = bare_answer
                                response = bare_resp
                                retry_succeeded = True
                                _ask_meta["filter_retry"] = "question-only"
                                log.info(
                                    "/ask: question-only retry succeeded"
                                )
                                try:
                                    grounding_metadata = (
                                        bare_resp.candidates[0]
                                        .grounding_metadata
                                    )
                                except (AttributeError, IndexError, TypeError):
                                    grounding_metadata = None
                            else:
                                log.warning(
                                    "/ask: question-only retry also empty — "
                                    "shipping fallback wrapper"
                                )
                    except Exception as e:
                        log.warning(
                            f"/ask: question-only retry call failed: {e}"
                        )

                if not retry_succeeded:
                    _ask_meta["filter_retry"] = "failed"
                    answer = (
                        "→ Gemini bounced this one — its hard filter blocked "
                        "the prompt. Try asking a different way or about a "
                        "different subject."
                    )
            elif finish_reason in ("MAX_TOKENS", "OTHER", None):
                answer = (
                    "→ Thought myself in circles and ran out of room. "
                    "Try asking it more directly."
                )
            else:
                answer = (
                    f"→ No response came back (reason: {finish_reason}). "
                    f"Try again or rephrase."
                )

        # Strip leaked markdown-image embeds (2026-07-29): with code
        # execution the model writes `![alt](chart.png)` into its text
        # assuming inline render — but the chart posts as its OWN Discord
        # embed and the markdown shows as raw text at the top. Drop the
        # image tag, keep any alt text as a plain caption if present.
        answer = re.sub(r"!\[([^\]]*)\]\([^)]*\)",
                        lambda m: m.group(1).strip(), answer or "")
        answer = re.sub(r"\n{3,}", "\n\n", answer).strip()

        sources_footer = _build_sources_footer(grounding_metadata)
        full = (answer + sources_footer)[:4000]

        # Reconcile token budget with EVERYTHING actually spent — the
        # tool-call loop plus all retry calls (repetition, voice-strip,
        # slur-mask). Moved here 2026-06-10; previously ran before the
        # retries, leaving their usage unmeasured.
        try:
            get_budget().record_actual(
                estimated=_ask_est_total,
                actual=_ask_actual_total,
                caller=f"ask:{(question or '')[:60]}",
            )
        except Exception as e:
            log.debug(f"/ask token_budget record_actual non-fatal: {e}")

        db.record_ask_query(user_id)

        # QC log: append every interaction to /data/ask-logs/YYYY-MM-DD.md
        # so the daily publish job can push to GitHub for browseable review.
        # Failure is non-fatal — the user still gets their answer.
        try:
            # Final grounding status + source count for the audit stamp.
            try:
                _ask_meta["grounded"] = bool(
                    _grounding_has_sources(grounding_metadata)
                )
                _ask_meta["sources"] = len(
                    getattr(grounding_metadata, "grounding_chunks", None)
                    or []
                )
            except Exception:
                pass
            db.append_ask_interaction(
                asker_display_name=asker_display_name,
                asker_username=asker_username,
                channel_name=channel_name,
                question=question,
                answer=full,
                # Forensic logging: pass the FULL user_content (profiles
                # + analyst + chat context + separator + question) so
                # the log shows what Gemini actually saw, not just the
                # last 5% of the prompt. Rendered in a collapsible
                # <details> block in the markdown file.
                full_prompt=user_content,
                tool_trace=_ask_tool_trace,
                raw_answer=_raw_answer_pre_clean,
                meta=_ask_meta,
            )
        except Exception as e:
            log.warning(f"ask-log append failed (non-fatal): {e}")

        _embeds, _files = _build_ask_embeds(full, _code_images)
        return (_embeds, _files) if _files else _embeds[0]
    except Exception as e:
        # One automatic retry on TRANSIENT server-side failures (5xx /
        # timeout) before surfacing anything to the user. These resolve
        # in seconds; the caller shouldn't eat a "try again" for them.
        # The retry re-runs the whole flow (tool calls are idempotent
        # reads); a second failure falls through to normal handling.
        _err = str(e).lower()
        _is_transient = any(
            t in _err for t in ("500", "503", "internal", "timeout",
                                "unavailable", "deadline")
        )
        if _is_transient and not _transient_retry:
            log.warning(
                f"/ask transient failure ({type(e).__name__}) — retrying "
                f"once in 2s: {str(e)[:150]}"
            )
            await asyncio.sleep(2)
            return await _answer_with_gemini(
                question, user_id,
                chat_context=chat_context,
                fetched_urls=fetched_urls,
                images=images,
                profile_user_ids=profile_user_ids,
                asker_display_name=asker_display_name,
                asker_username=asker_username,
                channel_name=channel_name,
                channel_id=channel_id,
                _transient_retry=True,
            )
        log.error(f"Gemini /ask call failed: {e}", exc_info=True)
        # Log the FAILURE to the ask-log so QC sees the complete record
        # (failures were previously invisible — finding 2026-06-10).
        try:
            db.append_ask_interaction(
                asker_display_name=asker_display_name,
                asker_username=asker_username,
                channel_name=channel_name,
                question=question,
                answer=f"(failed: {type(e).__name__}: {str(e)[:200]})",
                interaction_type="failed",
            )
        except Exception:
            pass
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
        # Catch up on caller-channel messages we may have missed during
        # downtime / startup. Discord drops gateway events while
        # disconnected and doesn't replay them on reconnect, so without
        # this any trade posted during a flap would be lost forever.
        try:
            from analyst_log.watcher import run_caller_catchup
            await run_caller_catchup(bot, reason="on_ready")
        except Exception as e:
            log.error(f"Analyst catch-up on_ready failed: {e}", exc_info=True)
        # Chat-message catch-up — same shape, broader scope. Persists
        # every message from configured channels into chat_messages for
        # local SQL access. On first deploy this also bootstraps up to
        # 30 days of history per channel.
        try:
            from chat_ingestion.watcher import run_chat_catchup
            await run_chat_catchup(bot, reason="on_ready")
        except Exception as e:
            log.error(f"Chat catch-up on_ready failed: {e}", exc_info=True)

    @bot.event
    async def on_resumed():
        """Fires when the gateway successfully resumes a session after a
        brief WS drop (no full re-identify). Re-run the analyst catch-up
        because any message events fired during the disconnect window
        were lost. Rate-limited to one run per 2 min globally so a
        flap-storm doesn't trigger a scan-storm.
        """
        log.info("Discord gateway resumed — running analyst + chat catch-up")
        try:
            from analyst_log.watcher import run_caller_catchup
            await run_caller_catchup(bot, reason="on_resumed")
        except Exception as e:
            log.error(f"Analyst catch-up on_resumed failed: {e}", exc_info=True)
        try:
            from chat_ingestion.watcher import run_chat_catchup
            await run_chat_catchup(bot, reason="on_resumed")
        except Exception as e:
            log.error(f"Chat catch-up on_resumed failed: {e}", exc_info=True)

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

    @bot.tree.command(
        name="refresh_profiles",
        description="Force a user-profile refresh via the live bot (no twin-client races)",
    )
    @app_commands.describe(
        password="Admin password",
        force=(
            "When True, bypass the 20-msg delta gate and re-profile EVERY "
            "user above the 30-msg lifetime floor over the 30-day window. "
            "Use after a prompt change to refresh stale dossiers. ~$0.10-0.20 "
            "in Gemini tokens for ~50 users."
        ),
    )
    async def refresh_profiles_command(
        interaction: discord.Interaction,
        password: str,
        force: bool = False,
    ):
        """Trigger _user_profile_refresh_job inline on the live worker.

        Why this exists: running the backfill script via `railway ssh` spawns
        a SECOND discord.Client on the same bot token as this worker, which
        causes gateway-session contention and intermittent WebSocket drops
        mid-scan. Invoking the refresh through a slash command uses the
        worker's already-connected client — no second session, no race.
        Long-running (3-6 min); responds ephemerally on completion.

        `force=True` bypasses the delta gate — every user above the
        30-msg lifetime floor gets re-profiled regardless of how many
        new messages they've accumulated since their last refresh.
        Use after a prompt rewrite to force the new format across all
        existing dossiers in one shot.
        """
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            from scheduler.jobs import _user_profile_refresh_job
            log.info(
                "Manual /refresh_profiles triggered by %s (force=%s)",
                interaction.user, force,
            )
            await _user_profile_refresh_job(bot=bot, force=force)
            mode = "FORCED (all users)" if force else "delta-gated"
            await interaction.followup.send(
                f"✅ Profile refresh complete ({mode}). "
                f"Snapshot pushed to pulse-data branch.",
                ephemeral=True,
            )
        except Exception as e:
            log.error("Manual /refresh_profiles failed: %s", e, exc_info=True)
            await interaction.followup.send(
                f"Refresh failed: {str(e)[:300]}",
                ephemeral=True,
            )

    @bot.tree.command(
        name="backfill_member_trades",
        description="Extract historical member-mode trades from chat_messages into analyst_trades",
    )
    @app_commands.describe(
        password="Admin password",
        days="Window (days). Default 14 — matches the points-ledger window.",
        max_rows=(
            "Hard cap on candidate rows processed (cost guard). Default 500 "
            "is safe — each row is one Gemini OCR call ~$0.0003."
        ),
        dry_run="When true, prints what WOULD be written but doesn't insert.",
    )
    async def backfill_member_trades_command(
        interaction: discord.Interaction,
        password: str,
        days: int = 14,
        max_rows: int = 500,
        dry_run: bool = False,
    ):
        """One-shot backfill: walks chat_messages for posts in
        eager-OCR alert channels by non-callers in the last N days,
        runs extract_trade_from_caption against (content + OCR text),
        and writes analyst_trades rows with tracking_mode='member'.
        Idempotent — dedup'd on (discord_message_id, attachment_id=0)
        so re-running picks up where prior runs left off.

        Why this command exists rather than railway ssh: SSH sessions
        time out at ~10 min; the extraction loop can run 5-15 min for
        the full window. Running inline on the live worker uses the
        already-warm Gemini client and avoids the SSH timeout / key
        management entirely.
        """
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            from scripts.backfill_member_trades import run_backfill
            log.info(
                "Manual /backfill_member_trades triggered by %s (days=%d, max=%d, dry=%s)",
                interaction.user, days, max_rows, dry_run,
            )
            result = await run_backfill(
                days=days, max_rows=max_rows, dry_run=dry_run,
            )
            # Compose ephemeral summary
            counts = result["counts"]
            lines = [
                f"✅ Backfill complete ({'DRY RUN' if dry_run else 'LIVE'})",
                f"Window: last {result['days']}d",
                f"Candidates scanned: {result['candidate_count']}",
                "",
                "**Status breakdown:**",
            ]
            for status in sorted(counts.keys()):
                lines.append(f"  - `{status}`: {counts[status]}")
            if result["details"]:
                tag = "Would-write" if dry_run else "Wrote"
                lines.append("")
                lines.append(f"**{tag} (first 15):**")
                for d in result["details"][:15]:
                    lines.append(
                        f"  - {d['posted_at'][:16]} `{d.get('action') or '?'} "
                        f"{d.get('ticker') or '?'} {d.get('strike') or '?'} "
                        f"exp={d.get('expiry') or '?'} gain={d.get('gain_pct') or '?'}` "
                        f"(uid={d['author_id']})"
                    )
            body = "\n".join(lines)
            # Discord 2000-char limit on follow-up content
            if len(body) > 1900:
                body = body[:1900] + "\n…(truncated)"
            await interaction.followup.send(body, ephemeral=True)
        except Exception as e:
            log.error("Manual /backfill_member_trades failed: %s", e, exc_info=True)
            await interaction.followup.send(
                f"Backfill failed: {str(e)[:300]}",
                ephemeral=True,
            )

    @bot.tree.command(
        name="refresh_chat",
        description="Force a chat-message catch-up scan over the last 30 days (gap recovery)",
    )
    @app_commands.describe(
        password="Admin password",
        full_window=(
            "true = ignore latest-stored timestamp and scan the full "
            "30d window (use when there are gaps in stored data). "
            "false = normal resume from latest-stored - 1h buffer."
        ),
    )
    async def refresh_chat_command(
        interaction: discord.Interaction,
        password: str,
        full_window: bool = False,
    ):
        """Trigger run_chat_catchup inline on the live worker. Used to
        force-fill gaps in chat_messages that the normal on_ready /
        on_resumed catch-up missed (e.g. when an early scan failed and
        live ingestion has since moved MAX(posted_at) past the gap,
        hiding it from the resume logic). Channel-allowlisted +
        password-gated; ephemeral response since the body is admin
        diagnostics.
        """
        if not await _check_pulse_channel(interaction):
            return
        if settings.command_password and password != settings.command_password:
            await interaction.response.send_message("Invalid password.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            from chat_ingestion.watcher import run_chat_catchup
            log.info(
                "Manual /refresh_chat triggered by %s (full_window=%s)",
                interaction.user, full_window,
            )
            n_new = await run_chat_catchup(
                bot,
                reason=f"manual/{interaction.user}",
                force=True,
                force_full_window=full_window,
            )
            await interaction.followup.send(
                f"✅ Chat catch-up complete. Stored **{n_new}** new "
                f"rows" + (
                    " (full 30-day window scan)"
                    if full_window else " (resume-from-latest scan)"
                ) + ".",
                ephemeral=True,
            )
        except Exception as e:
            log.error("Manual /refresh_chat failed: %s", e, exc_info=True)
            await interaction.followup.send(
                f"Catch-up failed: {str(e)[:300]}",
                ephemeral=True,
            )

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

    @bot.tree.command(
        name="reminders",
        description="Show upcoming scheduled channel reminders",
    )
    async def reminders_command(interaction: discord.Interaction):
        if not await _check_pulse_channel(interaction):
            return
        from datetime import datetime as _dt
        import pytz as _pytz
        from reminders import calendar as _cal
        tz = _pytz.timezone(settings.timezone)
        today = _dt.now(tz).date()
        try:
            entries = _cal.load_calendar()
            up = _cal.upcoming(entries, today)
        except Exception as e:
            log.warning(f"/reminders: calendar load failed: {e}")
            up = []
        if not up:
            await interaction.response.send_message(
                "📅 No upcoming reminders on the calendar.", ephemeral=False
            )
            return
        lines = []
        for e in up[:25]:
            leads = ", ".join(
                "day-of" if l == 0 else f"{l}d"
                for l in e.get("lead_days", [])
            )
            lines.append(
                f"**{_cal._fmt_date(e['_date'])}** — {e['event']}"
                + (f"  _(lead: {leads})_" if leads else "")
            )
        embed = discord.Embed(
            title="📅 Upcoming reminders",
            description="\n".join(lines),
            color=0xF1C40F,
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)

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
                bot_client=bot,
            )
            fetched_urls = await _maybe_fetch_user_urls(question)
            # Profile lookup: ASKER + anyone the question mentions
            # by name or @-mention. Deliberately NOT including
            # chat_author_ids (everyone speaking in recent chat) —
            # that produced 10-15 profiles per call and a
            # cross-attribution risk where the model could pull
            # one user's material against another. The recent-chat
            # block still shows everyone's LITERAL MESSAGES; they
            # just don't get their dossier loaded unless they're
            # the asker or explicitly named.
            mentioned_ids = []
            try:
                mentioned_ids = db.find_users_mentioned_in_text(question)
            except Exception as e:
                log.warning(f"Name-mention lookup failed: {e}")
            # Profile auto-load scope: asker + raw <@USER_ID> mentions
            # TYPED into the slash question (parity with the @mention
            # path's first-class Discord mentions — added 2026-06-10;
            # previously slash loaded asker only). Literal-name-match
            # users still go through the lookup_user_profile tool on
            # both paths (deliberate — avoids dossier cross-loading).
            raw_mention_ids = [
                int(m) for m in re.findall(r"<@!?(\d{15,21})>", question or "")
            ]
            profile_ids = list(set(
                ([user_id] if user_id else []) + raw_mention_ids
            ))
            # Subject-verbatim (parity with @mention path): when the
            # question references OTHER users, inject their recent
            # verbatim messages so the model can quote receipts instead
            # of paraphrasing from profiles.
            try:
                _sv_ids = list(set(mentioned_ids + raw_mention_ids))
                if _sv_ids:
                    subject_verbatim = _format_subject_verbatim_block(
                        _sv_ids,
                        exclude_user_id=user_id,
                    )
                    if subject_verbatim:
                        question = f"{subject_verbatim}\n\n{question}"
            except Exception as e:
                log.warning(f"Subject-verbatim injection failed (/ask): {e}")
            asker = interaction.user
            # Resolve raw <@USER_ID> mentions in the question to readable
            # @DisplayName (username) so Gemini can connect them to the
            # WHO'S TALKING profiles (also keyed by username).
            question = await _resolve_mentions_in_text(
                bot, interaction.guild, question
            )
            _ch_id = getattr(interaction.channel, "id", None)
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
                channel_id=int(_ch_id) if _ch_id is not None else None,
            )
            _embeds, _qfiles = _normalize_ask_result(embed)
            await interaction.followup.send(
                embeds=_embeds, **({"files": _qfiles} if _qfiles else {})
            )
            _text_embed = next(
                (e for e in _embeds if e.description), None)
            # Cross-window anti-recycling: persist the bot's answer so the
            # next /ask from this asker in this channel can see what hooks
            # we already pulled — see ask_bot_answers table in db.py.
            try:
                if (user_id and _ch_id is not None and _text_embed
                        and _text_embed.description):
                    db.record_ask_bot_answer(
                        asker_user_id=int(user_id),
                        channel_id=int(_ch_id),
                        question=question,
                        answer=_text_embed.description or "",
                    )
            except Exception as e:
                log.info(
                    f"record_ask_bot_answer (/ask slash) failed (non-fatal): {e}"
                )
        except Exception as e:
            log.error(f"/ask failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.event
    async def on_message(message: discord.Message):
        # Ignore the bot's own messages + other bots.
        if message.author.bot:
            return

        # Persistent chat ingestion — store EVERY non-bot message in
        # configured channels so downstream consumers (claim verification,
        # profile refresh, analytics) can read locally instead of scanning
        # Discord history. Best-effort, never blocks downstream handlers.
        try:
            from chat_ingestion.watcher import ingest_message
            await ingest_message(message)
        except Exception as e:
            log.warning(f"Chat ingestion dispatch failed: {e}")

        # Dispatch to the analyst-log watcher.
        # - If the channel is owned by a configured analyst caller: fire
        #   in caller mode (writes the row, posts the announce embed).
        # - Else, if the channel is in `chat_eager_ocr_channels` (the
        #   shared alert rooms + the two new caller channels not yet
        #   wired through analyst_callers): fire in member mode (writes
        #   the row, NO announce, never bleeds into caller /ask context).
        # - Else: skip — the message isn't from a trade-tracking channel.
        # Runs side-by-side with @mention handling below.
        try:
            chan_name = getattr(message.channel, "name", None)
            matched_caller = (
                settings.caller_by_channel(chan_name) if chan_name else None
            )
            if matched_caller:
                from analyst_log.watcher import watch_message
                await watch_message(
                    bot, message, caller=matched_caller, tracking_mode="caller",
                )
            elif chan_name:
                eager_ocr_channels = settings.resolve_chat_eager_ocr_channels()
                if chan_name in eager_ocr_channels:
                    from analyst_log.watcher import watch_message
                    await watch_message(
                        bot, message, caller=None, tracking_mode="member",
                    )
        except Exception as e:
            log.error(f"Analyst watcher dispatch failed: {e}", exc_info=True)

        # Respond when the bot is explicitly @-mentioned OR when the
        # message is a DIRECT REPLY to one of the bot's own messages.
        # 2026-07-10: 2pale replied to the bot with an image-only SOXL
        # receipt and no ping — the mention-only trigger dropped it
        # entirely; his follow-up ping carried no image, and the bot
        # graded a screenshot it never saw ("6.1x isn't 7x"). A reply
        # to the bot IS addressed to the bot; ping on/off shouldn't
        # decide whether it gets read.
        _is_reply_to_bot = False
        if bot.user is not None and getattr(message, "reference", None):
            _ref = message.reference
            _ref_msg = getattr(_ref, "resolved", None)
            if _ref_msg is None and getattr(_ref, "message_id", None):
                # Gateway didn't resolve the parent (older message) —
                # one fetch; failure just means mention-only behavior.
                try:
                    _ref_msg = await message.channel.fetch_message(
                        _ref.message_id
                    )
                except Exception:
                    _ref_msg = None
            _ref_author = getattr(_ref_msg, "author", None)
            _is_reply_to_bot = bool(
                _ref_author
                and getattr(_ref_author, "id", None) == bot.user.id
            )
            # A reply to the bot fires EVEN when it @-tags another user
            # (user decision 2026-07-11, reversing a one-day stand-down):
            # replying to the bot's message is engaging the bot, and the
            # bot holding its own in a three-way exchange is the point.
            # The roast-recycle guard keeps repeat material from shipping
            # when it does jump in.
        if bot.user is None or (
            bot.user not in message.mentions and not _is_reply_to_bot
        ):
            await bot.process_commands(message)
            return
        # Strip the mention(s) from the content to get the actual question.
        content = message.content or ""
        for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            content = content.replace(mention, "")
        question = content.strip()
        # If the user just tagged the bot with NO question text but
        # the message is a reply or a forward, treat the referenced
        # content as the implicit subject and ask the bot to weigh in.
        # Without this, a "@bot" reply with no comment hits the
        # "Mention me with a question" wall even though there's
        # clearly something to engage with (the parent message or
        # forwarded post). The default prompt nudges the bot to
        # respond ABOUT the referenced content; the reply/forward
        # resolution later in this handler injects the actual content.
        if not question:
            has_reference = bool(getattr(message, "reference", None))
            has_snapshot = bool(getattr(message, "message_snapshots", None))
            # A bare tag/reply with an image attached IS the question —
            # "read this" (receipt, chart, screenshot).
            has_images = bool(getattr(message, "attachments", None))
            if has_reference or has_snapshot or has_images:
                question = "Weigh in on this."
            else:
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
                    bot_client=bot,
                )
                fetched_urls = await _maybe_fetch_user_urls(question)
                # Scoped profile lookup: ASKER + anyone explicitly
                # named in the question (via Discord @-mention or
                # display-name match). chat_author_ids (everyone
                # speaking in recent chat) deliberately NOT included
                # — see /ask slash command path for the rationale.
                # Reply/forward parent author gets added below.
                mentioned_ids = []
                try:
                    mentioned_ids = db.find_users_mentioned_in_text(question)
                    for u in (message.mentions or []):
                        if not u.bot and u.id != message.author.id:
                            mentioned_ids.append(u.id)
                except Exception as e:
                    log.warning(f"Name-mention lookup failed: {e}")
                # Profile auto-load scope: asker + Discord first-class
                # @-mentions + reply/forward author (the latter is added
                # below when ref_uid is resolved). Literal name matches
                # in question text or reply-parent text no longer trigger
                # profile load — those go through lookup_user_profile.
                # mentioned_ids is still used for subject-verbatim.
                profile_ids = list(set(
                    [message.author.id]
                    + [u.id for u in (message.mentions or []) if not u.bot]
                ))

                # Inject the asker's recent verbatim messages from
                # chat_messages so the model has the actual receipts
                # available if the question references them or if the
                # asker challenges a prior claim with "show me where I
                # said that." Cheap (~50 msgs × ~80 chars = 4 KB) and
                # closes the gaslight loop end-to-end — without this
                # block the model has to rely on the asker's profile
                # (which summarizes but doesn't quote) and the recent
                # chat window (which may not include older receipts).
                # Asker-verbatim block disabled — recent channel chat
                # (50 msgs, 24h) + the asker's profile in WHO'S TALKING
                # already give the model plenty of context about the
                # asker. The verbatim helper (_format_asker_verbatim_block)
                # is kept in the file as dead code in case we want to
                # re-enable for specific rebuttal patterns later.

                # Mention-aware verbatim: when the question references
                # OTHER users (Discord @-mentions or known display-name
                # mentions), inject their recent chat too. Same shape
                # as the asker block, narrower window. Skips the asker
                # since they're already covered.
                try:
                    if mentioned_ids:
                        subject_verbatim = _format_subject_verbatim_block(
                            mentioned_ids,
                            exclude_user_id=message.author.id,
                        )
                        if subject_verbatim:
                            question = f"{subject_verbatim}\n\n{question}"
                except Exception as e:
                    log.warning(f"Subject-verbatim injection failed: {e}")

                # Resolve any referenced message (reply parent or
                # forward snapshot). Discord forwards carry the snapshot
                # content INLINE — not in message.content — so without
                # this the bot would only see the asker's caption ("make
                # him feel better") and miss the actual forwarded post.
                (
                    ref_content,
                    ref_uid,
                    ref_display,
                    ref_attachments,
                ) = await _resolve_referenced_message(bot, message)

                # Add the original author to profile context so the bot
                # has their personality dossier (e.g. forwarding someone
                # crying → bot can address them by name + voice).
                if ref_uid and ref_uid != message.author.id:
                    profile_ids = list(set(profile_ids + [ref_uid]))

                # Subject-detection from reply-parent content.
                # When the asker replies to a previous message that names
                # someone, name-mentions in the parent's text still inform
                # mentioned_ids (for subject-verbatim quoting). Under the
                # narrowed-scope design (2026-06-01), reply-parent name
                # matches no longer auto-load profiles — that goes through
                # lookup_user_profile tool calls. The reply-parent's AUTHOR
                # still triggers profile load below (ref_uid handling).
                if ref_content:
                    try:
                        ref_mentioned = db.find_users_mentioned_in_text(
                            ref_content
                        )
                        for uid in ref_mentioned:
                            if uid != message.author.id and (
                                bot.user is None or uid != bot.user.id
                            ):
                                if uid not in mentioned_ids:
                                    mentioned_ids.append(uid)
                    except Exception as e:
                        log.warning(
                            f"Reply-parent name-mention lookup failed: {e}"
                        )

                # Prepend the referenced content to the question so
                # Gemini sees the explicit framing — what was said by
                # whom, and what the asker is asking about it.
                if ref_content:
                    is_forward = bool(
                        getattr(message, "message_snapshots", None)
                    )
                    label = (
                        "FORWARDED MESSAGE"
                        if is_forward
                        else "MESSAGE BEING REPLIED TO"
                    )
                    author_tag = ref_display or "(author not resolved)"
                    if ref_uid:
                        author_tag += f" — user_id {ref_uid}"
                    question = (
                        f"[{label} — from {author_tag}]\n"
                        f'"{ref_content}"\n\n'
                        f"[{(getattr(message.author, 'display_name', None) or message.author.name)}'s message to you]\n"
                        f"{question}"
                    )

                # Scoped image collection: the @mention message + the
                # referenced (reply parent OR forward snapshot) message.
                # Cap at _IMAGE_MAX_PER_CALL total.
                images = await _extract_images_from_message(
                    message,
                    remaining_slots=_IMAGE_MAX_PER_CALL,
                )
                # Pull images from the referenced message's attachments
                # using the same byte-fetch path. Works for both replies
                # and forwards (snapshot.attachments are real Attachment
                # objects with .read()).
                _image_source_msg = message if images else None
                if ref_attachments and len(images) < _IMAGE_MAX_PER_CALL:
                    remaining = _IMAGE_MAX_PER_CALL - len(images)
                    for att in ref_attachments:
                        if remaining <= 0:
                            break
                        ct = (getattr(att, "content_type", None) or "").lower()
                        is_pdf = ct.startswith("application/pdf")
                        if not (ct.startswith("image/") or is_pdf):
                            continue
                        cap = _PDF_MAX_BYTES if is_pdf else _IMAGE_MAX_BYTES
                        if getattr(att, "size", 0) and att.size > cap:
                            continue
                        try:
                            data = await att.read()
                            images.append((data, ct))
                            remaining -= 1
                        except Exception as e:
                            log.info(f"/ask referenced-msg attachment read failed: {e}")

                # Look-back image fallback (2026-07-10): the room's
                # pattern is screenshot first, ask second ("Weigh in on
                # this." 9 seconds after the image, as its own message).
                # If neither the ask nor the referenced message carried
                # an image, scan the asker's OWN last few messages (5-min
                # window) for one so the bot reads the actual receipt
                # instead of grading a screenshot it never saw.
                if not images:
                    try:
                        async for _prev in message.channel.history(
                            limit=8, before=message
                        ):
                            if _prev.author.id != message.author.id:
                                continue
                            _age = (
                                message.created_at - _prev.created_at
                            ).total_seconds()
                            if _age > 300:
                                break
                            if not _prev.attachments:
                                continue
                            _lb = await _extract_images_from_message(
                                _prev, remaining_slots=_IMAGE_MAX_PER_CALL,
                            )
                            if _lb:
                                images = _lb
                                _image_source_msg = _prev
                                log.info(
                                    f"/ask: look-back image pulled from the "
                                    f"asker's message {_prev.id} "
                                    f"({int(_age)}s before the ask)"
                                )
                                break
                    except Exception as e:
                        log.info(f"/ask look-back image scan failed: {e}")

                # Receipt → ledger (2026-07-10): a screenshot handed
                # directly to the bot (reply / tag / look-back) is an
                # entry-or-exit receipt candidate. Route the image-
                # bearing message through the member-mode analyst
                # watcher — OCR → trade extraction → ledger row, no
                # announce; a non-trade image is a silent no-op and the
                # watcher is idempotent by message id. Eager-OCR/caller
                # channels already dispatch in on_message, so only cover
                # the rest.
                if images and _image_source_msg is not None:
                    try:
                        _rcpt_chan = getattr(message.channel, "name", "") or ""
                        _covered = (
                            _rcpt_chan
                            in settings.resolve_chat_eager_ocr_channels()
                            or bool(settings.caller_by_channel(_rcpt_chan))
                        )
                        if not _covered:
                            from analyst_log.watcher import (
                                watch_message as _receipt_watch,
                            )
                            asyncio.create_task(
                                _receipt_watch(
                                    bot, _image_source_msg,
                                    caller=None, tracking_mode="member",
                                ),
                                name=(
                                    f"ask_receipt_ledger_"
                                    f"{_image_source_msg.id}"
                                ),
                            )
                            log.info(
                                f"/ask: receipt-candidate image dispatched "
                                f"to member-mode ledger extraction "
                                f"(msg={_image_source_msg.id})"
                            )
                    except Exception as e:
                        log.info(f"/ask receipt ledger dispatch failed: {e}")

                # Resolve raw <@USER_ID> mentions in the question text
                # so Gemini can connect tagged users to WHO'S TALKING.
                question = await _resolve_mentions_in_text(
                    bot, message.guild, question
                )
                _ch_id = getattr(message.channel, "id", None)
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
                    channel_id=int(_ch_id) if _ch_id is not None else None,
                )
                _embeds, _qfiles = _normalize_ask_result(embed)
                await message.reply(
                    embeds=_embeds, mention_author=False,
                    **({"files": _qfiles} if _qfiles else {})
                )
                _text_embed = next(
                    (e for e in _embeds if e.description), None)
                # Cross-window anti-recycling: persist the bot's answer.
                try:
                    if (message.author.id and _ch_id is not None
                            and _text_embed and _text_embed.description):
                        db.record_ask_bot_answer(
                            asker_user_id=int(message.author.id),
                            channel_id=int(_ch_id),
                            question=question,
                            answer=_text_embed.description or "",
                        )
                except Exception as e:
                    log.info(
                        f"record_ask_bot_answer (@mention) failed "
                        f"(non-fatal): {e}"
                    )
        except Exception as e:
            log.error(f"@mention /ask failed: {e}", exc_info=True)
            try:
                # Room-voice failure line (2026-07-15: a double-500 shipped
                # "(failed: ServerError: 500 INTERNAL {'error': ...})" as
                # chat text). The raw error stays in logs + the ask-log
                # 'failed' entry; the room gets a human line.
                await message.reply(
                    "→ Gemini choked on that one mid-answer. Run it back "
                    "in a minute.",
                    mention_author=False,
                )
            except Exception:
                pass
        await bot.process_commands(message)

    return bot
