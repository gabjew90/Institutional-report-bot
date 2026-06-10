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
    "WHAT CHANGED": 0x1ABC9C,           # Teal (format-overhaul Phase 1)
    "INSIGHTS": 0x3498DB,               # Blue
    "ALPHA": 0x3498DB,                  # Blue (for "INSIGHTS & ALPHA")
    "TRADE BOARD": 0x9B59B6,            # Purple (format-overhaul Phase 1)
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


def _normalize_whitespace(text: str) -> str:
    """Collapse excessive blank lines so Discord doesn't render tall empty gaps.

    Runs of 3+ newlines → 2 (single blank line paragraph break).
    Trailing whitespace stripped.
    """
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_markdown_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into (header, content) tuples by ## headers."""
    sections: list[tuple[str, str]] = []
    current_header = ""
    current_content: list[str] = []

    for line in markdown.split("\n"):
        if line.startswith("## "):
            if current_header or current_content:
                sections.append((current_header, _normalize_whitespace("\n".join(current_content))))
            current_header = line.lstrip("# ").strip()
            current_content = []
        else:
            current_content.append(line)

    if current_header or current_content:
        sections.append((current_header, _normalize_whitespace("\n".join(current_content))))

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


def _extract_pulse_title(md: str) -> tuple[str | None, str]:
    """Extract the leading H1 title from pulse markdown.

    Returns (title, remaining_markdown). If no H1 at the top, returns
    (None, original_md). Strips the H1 line + optional blank line so it
    isn't duplicated downstream when sections render.
    """
    if not md:
        return None, md or ""
    m = re.match(r'^\s*#\s+(.+?)\s*\n+', md)
    if not m:
        return None, md
    title = m.group(1).strip()
    remaining = md[m.end():]
    return title, remaining


def _format_pulse_datetime() -> str:
    """Day-of-week + date + time in ET as a single header string.

    Example: 'Wednesday, May 7, 2026 — 11:23 PM ET'.
    """
    tz = pytz.timezone(settings.timezone)
    now_local = datetime.now(tz)
    weekday = now_local.strftime("%A")
    month = now_local.strftime("%B")
    day = now_local.day  # no zero-pad
    year = now_local.year
    hour_12 = now_local.hour % 12 or 12
    ampm = "AM" if now_local.hour < 12 else "PM"
    minute = now_local.minute
    tzname = now_local.tzname() or "ET"
    return f"{weekday}, {month} {day}, {year} — {hour_12}:{minute:02d} {ampm} {tzname}"


def format_report_header_message(report: DailyReport) -> str:
    """Returns empty string — title + date both live inside the gold
    header embed now (built in format_report_embeds via Discord's markdown-
    header support in embed descriptions). Kept for API compatibility
    with the sender's leading_content kwarg.
    """
    return ""


def format_report_embeds(report: DailyReport) -> list[discord.Embed]:
    """Convert a DailyReport's section content into a list of Discord embeds.

    Layout: a slim gold "date marker" embed (visual stripe under the
    leading markdown title) + per-section embeds (RECAP / INSIGHTS /
    WHAT TO WATCH) + footer embed.

    Title itself ships as the leading markdown message via
    format_report_header_message() — embed titles can't render at
    markdown-header size, but the gold date embed restores the visual
    stripe marker the original gold-header-embed used to provide.
    """
    embeds: list[discord.Embed] = []

    # Header embed: gold-bordered, contains BOTH the eye-catching title and
    # the date inside its description. Discord renders `#` and `##` markdown
    # headers in embed descriptions as large/medium text — so the title gets
    # big-text treatment AND lives inside the embed (no asymmetry where the
    # date was embedded but the title wasn't).
    pulse_title, body_md = _extract_pulse_title(report.markdown_content or "")
    if not pulse_title:
        pulse_title = "Market Pulse"
    header_embed = discord.Embed(
        description=f"# {pulse_title}\n## {_format_pulse_datetime()}",
        color=0xFFD700,
    )
    embeds.append(header_embed)

    # Parse markdown into sections and create embeds
    sections = _split_markdown_sections(body_md)

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
