"""Discord bot client with slash commands."""

import logging
from datetime import datetime

import discord
import pytz
from discord import app_commands
from discord.ext import commands

from config import settings
from discord_bot.sender import send_embeds
import db

log = logging.getLogger(__name__)

_display_tz = pytz.timezone(settings.timezone)


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

    @bot.tree.command(name="pulse", description="Generate a Market Pulse from analyses in the window")
    @app_commands.describe(
        hours="Optional: how many hours back to look (default: since last scheduled pulse, or 24h). Max 168 (1 week).",
    )
    async def pulse_command(interaction: discord.Interaction, hours: int | None = None):
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

    @bot.tree.command(name="load", description="Ingest + analyze PDFs uploaded to Dropbox in the last N hours")
    @app_commands.describe(
        hours="How many hours of recent PDFs to load (max 48)",
        password="Admin password",
    )
    async def load_command(interaction: discord.Interaction, hours: int, password: str):
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
            from pipeline.orchestrator import reanalyze_recent_pdfs

            status_msg = await interaction.followup.send(
                f"Starting reanalyze ({hours}h window, {filter_label})…"
            )

            async def on_progress(stats: dict, phase: str):
                if phase == "starting":
                    content = (
                        f"**Reanalyze ({hours}h window)** — targeting {stats['target']} PDFs "
                        f"from the DB. Re-downloading from Dropbox and re-running analysis "
                        f"with the current prompt."
                    )
                elif phase == "processing":
                    done = stats["processed"] + stats["failed"]
                    target = stats["target"]
                    pct = int((done / target) * 100) if target else 0
                    current = stats.get("current_file", "")
                    recent = stats.get("recent_files", [])
                    content = (
                        f"**Reanalyze ({hours}h window)** — {done}/{target} done ({pct}%)\n"
                        f"Processed: {stats['processed']} | Failed: {stats['failed']}\n"
                        f"Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out"
                    )
                    if current:
                        content += f"\n\n**Now:** {current[:80]}"
                    if recent:
                        content += f"\n**Recent:**\n" + "\n".join(recent[-5:])
                    content = content[:1900]
                else:  # done
                    content = (
                        f"**Reanalyze complete ({hours}h window)**\n"
                        f"Target: {stats['target']} | Processed: {stats['processed']} | "
                        f"Failed: {stats['failed']}\n"
                        f"Tokens: {stats['input_tokens']:,} in / {stats['output_tokens']:,} out\n"
                        f"New analysis rows appended alongside old ones. "
                        f"Run `/pulse` to see synthesis with refreshed data."
                    )
                try:
                    await status_msg.edit(content=content)
                except Exception:
                    pass

            await reanalyze_recent_pdfs(hours, progress_cb=on_progress, priority_filter=priority_filter)
        except Exception as e:
            log.error(f"Reanalyze failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.tree.command(name="clearqueue", description="Delete pending (DOWNLOADED) PDFs from the queue — destructive, cancels backlog")
    @app_commands.describe(
        password="Admin password",
        confirm="Set true to skip the >500 safety check for large purges",
    )
    async def clearqueue_command(interaction: discord.Interaction, password: str, confirm: bool = False):
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

    @bot.tree.command(name="seedcursor", description="Seed Dropbox cursor to current state (skips backfill on next poll)")
    @app_commands.describe(password="Admin password")
    async def seedcursor_command(interaction: discord.Interaction, password: str):
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
