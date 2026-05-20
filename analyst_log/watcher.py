"""Discord event handler that turns analyst-channel posts into trade-log
rows. Called from `discord_bot.bot.on_message` when the message channel
matches `settings.analyst_channel_name`.

Flow per message:
  1. Skip if no image attachments.
  2. For each image attachment, dedup by (message_id, attachment_id).
  3. Download the image, OCR via `analyst_log.ocr.extract_trade_from_image`.
  4. Write a row to `analyst_trades` (whether trade or not — non-trades
     get is_trade=0 so we don't re-OCR them on bot restart).
  5. For actual trades, post a one-line summary to the configured
     announce channel (`settings.analyst_test_announce_channel`).
"""

import logging
from typing import Any

import discord

import db
from analyst_log.ocr import extract_trade_from_caption, extract_trade_from_image
from config import settings

log = logging.getLogger(__name__)


async def _fetch_reply_parent_caption(
    message: discord.Message,
) -> str | None:
    """If `message` is a Discord reply, fetch the parent message and
    return its content (caption). Returns None if not a reply, if
    fetch fails, or the parent has no content.

    Used to enrich caption-only and image extractions with chain
    context — e.g. a reply 'closed' on an earlier 'BTO MSFT 430C 5/20
    @3.65' post resolves to a close of that same contract.
    """
    ref = getattr(message, "reference", None)
    if not ref:
        return None
    parent_id = getattr(ref, "message_id", None)
    if not parent_id:
        return None
    # Prefer the cached resolved message if discord.py already has it
    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return (resolved.content or "").strip() or None
    # Otherwise fetch from the channel
    try:
        parent = await message.channel.fetch_message(parent_id)
    except Exception as e:
        log.debug(f"Analyst log: reply-parent fetch failed for {parent_id}: {e}")
        return None
    return (parent.content or "").strip() or None


def _is_extraction_actionable(extracted: dict) -> bool:
    """Return True only if the extracted JSON has the minimum-viable
    fields for a real trade row. Guards against model-sentinel garbage
    (strike=0 from 'couldn't extract', missing ticker, etc.) so we
    don't write junk rows to analyst_trades that later leak into
    /ask position lookups.

    Rules:
    - is_trade_screenshot must be True
    - ticker must be a non-empty string
    - For call/put contracts: strike must be present AND non-zero
    - (stock contracts can have null strike — but we don't OCR stock
      tickets typically; tolerated for future flexibility)
    """
    if not extracted.get("is_trade_screenshot"):
        return False
    ticker = (extracted.get("ticker") or "").strip()
    if not ticker:
        return False
    ctype = (extracted.get("contract_type") or "").lower()
    if ctype in ("call", "put"):
        strike = extracted.get("strike")
        try:
            strike_f = float(strike) if strike is not None else None
        except (TypeError, ValueError):
            return False
        if not strike_f or strike_f == 0:
            return False
    return True


