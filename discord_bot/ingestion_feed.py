"""Real-time ingestion feed for Discord.

Posts a structured embed for each newly-analyzed HIGH/MEDIUM PDF, trickled
1-per-interval (default 60s) into a dedicated Discord channel. Zero new LLM
tokens — uses the structured fields already extracted by the deep-analysis
pipeline (key_insights, entities_mentioned, trade_ideas, etc.).

Flow:
- 60s scheduler tick calls announce_next_pending(bot)
- Pulls the next pdf_file_id > last_announced where priority IN ('high','medium')
- Builds an embed from the cached analysis_json + a 4h Dropbox download link
- Posts to DISCORD_INGEST_FEED_CHANNEL_ID (single configured channel)
- Updates last_announced

On startup: announce_startup_backfill() checks for backlog. If more than
INGEST_FEED_BACKLOG_THRESHOLD PDFs are queued, posts a single summary card
and fast-forwards last_announced to current MAX (skips the backfill items).
Otherwise lets the trickle catch them up at 60s cadence.
"""

import json
import logging
from datetime import datetime
from typing import Any

import discord
import pytz

from config import settings
import db

log = logging.getLogger(__name__)

PRIORITY_COLOR = {
    "high": 0x2ECC71,    # green
    "medium": 0xF1C40F,  # amber
    "low": 0x95A5A6,     # gray (won't be posted; LOW filtered upstream)
}
PRIORITY_EMOJI = {"high": "🟢", "medium": "🟡", "low": "⚪"}
CASHTAG_CLASSES = {"stock", "etf", "crypto", "index"}
_ET = pytz.timezone("America/New_York")


def feed_enabled() -> bool:
    return bool(settings.discord_ingest_feed_channel_id)


def _channel_id() -> int | None:
    raw = settings.discord_ingest_feed_channel_id.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _fmt_uploaded(dropbox_modified_at: str | None) -> str:
    if not dropbox_modified_at:
        return ""
    try:
        clean = dropbox_modified_at.replace("T", " ")[:19]
        dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.UTC)
        return f"uploaded {dt.astimezone(_ET).strftime('%b %-d %H:%M %Z')}"
    except Exception:
        return ""


