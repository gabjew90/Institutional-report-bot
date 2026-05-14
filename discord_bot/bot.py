"""Discord bot client with slash commands."""

import logging
import re
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


# --- Perplexity /ask integration --------------------------------------------
# Lazy-initialized OpenAI-compatible client pointed at Perplexity Sonar.
# Empty PERPLEXITY_API_KEY => `_get_perplexity_client()` returns None and the
# /ask + @mention surfaces return a polite "not configured" message instead of
# erroring out.
_perplexity_client = None


def _get_perplexity_client():
    global _perplexity_client
    if _perplexity_client is not None:
        return _perplexity_client
    if not settings.perplexity_api_key:
        return None
    try:
        from openai import OpenAI
        _perplexity_client = OpenAI(
            api_key=settings.perplexity_api_key,
            base_url="https://api.perplexity.ai",
        )
        return _perplexity_client
    except Exception as e:
        log.error(f"Failed to init Perplexity client: {e}")
        return None


def _format_citations(answer_text: str, search_results: list) -> str:
    """Replace inline [N] markers in the answer with [[N]](url) markdown links.

    Perplexity returns a `search_results` array where each item has a `url`
    (and usually `title`). Inline references in the answer look like `[1]`.
    We turn those into clickable markdown so Discord renders them as links.
    """
    if not search_results:
        return answer_text

    def repl(match: re.Match) -> str:
        try:
            n = int(match.group(1))
        except ValueError:
            return match.group(0)
        idx = n - 1
        if 0 <= idx < len(search_results):
            url = (search_results[idx] or {}).get("url")
            if url:
                return f"[[{n}]](<{url}>)"
        return match.group(0)

    return re.sub(r"\[(\d+)\]", repl, answer_text)


async def _answer_with_perplexity(question: str, user_id: int) -> discord.Embed:
    """Run a Perplexity Sonar query and return a Discord embed.

    Enforces the per-user daily cap. Returns a single embed with the formatted
    answer + NFA footer, or an error embed on failure.
    """
    # Quota check
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

    client = _get_perplexity_client()
    if client is None:
        return discord.Embed(
            description=(
                "/ask is not configured on this bot. Set the "
                "`PERPLEXITY_API_KEY` env var to enable web-search Q&A."
            ),
            color=0xE74C3C,
        )

    try:
        response = client.chat.completions.create(
            model="sonar-pro",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a financial markets assistant for active "
                        "traders. Format responses with arrow points using "
                        "→ followed by a blank line between each point. "
                        "Bold key terms and numbers with **bold**. Keep it "
                        "to 3-5 points max. Cite sources. Be extremely "
                        "concise and readable."
                    ),
                },
                {"role": "user", "content": question},
            ],
            max_tokens=200,
            temperature=0.2,
        )
        answer = response.choices[0].message.content or ""
        search_results = getattr(response, "search_results", None) or []
        formatted = _format_citations(answer, search_results)
        db.record_ask_query(user_id)
        embed = discord.Embed(description=formatted[:4000], color=0x228B22)
        embed.set_footer(text="Hi, I'm AI-powered - NFA")
        return embed
    except Exception as e:
        log.error(f"Perplexity /ask call failed: {e}", exc_info=True)
        return discord.Embed(
            description=f"Web search failed: {str(e)[:200]}",
            color=0xE74C3C,
        )


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

    @bot.tree.command(name="ask", description="Web-search Q&A via Perplexity Sonar (not our research corpus)")
    @app_commands.describe(question="What do you want to know? (current events, prices, news, etc.)")
    async def ask_command(interaction: discord.Interaction, question: str):
        question = (question or "").strip()
        if not question:
            await interaction.response.send_message("Ask a question first.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            user_id = interaction.user.id if interaction.user else 0
            embed = await _answer_with_perplexity(question, user_id)
            await interaction.followup.send(embed=embed)
        except Exception as e:
            log.error(f"/ask failed: {e}", exc_info=True)
            await interaction.followup.send(f"Error: {str(e)[:200]}")

    @bot.event
    async def on_message(message: discord.Message):
        # Ignore the bot's own messages + other bots.
        if message.author.bot:
            return
        # Only respond when the bot is explicitly @-mentioned.
        if bot.user is None or bot.user not in message.mentions:
            await bot.process_commands(message)
            return
        # Strip the mention(s) from the content to get the actual question.
        content = message.content or ""
        for mention in (f"<@{bot.user.id}>", f"<@!{bot.user.id}>"):
            content = content.replace(mention, "")
        question = content.strip()
        if not question:
            await message.reply(
                "Mention me with a question and I'll search the web for you.",
                mention_author=False,
            )
            return
        try:
            async with message.channel.typing():
                embed = await _answer_with_perplexity(question, message.author.id)
                await message.reply(embed=embed, mention_author=False)
        except Exception as e:
            log.error(f"@mention /ask failed: {e}", exc_info=True)
            try:
                await message.reply(f"Error: {str(e)[:200]}", mention_author=False)
            except Exception:
                pass
        await bot.process_commands(message)

    return bot