def _lookup_open_price(
    caller: str | None,
    ticker: str,
    contract_type: str | None,
    strike: float | None,
    expiry: str,
) -> float | None:
    """Look up the earliest recorded open/add price for the matching
    contract under this caller. Used to derive close-side price or
    gain% when one side is missing on a CLOSE/TRIM screenshot.
    Returns None if no open price is on file (caller has the contract
    in unknown-cost-basis state — caller may have opened off-channel).
    """
    if not ticker or not expiry:
        return None
    params: list[Any] = [ticker.upper(), expiry]
    sql = (
        "SELECT price FROM analyst_trades "
        "WHERE is_trade = 1 "
        "  AND UPPER(ticker) = ? "
        "  AND expiry = ? "
        "  AND action IN ('open', 'add') "
        "  AND price IS NOT NULL "
        "  AND price != 0"
    )
    if contract_type:
        sql += " AND LOWER(COALESCE(contract_type,'')) = ?"
        params.append(contract_type.strip().lower())
    if strike is not None:
        try:
            sql += " AND COALESCE(strike,-1) = ?"
            params.append(float(strike))
        except (TypeError, ValueError):
            return None
    if caller:
        sql += " AND LOWER(COALESCE(caller,'')) = ?"
        params.append(caller.strip().lower())
    sql += " ORDER BY posted_at ASC LIMIT 1"
    try:
        row = db.get_connection().execute(sql, tuple(params)).fetchone()
    except Exception as e:
        log.debug(f"_lookup_open_price query failed: {e}")
        return None
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def _derive_close_metrics(extracted: dict, caller_name: str | None) -> None:
    """On a CLOSE/TRIM extraction with exactly one of {price, gain_pct}
    missing, derive the missing value from the open-side price.

      close_price = open_price * (1 + gain_pct/100)
      gain_pct    = (close_price - open_price) / open_price * 100

    If both fields are missing OR both are present, no-op. If the open
    price isn't on file, no-op (no anchor to derive from). Mutates the
    extracted dict in place; tags `price_source` / `gain_source` on the
    blob so the forensic JSON shows the value was computed, not OCRed.
    """
    action = (extracted.get("action") or "").lower()
    if action not in ("close", "trim"):
        return

    try:
        p = float(extracted.get("price")) if extracted.get("price") is not None else None
    except (TypeError, ValueError):
        p = None
    has_price = bool(p and p != 0)

    try:
        g = float(extracted.get("gain_pct")) if extracted.get("gain_pct") is not None else None
    except (TypeError, ValueError):
        g = None
    has_gain = g is not None  # 0% is a valid round-trip

    # Both present or both missing → nothing to derive
    if has_price == has_gain:
        return

    open_price = _lookup_open_price(
        caller_name,
        (extracted.get("ticker") or "").strip(),
        extracted.get("contract_type"),
        extracted.get("strike"),
        (extracted.get("expiry") or "").strip(),
    )
    if not open_price:
        return

    if has_gain and not has_price:
        extracted["price"] = round(open_price * (1 + g / 100.0), 2)
        extracted["price_source"] = "derived_from_gain_and_open"
    elif has_price and not has_gain:
        extracted["gain_pct"] = round(((p - open_price) / open_price) * 100.0, 2)
        extracted["gain_source"] = "derived_from_price_and_open"


