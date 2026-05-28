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
from datetime import datetime, timedelta, timezone
from typing import Any

import discord

import db
from analyst_log.ocr import extract_trade_from_caption, extract_trade_from_image
from config import settings

log = logging.getLogger(__name__)


# State for catch-up rate-limiting. on_resumed can fire rapidly during
# gateway flapping; without this we'd run the scan over and over.
_CATCHUP_MIN_INTERVAL_SEC = 120  # don't re-run within 2 minutes
_CATCHUP_HARD_CAP_HOURS = 24     # never scan more than 24h back per channel
_CATCHUP_BUFFER_MIN = 60         # rewind 1h before latest-known-msg as safety margin
_last_catchup_at: dict[str, datetime] = {}


async def run_caller_catchup(
    bot: discord.Client,
    *,
    reason: str = "startup",
    force: bool = False,
) -> int:
    """Scan each configured caller's channel for messages we may have
    missed during a downtime / gateway-flapping window. Routes each
    eligible message through watch_message — the existing dedup
    (UNIQUE(message_id, attachment_id) + analyst_trade_exists check)
    makes re-processing already-logged messages a silent no-op.

    Triggered from on_ready and on_resumed in bot.py. The on_resumed
    case is the important one — when the gateway invalidates and
    re-identifies, message events fired during the disconnect window
    are LOST (Discord does not replay them). Without this catch-up, any
    BK/Abe trade screenshot posted during a flap is gone forever from
    the log.

    Scan window per channel: from
      max(latest_known_posted_at - 1h buffer, now - 24h hard cap)
    to now. So a chatty caller with messages 5 min ago triggers a ~1h
    scan; a quiet caller with no recent activity caps at 24h.

    Rate-limited to one run per 2 minutes globally to avoid hammering
    Discord when the gateway is flapping rapidly. Pass force=True to
    skip the rate limit (used by /reanalyze-style manual triggers).

    Returns the count of NEW messages logged across all callers (i.e.
    messages that weren't already in the DB before this run).
    """
    global _last_catchup_at
    now = datetime.now(timezone.utc)
    if not force:
        last = _last_catchup_at.get("global")
        if last and (now - last).total_seconds() < _CATCHUP_MIN_INTERVAL_SEC:
            log.debug(
                f"Analyst catch-up skipped — ran "
                f"{(now - last).total_seconds():.0f}s ago"
            )
            return 0
    _last_catchup_at["global"] = now

    callers = settings.resolve_analyst_callers()
    if not callers:
        return 0

    total_new = 0
    for caller in callers:
        chan_name = caller.get("channel")
        if not chan_name:
            continue
        target = None
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name == chan_name:
                    target = ch
                    break
            if target:
                break
        if target is None:
            log.debug(
                f"Catch-up: channel '{chan_name}' for {caller['name']} "
                f"not found in any guild"
            )
            continue

        # Determine the start time for this caller's scan
        latest_iso = db.get_latest_analyst_trade_posted_at(caller["name"])
        if latest_iso:
            try:
                # Normalize ISO timestamp — DB stores both T-separated and
                # space-separated, with or without UTC offset
                norm = latest_iso.replace(" ", "T")
                if norm.endswith("Z"):
                    norm = norm[:-1] + "+00:00"
                latest_dt = datetime.fromisoformat(norm)
                if latest_dt.tzinfo is None:
                    latest_dt = latest_dt.replace(tzinfo=timezone.utc)
            except Exception as e:
                log.warning(f"Catch-up: bad timestamp {latest_iso!r}: {e}")
                latest_dt = None
        else:
            latest_dt = None

        hard_floor = now - timedelta(hours=_CATCHUP_HARD_CAP_HOURS)
        if latest_dt:
            scan_from = max(
                latest_dt - timedelta(minutes=_CATCHUP_BUFFER_MIN),
                hard_floor,
            )
        else:
            scan_from = hard_floor

        expected_username = (caller.get("username") or "").lower()
        n_seen = 0
        n_processed = 0
        try:
            async for msg in target.history(
                limit=None, after=scan_from, oldest_first=True
            ):
                n_seen += 1
                if msg.author.bot:
                    continue
                if expected_username and msg.author.name.lower() != expected_username:
                    continue
                # Before-watch-message dedup check so we can count "new"
                # messages accurately (the watcher itself silently skips
                # via the same check, but we don't get a return signal).
                synthetic_id = 0 if not msg.attachments else None
                # Crude: if ANY attachment id is new, count as new. For
                # caption-only msgs the synthetic_id=0 path is used.
                is_new = False
                if msg.attachments:
                    for att in msg.attachments:
                        if not db.analyst_trade_exists(msg.id, att.id):
                            is_new = True
                            break
                else:
                    is_new = not db.analyst_trade_exists(msg.id, 0)
                if is_new:
                    n_processed += 1
                await watch_message(bot, msg, caller=caller)
            if n_processed:
                log.info(
                    f"Analyst catch-up ({reason}): #{chan_name} — "
                    f"scanned {n_seen}, logged {n_processed} new "
                    f"messages for {caller['name']} "
                    f"(from {scan_from.isoformat()})"
                )
                total_new += n_processed
            else:
                log.debug(
                    f"Analyst catch-up ({reason}): #{chan_name} — "
                    f"scanned {n_seen}, nothing new for {caller['name']}"
                )
        except Exception as e:
            log.warning(
                f"Analyst catch-up ({reason}) failed for {caller['name']} "
                f"in #{chan_name}: {e}",
                exc_info=True,
            )
    return total_new


