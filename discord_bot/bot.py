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

    @bot.tree.command(name="pulse", description="Generate a Market Pulse from analyses since the last report")
    async def pulse_command(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            from pipeline.orchestrator import run_manual_pulse
            report = await run_manual_pulse()

            if report:
                from report.formatter import format_report_embeds
                embeds = format_report_embeds(report)
                success = await send_embeds(interaction.channel, embeds)
                if success and report.report_id:
                    db.mark_report_sent(report.report_id)
                await interaction.followup.send(
                    f"Market Pulse generated from {report.pdf_count} reports."
                )
            else:
                await interaction.followup.send(
                    "No analyses available. Run `/load 24` first to ingest recent PDFs."
                )
        except Exception as e:
            log.error(f"Manual pulse failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error generating pulse: {str(e)[:200]}")

    @bot.tree.command(name="load", description="Ingest + analyze PDFs uploaded to Dropbox in the last N hours")
    @app_commands.describe(hours="How many hours of recent PDFs to load (max 48)")
    async def load_command(interaction: discord.Interaction, hours: int):
        if hours < 1 or hours > 48:
            await interaction.response.send_message("Hours must be between 1 and 48.")
            return
        await interaction.response.defer(thinking=True)

        try:
            from pipeline.orchestrator import ingest_recent_pdfs
            stats = await ingest_recent_pdfs(hours)
            await interaction.followup.send(
                f"**Load complete ({hours}h window)**\n"
                f"Found: {stats['found']} | New: {stats['new']} | "
                f"Processed: {stats['processed']} | Low (skipped deep): {stats['skipped_low']} | "
                f"Failed: {stats['failed']}\n"
                f"Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out\n"
                f"Run `/pulse` to synthesize a report."
            )
        except Exception as e:
            log.error(f"Load failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error loading PDFs: {str(e)[:200]}")

    @bot.tree.command(name="seedcursor", description="Seed Dropbox cursor to current state (skips backfill on next poll)")
    async def seedcursor_command(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        try:
            from pipeline.orchestrator import seed_dropbox_cursor_to_now
            ts = seed_dropbox_cursor_to_now()
            await interaction.followup.send(
                f"Dropbox cursor seeded at `{ts}`. "
                "Next 15-min poll will only pick up NEW uploads."
            )
        except Exception as e:
            log.error(f"Seed cursor failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.tree.command(name="status", description="Show pipeline health and DB state")
    async def status_command(interaction: discord.Interaction):
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

        # Priority breakdown
        pri_parts = [f"{p}: {c}" for p, c in full["priority_counts"].items()]
        if pri_parts:
            embed.add_field(
                name="Priority mix",
                value=" | ".join(pri_parts),
                inline=False,
            )

        # Upload date range — tells user how far back the analyses reach
        if full["earliest_upload"] and full["latest_upload"]:
            earliest = full["earliest_upload"][:16].replace("T", " ")
            latest = full["latest_upload"][:16].replace("T", " ")
            embed.add_field(
                name="Upload range in DB",
                value=f"Earliest: `{earliest}`\nLatest: `{latest}`",
                inline=False,
            )

        # Tokens all-time
        embed.add_field(
            name="Tokens (all-time)",
            value=f"In: {full['input_tokens']:,} | Out: {full['output_tokens']:,}",
            inline=False,
        )

        # Pulse history
        pulse_lines = []
        if full["last_daily_pulse"]:
            d = full["last_daily_pulse"]
            created = d["created_at"][:16].replace("T", " ")
            sent = "sent" if d["discord_sent_at"] else "NOT sent"
            pulse_lines.append(f"**Last scheduled:** {created} ({d['pdf_count']} PDFs, {sent})")
        else:
            pulse_lines.append("**Last scheduled:** never")
        if full["last_manual_pulse"]:
            m = full["last_manual_pulse"]
            created = m["created_at"][:16].replace("T", " ")
            pulse_lines.append(f"**Last manual:** {created} ({m['pdf_count']} PDFs)")
        embed.add_field(name="Pulses", value="\n".join(pulse_lines), inline=False)

        # Dropbox state
        cursor_state = "✅ seeded" if full["cursor_set"] else "❌ unset (next poll will backfill!)"
        last_poll = full["last_poll_at"][:16].replace("T", " ") if full["last_poll_at"] else "never"
        embed.add_field(
            name="Dropbox watcher",
            value=f"Cursor: {cursor_state}\nLast poll: `{last_poll}`",
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