async def watch_message(
    bot: discord.Client,
    message: discord.Message,
    caller: dict | None = None,
) -> None:
    """Process an analyst-channel message. Side-effect-only — writes to DB
    and posts to the announce channel. All failures are logged and
    swallowed so a single bad image never blocks subsequent processing.

    `caller` is the matched caller dict from settings.resolve_analyst_callers():
    {name, display, username, channel, enabled}. When provided, the
    watcher uses caller['username'] for the author filter and writes
    caller['name'] as the canonical `caller` field on each row.

    Legacy callers passing only (bot, message) fall back to the global
    `analyst_primary_author` setting for filtering, and the stored
    `caller` field is None — backwards-compatible with the pre-registry
    deployment but loses hard-separation in /ask context.
    """
    # Resolve the username to filter by + the canonical caller name to
    # store. Registry-driven (preferred) or legacy fallback.
    if caller:
        expected_username = caller.get("username", "").strip().lower()
        canonical_caller = caller.get("name", "").strip().lower() or None
        caller_display = caller.get("display") or canonical_caller or "the caller"
    else:
        expected_username = (settings.analyst_primary_author or "").strip().lower()
        canonical_caller = None
        caller_display = "the caller"

    if expected_username:
        author_username = (message.author.name or "").lower()
        if author_username != expected_username:
            log.debug(
                f"Analyst log: skipping non-matching author '{author_username}' "
                f"in {message.channel.name} (expected='{expected_username}', "
                f"caller={canonical_caller})"
            )
            return

    caption = (message.content or "").strip()
    posted_at = message.created_at.isoformat()
    author_name = (
        getattr(message.author, "display_name", None) or message.author.name
    )

    # Reply-chain context: if this message is a Discord reply, fetch
    # the parent's caption so terse follow-ups like "closed" or "sold
    # @19" can be resolved against the position the parent established.
    parent_caption = await _fetch_reply_parent_caption(message)

    # Caption-only path: a message with NO image attachments but a
    # non-empty caption goes through text-only extraction. The caption
    # must contain ticker + strike (expiry defaults to today/0DTE) —
    # otherwise the extraction path tags is_trade=false and we skip
    # recording. Reply-chain context (parent_caption) is passed through
    # so 'closed' style follow-ups resolve correctly.
    if not message.attachments:
        if not caption:
            return
        # Dedup: use a synthetic attachment_id=0 so the UNIQUE(message_id,
        # attachment_id) constraint still works for caption-only rows.
        synthetic_att_id = 0
        if db.analyst_trade_exists(message.id, synthetic_att_id):
            return
        extracted = await extract_trade_from_caption(
            caption, parent_caption=parent_caption,
            caller_name=caller_display,
        )
        if extracted is None:
            log.warning(
                f"Analyst log: caption extraction returned None — "
                f"msg={message.id} caption={caption[:120]!r}"
            )
            return
        # Storage guardrail: downgrade junk extractions to is_trade=false
        # so we don't write garbage rows that pollute /ask context.
        if extracted.get("is_trade_screenshot") and not _is_extraction_actionable(extracted):
            log.info(
                f"Analyst log: junk caption extraction rejected — "
                f"msg={message.id} extracted={extracted}"
            )
            extracted["is_trade_screenshot"] = False
            extracted["what_it_appears_to_be"] = (
                "extraction failed integrity check (missing ticker or strike=0)"
            )
        is_trade = bool(extracted.get("is_trade_screenshot"))
        if is_trade:
            _derive_close_metrics(extracted, canonical_caller)
        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=synthetic_att_id,
                author=author_name,
                posted_at=posted_at,
                image_url=None,
                caption=caption,
                is_trade=is_trade,
                gemini_json=extracted,
                ticker=extracted.get("ticker") if is_trade else None,
                contract_type=extracted.get("contract_type") if is_trade else None,
                strike=extracted.get("strike") if is_trade else None,
                expiry=extracted.get("expiry") if is_trade else None,
                action=extracted.get("action") if is_trade else None,
                gain_pct=extracted.get("gain_pct") if is_trade else None,
                price=extracted.get("price") if is_trade else None,
                caller=canonical_caller,
            )
        except Exception as e:
            log.error(f"Analyst log: caption-only DB insert failed: {e}", exc_info=True)
            return
        if is_trade:
            await _announce_to_channel(bot, message, extracted, author_name, caller=caller)
        return

    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if not ct.startswith("image/"):
            continue

        if db.analyst_trade_exists(message.id, att.id):
            log.debug(
                f"Analyst log: skipping already-logged image — "
                f"msg={message.id} att={att.id}"
            )
            continue

        try:
            img_bytes = await att.read()
        except Exception as e:
            log.error(f"Analyst log: failed to read attachment {att.url}: {e}")
            continue

        extracted = await extract_trade_from_image(
            img_bytes, ct, caption, parent_caption=parent_caption,
            caller_name=caller_display,
        )
        # Storage guardrail: same junk-extraction filter as caption-only.
        if extracted and extracted.get("is_trade_screenshot") and not _is_extraction_actionable(extracted):
            log.info(
                f"Analyst log: junk image extraction rejected — "
                f"msg={message.id} att={att.id} extracted={extracted}"
            )
            extracted["is_trade_screenshot"] = False
            extracted["what_it_appears_to_be"] = (
                "extraction failed integrity check (missing ticker or strike=0)"
            )
        if extracted is None:
            # OCR failed entirely — don't insert a row, so we'll retry on
            # the next bot restart. (Rare; usually means Gemini call errored.)
            log.warning(
                f"Analyst log: extraction returned None for {att.url} — "
                f"will retry on next restart"
            )
            continue

        is_trade = bool(extracted.get("is_trade_screenshot"))
        if is_trade:
            _derive_close_metrics(extracted, canonical_caller)

        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=att.id,
                author=author_name,
                posted_at=posted_at,
                image_url=att.url,
                caption=caption,
                is_trade=is_trade,
                gemini_json=extracted,
                ticker=extracted.get("ticker") if is_trade else None,
                contract_type=extracted.get("contract_type") if is_trade else None,
                strike=extracted.get("strike") if is_trade else None,
                expiry=extracted.get("expiry") if is_trade else None,
                action=extracted.get("action") if is_trade else None,
                gain_pct=extracted.get("gain_pct") if is_trade else None,
                price=extracted.get("price") if is_trade else None,
                caller=canonical_caller,
            )
        except Exception as e:
            log.error(f"Analyst log: DB insert failed: {e}", exc_info=True)
            continue

        if is_trade:
            await _announce_to_channel(bot, message, extracted, author_name, caller=caller)