async def _fetch_reply_parent_caption(
    message: discord.Message,
) -> str | None:
    """If `message` is a Discord reply, return the best-available context
    string describing the parent message.

    The function PREFERS the parent's stored analyst_trades extraction
    (ticker / strike / expiry / action / price) when one exists in our
    DB — that's the structured data the parent's image was OCR'd into,
    and it's far more useful for resolving a sparse follow-up like
    "closed" or "sold @0.41" than the raw text caption alone.

    Why this matters: a caller often opens with `OPEN HOOD 80C 5/22` in
    a screenshot whose TEXT CAPTION says something colorful like "Bought
    these. Thesis is Bitcoin shall climb." The expiry (5/22) lives in
    the IMAGE — not the text. When the caller replies "Closed HOOD 80c
    @0.41" hours later, fetching only the parent's text caption gives
    us "Bought these. Thesis is..." — useless for inferring the expiry.
    Fetching the parent's stored trade row gives us "open HOOD 80C
    2026-05-22 @0.25" — exactly what the OCR needs to anchor the close.

    Fallback chain:
      1. analyst_trades row for parent.discord_message_id (preferred)
      2. cached parent via message.reference.resolved
      3. live fetch_message(parent_id) for the text caption

    Returns None when the message isn't a reply or no context can be
    retrieved through any route.
    """
    ref = getattr(message, "reference", None)
    if not ref:
        return None
    parent_id = getattr(ref, "message_id", None)
    if not parent_id:
        return None

    # 1. PREFERRED: structured parent-trade context from our own DB
    try:
        parent_trade = db.get_analyst_trade_by_message_id(parent_id)
    except Exception as e:
        log.debug(f"Analyst log: parent-trade DB lookup failed for {parent_id}: {e}")
        parent_trade = None
    if parent_trade and parent_trade.get("is_trade"):
        ctype = (parent_trade.get("contract_type") or "").lower()
        ct_suffix = {"call": "C", "put": "P"}.get(ctype, "")
        strike = parent_trade.get("strike")
        try:
            strike_str = (
                f"{int(strike) if float(strike) == int(float(strike)) else strike}"
                if strike is not None else "?"
            )
        except (TypeError, ValueError):
            strike_str = "?"
        ticker = parent_trade.get("ticker") or "?"
        expiry = parent_trade.get("expiry") or "?"
        action = (parent_trade.get("action") or "?").upper()
        price = parent_trade.get("price")
        spec_parts = [
            f"{action} {ticker} {strike_str}{ct_suffix} {expiry}"
        ]
        if price:
            try:
                spec_parts.append(f"@{float(price):.2f}")
            except (TypeError, ValueError):
                pass
        spec = " ".join(spec_parts)
        # Append the parent's original caption text if any, for color/voice
        original_caption = (parent_trade.get("caption") or "").strip()
        if original_caption:
            return f"{spec} — caller's words: {original_caption[:200]!r}"
        return spec

    # 2. Cached resolved message (no DB row, but discord.py has it in cache)
    resolved = getattr(ref, "resolved", None)
    if isinstance(resolved, discord.Message):
        return (resolved.content or "").strip() or None

    # 3. Last-resort live fetch
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
    - action must be one of the four real trade actions (open/add/trim/
      close). "viewing" / "unclear" / anything else is observation noise
      and gets downgraded — those rows had been polluting /ask's
      RECENT TRADES block (see 2026-05-28 QC: action='viewing' on a 🍆
      caption against an MSFT stats screen).
    - ticker must be a non-empty string
    - For call/put contracts: strike must be present AND non-zero
    - (stock contracts can have null strike — but we don't OCR stock
      tickets typically; tolerated for future flexibility)
    """
    if not extracted.get("is_trade_screenshot"):
        return False
    action = (extracted.get("action") or "").strip().lower()
    if action not in ("open", "add", "trim", "close"):
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
    *,
    author_id: int | None = None,
    tracking_mode: str | None = "caller",
) -> float | None:
    """Look up the earliest recorded open/add price for the matching
    contract under this caller (or author_id in member mode). Used to
    derive close-side price or gain% when one side is missing on a
    CLOSE/TRIM screenshot. Returns None if no open price is on file.

    tracking_mode defaults to 'caller' so caller closes never inherit
    a member's earlier open price by accident; member-mode closes
    likewise only match against the same author's opens.
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
    if author_id is not None:
        sql += " AND author_id = ?"
        params.append(int(author_id))
    if tracking_mode is not None:
        sql += " AND tracking_mode = ?"
        params.append((tracking_mode or "").strip().lower() or "caller")
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


