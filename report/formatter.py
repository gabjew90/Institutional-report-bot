"""Format Market Pulse reports for Discord delivery.

Splits reports into Discord-compatible chunks (embeds or messages)
with color coding by section type.
"""

import logging
import re
from datetime import date

import discord

from report.models import DailyReport

log = logging.getLogger(__name__)

# Section color mapping
SECTION_COLORS = {
    "SESSION OUTLOOK": 0xFFD700,       # Gold
    "CLOSING PLAYBOOK": 0xFFD700,      # Gold
    "KEY MARKET MOVERS": 0xFF8C00,     # Dark Orange
    "NEW MARKET MOVERS": 0xFF8C00,     # Dark Orange
    "SECTOR ROTATIONS": 0x3498DB,      # Blue
    "EARNINGS": 0x2ECC71,              # Green
    "MACRO": 0x9B59B6,                 # Purple
    "CRYPTO": 0xF39C12,               # Orange
    "TRADE IDEAS": 0x2ECC71,           # Green
    "OVERNIGHT POSITIONING": 0x2ECC71, # Green
    "RISK RADAR": 0xE74C3C,            # Red
    "SESSION TAKEAWAYS": 0x3498DB,     # Blue
}

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

    # Header embed
    if report.report_type == "morning":
        title = f"MORNING MARKET PULSE | {today}"
        description = f"Pre-Market Edition | {report.pdf_count} institutional research reports analyzed"
        color = 0xFFD700
    else:
        title = f"AFTERNOON MARKET PULSE | {today}"
        description = f"Pre-Close Edition | {report.pdf_count} new reports since morning"
        color = 0xFF8C00

    header_embed = discord.Embed(
        title=title,
        description=description,
        color=color,
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

    # Footer embed
    footer_embed = discord.Embed(
        description=(
            "*Based on institutional research synthesis. Not financial advice. "
            "Do your own due diligence.*"
        ),
        color=0x95A5A6,
    )
    if report.report_type == "morning":
        footer_embed.set_footer(text="Afternoon update at 3:00 PM ET")
    else:
        footer_embed.set_footer(text="Next morning pulse at 8:30 AM ET")

    embeds.append(footer_embed)

    log.info(f"Formatted {len(embeds)} embeds for {report.report_type} pulse")
    return embeds


def format_report_plain(report: DailyReport) -> list[str]:
    """Convert a DailyReport into plain text messages (for CLI/testing).

    Splits into chunks under 2000 chars (Discord message limit).
    """
    today = report.report_date or date.today().isoformat()

    if report.report_type == "morning":
        header = (
            f"{'=' * 50}\n"
            f"MORNING MARKET PULSE | {today}\n"
            f"Pre-Market Edition | {report.pdf_count} reports analyzed\n"
            f"{'=' * 50}\n"
        )
    else:
        header = (
            f"{'=' * 50}\n"
            f"AFTERNOON MARKET PULSE | {today}\n"
            f"Pre-Close Edition | {report.pdf_count} new reports\n"
            f"{'=' * 50}\n"
        )

    full_text = header + "\n" + report.markdown_content

    # Split into 2000-char chunks
    return _chunk_text(full_text, max_len=1900)
