"""Discord bot client with slash commands."""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import settings
from discord_bot.sender import send_embeds
import db

log = logging.getLogger(__name__)


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

    @bot.tree.command(name="pulse", description="Generate a Market Pulse report")
    @app_commands.describe(
        hours="Hours to look back (e.g. 24, 48). Leave empty for since last report.",
    )
    async def pulse_command(
        interaction: discord.Interaction,
        hours: int | None = None,
    ):
        await interaction.response.defer(thinking=True)

        try:
            from datetime import datetime, timedelta
            from pipeline.orchestrator import run_manual_pulse

            parsed_since = None
            if hours:
                if hours > 48:
                    await interaction.followup.send("Max lookback is 48 hours.")
                    return
                parsed_since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

            report = await run_manual_pulse(since=parsed_since)

            if report:
                from report.formatter import format_report_embeds
                embeds = format_report_embeds(report)
                success = await send_embeds(interaction.channel, embeds)
                if success and report.report_id:
                    db.mark_report_sent(report.report_id)
                label = f"last {hours}h" if hours else "since last report"
                await interaction.followup.send(
                    f"Market Pulse generated from {report.pdf_count} reports ({label})."
                )
            else:
                await interaction.followup.send("No reports available to generate a pulse.")
        except Exception as e:
            log.error(f"Manual pulse failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error generating pulse: {str(e)[:200]}")

    @bot.tree.command(name="status", description="Show pipeline health and stats")
    async def status_command(interaction: discord.Interaction):
        stats = db.get_today_stats()

        embed = discord.Embed(
            title="Pipeline Status",
            color=0x3498DB,
        )
        embed.add_field(name="PDFs Today", value=str(stats["total"]), inline=True)
        embed.add_field(name="Processed", value=str(stats["processed"]), inline=True)
        embed.add_field(name="Pending", value=str(stats["pending"]), inline=True)
        embed.add_field(name="Failed", value=str(stats["failed"]), inline=True)
        embed.add_field(
            name="Tokens Used",
            value=f"In: {stats['input_tokens']:,} | Out: {stats['output_tokens']:,}",
            inline=False,
        )
        if stats["last_report_type"]:
            embed.add_field(
                name="Last Report",
                value=f"{stats['last_report_type']} at {stats['last_report_sent'] or 'not sent'}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    @bot.tree.command(name="reprocess", description="Retry a failed PDF by filename")
    @app_commands.describe(filename="The PDF filename to reprocess")
    async def reprocess_command(interaction: discord.Interaction, filename: str):
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

    return bot