def _resolve_close_expiry(
    extracted: dict,
    caller_name: str | None,
    *,
    author_id: int | None = None,
    tracking_mode: str | None = "caller",
) -> None:
    """For CLOSE/TRIM extractions where expiry is null, look up the
    caller's (or member-mode author's) currently-open position matching
    ticker + strike and use that expiry. Mutates extracted in place.

    Why: terse closers like "Closed HOOD 80c @0.41" don't state the
    expiry — the caller obviously means "close the position I already
    have open." Defaulting to today (0DTE) creates a phantom close that
    doesn't match the actual open. Matching against outstanding open
    positions resolves to the right contract every time.

    tracking_mode defaults to 'caller' for backwards-compat; pass
    tracking_mode='member' + author_id for member-mode close matching.

    Cases:
    - action != close/trim → no-op
    - expiry already populated → no-op (trust the OCR)
    - no matching open position → no-op
    """
    action = (extracted.get("action") or "").lower()
    if action not in ("close", "trim"):
        return
    if extracted.get("expiry"):
        return

    matched = db.find_matching_open_expiry(
        caller=caller_name,
        ticker=(extracted.get("ticker") or "").strip(),
        contract_type=extracted.get("contract_type"),
        strike=extracted.get("strike"),
        author_id=author_id,
        tracking_mode=tracking_mode,
    )
    if matched:
        extracted["expiry"] = matched
        extracted["expiry_source"] = "matched_open_position"
        notes = (extracted.get("notes") or "").strip()
        extracted["notes"] = (
            (notes + " " if notes else "")
            + f"(expiry resolved to {matched} via open-position match)"
        ).strip()