def _build_embed(row: dict, temp_link: str) -> discord.Embed:
    """Construct a Discord embed from a pdf_analyses row + Dropbox temp link."""
    try:
        analysis = json.loads(row.get("analysis_json") or "{}")
    except json.JSONDecodeError:
        analysis = {}

    priority = (row.get("priority") or "").lower()
    color = PRIORITY_COLOR.get(priority, 0x95A5A6)
    emoji = PRIORITY_EMOJI.get(priority, "⚪")
    source = (analysis.get("source") or "Unknown").strip()
    report_type = (analysis.get("report_type") or "other").strip()
    title = (analysis.get("title") or row.get("file_name") or "(untitled)").strip()
    if len(title) > 256:
        title = title[:253] + "..."

    # Headline insight: first key_insights entry (already a 1-2 sentence
    # summary written at deep-analysis time)
    insights = analysis.get("key_insights") or []
    description = (insights[0] if insights else "").strip()
    if len(description) > 600:
        description = description[:597] + "..."

    embed = discord.Embed(title=title, description=description, color=color)
    author_str = f"{emoji} {source} · {priority.upper()} · {report_type}"
    embed.set_author(name=author_str[:256])

    # Tickers — top 6 unique cashtag-eligible entities, $-prefixed
    entities = analysis.get("entities_mentioned") or []
    tickers: list[str] = []
    seen: set[str] = set()
    for e in entities:
        ticker = (e.get("ticker") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        asset_class = (e.get("class") or e.get("asset_class") or "").lower().strip()
        if asset_class in CASHTAG_CLASSES:
            tickers.append(f"${ticker}")
        else:
            tickers.append(ticker)
        if len(tickers) >= 6:
            break
    if tickers:
        embed.add_field(name="Tickers", value=" · ".join(tickers), inline=False)

    # Top trade idea (if present)
    trades = analysis.get("trade_ideas") or analysis.get("trades") or []
    if trades and isinstance(trades[0], dict):
        ti = trades[0]
        ti_text = (ti.get("description") or "").strip()
        if ti_text:
            meta_bits: list[str] = []
            conviction = (ti.get("conviction") or "").strip().lower()
            horizon = (ti.get("time_horizon") or "").strip()
            if conviction:
                meta_bits.append(conviction)
            if horizon:
                meta_bits.append(horizon)
            if meta_bits:
                ti_text += f" ({' / '.join(meta_bits)})"
            if len(ti_text) > 1024:
                ti_text = ti_text[:1021] + "..."
            embed.add_field(name="Trade idea", value=ti_text, inline=False)

    # Download link
    if temp_link:
        embed.add_field(name="📥 Download", value=f"[Open PDF]({temp_link})", inline=False)

    # Footer
    foot_bits: list[str] = []
    pages = row.get("total_pages") or analysis.get("total_pages")
    if pages:
        foot_bits.append(f"{pages} pages")
    uploaded = _fmt_uploaded(row.get("dropbox_modified_at"))
    if uploaded:
        foot_bits.append(uploaded)
    if foot_bits:
        embed.set_footer(text=" · ".join(foot_bits))

    return embed


async def announce_next_pending(bot) -> None:
    """Post the next queued HIGH/MEDIUM ingestion (one per call). 60s cadence."""
    if not feed_enabled() or bot is None:
        return
    cid = _channel_id()
    if cid is None:
        log.warning("Ingest feed: invalid DISCORD_INGEST_FEED_CHANNEL_ID")
        return

    last = db.get_ingest_feed_last_announced()
    row = db.get_next_pdf_to_announce(last)
    if not row:
        return

    pf_id = int(row["pf_id"])
    try:
        from dropbox_client.watcher import get_temporary_link
        import asyncio
        # Dropbox API call is sync — run in a thread to avoid blocking event loop
        temp_link = await asyncio.to_thread(get_temporary_link, row.get("dropbox_path") or "")
        embed = _build_embed(row, temp_link)

        channel = bot.get_channel(cid)
        if channel is None:
            log.warning(f"Ingest feed: channel {cid} not found")
            return
        await channel.send(embed=embed)
        log.info(f"Ingest feed: posted pdf_file_id={pf_id} ({row.get('file_name', '')[:60]})")

        # Mark as announced AFTER successful post so a transient Discord error
        # doesn't permanently skip the entry. If post fails the next tick retries.
        db.set_ingest_feed_last_announced(pf_id)
    except Exception as e:
        log.error(f"Ingest feed: failed to announce pdf_file_id={pf_id}: {e}", exc_info=True)


async def announce_startup_backfill(bot) -> None:
    """One-shot at boot. If there's a large backlog, summarize and fast-forward."""
    if not feed_enabled() or bot is None:
        return
    cid = _channel_id()
    if cid is None:
        return

    last = db.get_ingest_feed_last_announced()
    pending = db.count_pending_announcements(last)
    threshold = settings.ingest_feed_backlog_threshold

    if pending["total"] == 0:
        log.info("Ingest feed: no backlog at startup")
        return

    if pending["total"] < threshold:
        # Small enough — let the 60s trickle catch up naturally
        log.info(
            f"Ingest feed: small backlog at startup ({pending['total']} pending), "
            "letting trickle catch up"
        )
        return

    # Large backlog — post a summary card and fast-forward
    current_max = db.max_announceable_pdf_file_id()
    embed = discord.Embed(
        title="📦 Ingestion catch-up",
        description=(
            f"**{pending['total']} HIGH/MEDIUM PDFs** were processed while "
            f"the live feed was offline. Resuming real-time ingestion announcements now."
        ),
        color=0x95A5A6,
    )
    embed.add_field(
        name="Backfill breakdown",
        value=f"🟢 HIGH: **{pending['high']}**   🟡 MEDIUM: **{pending['medium']}**",
        inline=False,
    )
    embed.set_footer(text=f"fast-forwarded past pdf_file_id {last + 1}–{current_max}")

    try:
        channel = bot.get_channel(cid)
        if channel is None:
            log.warning(f"Ingest feed: channel {cid} not found at startup")
            return
        await channel.send(embed=embed)
        db.set_ingest_feed_last_announced(current_max)
        log.info(
            f"Ingest feed: posted backfill summary ({pending['total']} PDFs), "
            f"fast-forwarded last_announced to {current_max}"
        )
    except Exception as e:
        log.error(f"Ingest feed: backfill summary post failed: {e}", exc_info=True)