async def _announce_to_channel(
    bot: discord.Client,
    source_msg: discord.Message,
    extracted: dict[str, Any],
    author_name: str,
    caller: dict | None = None,
) -> None:
    """Post a one-line summary of the logged trade to the configured
    announce channel as an embed. The source channel name is the
    clickable link back to the original alert.
    Skips silently if the channel can't be found.

    Per-caller announce override: if `caller["announce_channel"]` is
    set, post there instead of the global `analyst_test_announce_channel`.
    Used by callers like f.jamal who announce back to their own channel.
    """
    per_caller_chan = (caller or {}).get("announce_channel") if caller else None
    chan_name = (
        per_caller_chan or settings.analyst_test_announce_channel or ""
    ).strip()
    if not chan_name:
        return

    target: discord.TextChannel | None = None
    for guild in bot.guilds:
        for ch in guild.text_channels:
            if ch.name.lower() == chan_name.lower():
                target = ch
                break
        if target:
            break

    if target is None:
        log.warning(
            f"Analyst log: announce channel '{chan_name}' not found in any guild"
        )
        return

    description = _format_announce_line(extracted, source_msg, author_name)
    embed = discord.Embed(description=description)
    try:
        await target.send(embed=embed)
    except Exception as e:
        log.error(f"Analyst log: announce failed in #{chan_name}: {e}")


def _format_announce_line(
    extracted: dict[str, Any],
    source_msg: discord.Message,
    author_name: str,
) -> str:
    """Render the announce line for the embed description. Format:

    📝 Logged: [**#🥷🏽-abe-alerts-🥷🏽**](jump_url) CLOSE **NOW 95C 5/29** (+79.6%) — "I'm out!"

    The bolded channel name links to the source alert message. Embed
    rendering means markdown links work and the URL doesn't show as
    visible text. `author_name` is kept in the signature for backwards
    compatibility but is no longer displayed in the output (the source
    channel is the clearer attribution).
    """
    ticker = extracted.get("ticker") or "?"
    strike = extracted.get("strike")
    contract_type = (extracted.get("contract_type") or "").lower()
    expiry = extracted.get("expiry") or ""
    action = (extracted.get("action") or "?").upper()
    gain_pct = extracted.get("gain_pct")
    price = extracted.get("price")
    screenshot_type = extracted.get("screenshot_type") or ""
    caption = (source_msg.content or "").strip()

    type_suffix = {"call": "C", "put": "P"}.get(contract_type, "")
    strike_str = (
        f"{int(strike) if strike == int(strike) else strike}" if strike else "?"
    )
    expiry_short = expiry[5:] if len(expiry) >= 10 else expiry  # MM-DD slice
    contract_str = f"{ticker} {strike_str}{type_suffix} {expiry_short}".strip()

    # Channel name with markdown link to the source message. Works inside
    # an embed description.
    channel_name = source_msg.channel.name if source_msg.channel else "source"
    jump_url = getattr(source_msg, "jump_url", "")
    if jump_url:
        channel_link = f"[**#{channel_name}**]({jump_url})"
    else:
        channel_link = f"**#{channel_name}**"

    line = f"📝 Logged: {channel_link} {action} **{contract_str}**"
    # Display rule: show price (cost) whenever it was extractable —
    # entry price on opens, exit price on closes. Gain% is appended
    # on exits (CLOSE/TRIM) when present. Both can render on the same
    # line so closes carry both the sold-at price and the realized %.
    # 0-values are treated as missing (model sentinel for "couldn't
    # extract", not an actual data point).
    is_exit_event = action in ("CLOSE", "TRIM")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        gain_f = float(gain_pct) if gain_pct is not None else None
    except (TypeError, ValueError):
        gain_f = None
    if price_f and price_f != 0:
        line += f" @{price_f:.2f}"
    if is_exit_event and gain_f is not None and gain_f != 0:
        line += f" ({gain_f:+.1f}%)"
    if screenshot_type == "stats_screen" and action == "VIEWING":
        line += " _(stats screen)_"
    if caption:
        # Trim long captions, escape Discord markdown noise
        cap_safe = caption.replace("`", "'").replace("\n", " ")[:80]
        line += f' — "{cap_safe}"'
    # (Previously appended a trailing <jump_url> here; replaced by the
    # markdown link on the channel-name segment above.)
    return line[:1900]  # Discord 2000-char hard limit, leave buffer