def _derive_close_metrics(
    extracted: dict,
    caller_name: str | None,
    *,
    author_id: int | None = None,
    tracking_mode: str | None = "caller",
) -> None:
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
        author_id=author_id,
        tracking_mode=tracking_mode,
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
    tracking_mode: str = "caller",
) -> None:
    """Process an analyst-channel message. Side-effect-only — writes to DB
    and (in caller mode) posts to the announce channel. All failures
    are logged and swallowed so a single bad image never blocks
    subsequent processing.

    Two modes:

    `tracking_mode='caller'` — the legacy / official path. Requires a
    `caller` dict from settings.resolve_analyst_callers(); enforces the
    caller['username'] author filter; writes caller['name'] as the
    canonical `caller` field; runs the announce embed back to Discord.

    `tracking_mode='member'` — universal member-alert path. `caller` is
    None. NO author-username filter (any user posting in an eager-OCR
    alert channel is a candidate). Row is stored with author_id +
    tracking_mode='member' so /ask caller context never sees it, but the
    future points-scoring system has data to read. NO announce embed.

    Legacy callers passing only (bot, message) fall back to the global
    `analyst_primary_author` setting for filtering, and the stored
    `caller` field is None — backwards-compatible with the pre-registry
    deployment but loses hard-separation in /ask context.
    """
    norm_tracking_mode = (tracking_mode or "").strip().lower()
    if norm_tracking_mode not in ("caller", "member"):
        norm_tracking_mode = "caller"

    # Resolve the username to filter by + the canonical caller name to
    # store. Registry-driven (preferred) or legacy fallback. In
    # member mode neither applies.
    if norm_tracking_mode == "member":
        expected_username = ""  # any author in this channel is a candidate
        canonical_caller = None
        # caller_display feeds the OCR extractor's "whose alert is this"
        # hint — for member mode, use the actual author's display name.
        caller_display = (
            getattr(message.author, "display_name", None)
            or message.author.name
            or "the trader"
        )
    elif caller:
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
                "extraction failed integrity check (missing ticker, "
                "strike=0, or non-trade action like 'viewing')"
            )
        is_trade = bool(extracted.get("is_trade_screenshot"))
        if is_trade:
            # Resolve close/trim expiry from matching open position BEFORE
            # deriving close metrics — the derive step needs an expiry to
            # look up the open price for gain%/price computation.
            _resolve_close_expiry(
                extracted,
                canonical_caller,
                author_id=getattr(message.author, "id", None),
                tracking_mode=norm_tracking_mode,
            )
            _derive_close_metrics(
                extracted,
                canonical_caller,
                author_id=getattr(message.author, "id", None),
                tracking_mode=norm_tracking_mode,
            )
        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=synthetic_att_id,
                author=author_name,
                author_id=getattr(message.author, "id", None),
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
                tracking_mode=norm_tracking_mode,
            )
        except Exception as e:
            log.error(f"Analyst log: caption-only DB insert failed: {e}", exc_info=True)
            return
        # Announce only for official-caller posts. Member-mode rows are
        # silent — they exist in analyst_trades for the future points
        # scoring, never produce a public log embed.
        if is_trade and norm_tracking_mode == "caller":
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
                "extraction failed integrity check (missing ticker, "
                "strike=0, or non-trade action like 'viewing')"
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
            # Resolve close/trim expiry from matching open position BEFORE
            # deriving close metrics — the derive step needs an expiry to
            # look up the open price for gain%/price computation.
            _resolve_close_expiry(
                extracted,
                canonical_caller,
                author_id=getattr(message.author, "id", None),
                tracking_mode=norm_tracking_mode,
            )
            _derive_close_metrics(
                extracted,
                canonical_caller,
                author_id=getattr(message.author, "id", None),
                tracking_mode=norm_tracking_mode,
            )

        try:
            db.record_analyst_trade(
                discord_message_id=message.id,
                discord_attachment_id=att.id,
                author=author_name,
                author_id=getattr(message.author, "id", None),
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
                tracking_mode=norm_tracking_mode,
            )
        except Exception as e:
            log.error(f"Analyst log: DB insert failed: {e}", exc_info=True)
            continue

        # Announce only for official-caller posts (see caption-path note above).
        if is_trade and norm_tracking_mode == "caller":
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
