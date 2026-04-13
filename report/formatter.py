"""Format Market Pulse reports for Discord delivery.

Splits reports into Discord-compatible chunks (embeds or messages)
with color coding by section type.
"""

import logging
import re
from datetime import date, datetime, timedelta

import discord
import pytz

from config import settings
from report.models import DailyReport

log = logging.getLogger(__name__)

# Section color mapping — matches the 3-section structure (RECAP / INSIGHTS / WHAT TO WATCH)
SECTION_COLORS = {
    "RECAP": 0xFFD700,                  # Gold
    "INSIGHTS": 0x3498DB,               # Blue
    "ALPHA": 0x3498DB,                  # Blue (for "INSIGHTS & ALPHA")
    "WHAT TO WATCH": 0xFF8C00,          # Dark Orange
}


def _next_pulse_str() -> str:
    """Compute next scheduled pulse time in the configured timezone.

    Avoids strftime('%-I') which isn't portable to Windows.
    """
    tz = pytz.timezone(settings.timezone)
    now_local = datetime.now(tz)
    target = now_local.replace(
        hour=settings.daily_pulse_hour,
        minute=settings.daily_pulse_minute,
        second=0, microsecond=0,
    )
    if target <= now_local:
        target = target + timedelta(days=1)
    is_tomorrow = target.date() > now_local.date()
    prefix = "Tomorrow" if is_tomorrow else "Today"
    hour_12 = target.hour % 12 or 12
    ampm = "AM" if target.hour < 12 else "PM"
    return f"{prefix} {hour_12}:{target.minute:02d} {ampm} {target.tzname()}"

# Maximum chars per embed description
MAX_EMBED_CHARS = 4000


def _get_section_color(header: str) -> int:
    """Match a section header to its color."""
    header_upper = header.upper()
    for key, color in SECTION_COLORS.items():
        if key in header_upper:
            return color
    return 0x95A5A6  # Default grey


def _split_markdown_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (header, content) tuples by ## headers."""
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_content: list[str] = []

    for line in markdown.split("\n"):
        if line.startswith("## "):
            if current_header or current_content:
                sections.append((current_header, "\n".join(current_content).strip()))
            current_header = line.lstrip("# ").strip()
            current_content = []
        else:
            current_content.append(line)

    if current_header or current_content:
        sections.append((current_header, "\n".join(current_content).strip()))

    return sections


def _chunk_text(text: str, max_len: int = MAX_EMBED_CHARS) -> list[str]:
    """Split text into chunks at natural boundaries."""
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break

        # Find a good break point
        break_point = max_len
        for sep in ["\n\n", "\n", ". ", " "]:
            idx = text.rfind(sep, 0, max_len)
            if idx > max_len // 2:
                break_point = idx + len(sep)
                break

        chunks.append(text[:break_point].rstrip())
        text = text[break_point:].lstrip()

    return chunks


def format_report_embeds(report: DailyReport) -> list[discord.Embed]:
    """Convert a DailyReport into a list of Discord embeds."""
    embeds: list[discord.Embed] = []
    today = report.report_date or date.today().isoformat()

    header_embed = discord.Embed(
        title=f"MARKET PULSE | {today}",
        description=f"{report.pdf_count} institutional research reports analyzed",
        color=0xFFD700,
    )
    embeds.append(header_embed)

    # Parse markdown into sections and create embeds
    sections = _split_markdown_sections(report.markdown_content)

    for header, content in sections:
        if not content.strip():
            continue

        color = _get_section_color(header)
        chunks = _chunk_text(content)

        for i, chunk in enumerate(chunks):
            section_title = header if i == 0 else f"{header} (continued)"
            embed = discord.Embed(
                title=section_title,
                description=chunk,
                color=color,
            )
            embeds.append(embed)

    # Footer embed — dynamic stats + next pulse time
    footer_lines = []
    stats = report.stats or {}
    pdf_count = stats.get("pdf_count") or report.pdf_count

    footer_lines.append(f"**{pdf_count} research reports analyzed**")

    top_sources = stats.get("top_sources") or []
    if top_sources:
        src_str = " · ".join(f"{src} ({n})" for src, n in top_sources[:5])
        footer_lines.append(f"Top sources: {src_str}")

    priority_mix = stats.get("priority_mix") or {}
    if priority_mix:
        pri_str = " · ".join(f"{k.lower()}: {v}" for k, v in priority_mix.items())
        footer_lines.append(f"Priority mix: {pri_str}")

    earliest = stats.get("earliest_upload")
    latest = stats.get("latest_upload")
    if earliest and latest:
        if earliest == latest:
            footer_lines.append(f"Research dated: {earliest}")
        else:
            footer_lines.append(f"Research dated: {earliest} → {latest}")

    footer_embed = discord.Embed(
        description="\n".join(footer_lines),
        color=0x95A5A6,
    )
    footer_embed.set_footer(text=f"Next pulse: {_next_pulse_str()}")
    embeds.append(footer_embed)

    log.info(f"Formatted {len(embeds)} embeds for daily pulse")
    return embeds


def format_report_plain(report: DailyReport) -> list[str]:
    """Convert a DailyReport into plain text messages (for CLI/testing).

    Splits into chunks under 2000 chars (Discord message limit).
    """
    today = report.report_date or date.today().isoformat()

    header = (
        f"{'=' * 50}\n"
        f"MARKET PULSE | {today}\n"
        f"{report.pdf_count} reports analyzed\n"
        f"{'=' * 50}\n"
    )

    full_text = header + "\n" + report.markdown_content

    # Split into 2000-char chunks
    return _chunk_text(full_text, max_len=1900)
