"""HTTP API for the Opus routine to fetch synthesis context + post results.

Two endpoints, token-gated via `?token=...` (matched against settings.pulse_api_token):

GET /api/pulse/context?token=XXX
  Returns the prepared synthesis context (analyses_json + live data + theme
  coverage + prev-pulse blocks). The Opus agent reads this, applies the DRAFT
  + AUDIT prompts (which it pulls from the git repo), and produces final
  markdown.

POST /api/pulse/submit?token=XXX
  Body: {"markdown": str, "report_type": "daily" | "manual"}
  Saves to daily_reports table and posts to every Discord channel in
  DISCORD_CHANNEL_ID. Returns {"report_id": int, "channels_sent": int}.

GET /healthz
  Liveness check, no auth.

The bot reference is injected at startup so /submit can post to Discord.
"""

import json
import logging
from typing import Any

from aiohttp import web

from config import settings
from pipeline.orchestrator import _load_analyses_from_db
from report.synthesizer import build_pulse_context
import db

log = logging.getLogger(__name__)


def _token_ok(request: web.Request) -> bool:
    expected = (settings.pulse_api_token or "").strip()
    if not expected:
        return False  # API disabled when no token configured
    given = request.query.get("token", "").strip()
    return given == expected


async def _healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _pulse_context(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    # Window selection: query param ?hours=N, default 24
    try:
        hours = int(request.query.get("hours", "24"))
    except ValueError:
        hours = 24
    hours = max(1, min(hours, 168))

    use_prev_context = request.query.get("use_prev", "true").lower() != "false"

    # Load analyses for the window
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    rows = db.get_analyses_since(cutoff)
    if not rows:
        return web.json_response({"error": "no analyses in window", "hours": hours}, status=404)

    analyses = _load_analyses_from_db(rows)
    if not analyses:
        return web.json_response({"error": "no parseable analyses"}, status=500)

    ctx = build_pulse_context(analyses, use_prev_context=use_prev_context)
    ctx["window_hours"] = hours
    ctx["use_prev_context"] = use_prev_context
    return web.json_response(ctx)


async def _pulse_submit(request: web.Request) -> web.Response:
    if not _token_ok(request):
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json body"}, status=400)

    markdown = (body or {}).get("markdown", "").strip()
    if not markdown:
        return web.json_response({"error": "markdown is required"}, status=400)

    report_type = (body.get("report_type") or "daily").lower()
    if report_type not in ("daily", "manual"):
        return web.json_response({"error": "report_type must be 'daily' or 'manual'"}, status=400)

    # Persist to daily_reports
    from datetime import date
    today = date.today().isoformat()
    pdf_count = int(body.get("pdf_count") or 0)
    input_tokens = int(body.get("input_tokens") or 0)
    output_tokens = int(body.get("output_tokens") or 0)

    report_id = db.insert_daily_report(
        report_date=today,
        report_type=report_type,
        report_json=json.dumps({"source": "opus_routine", "pdf_count": pdf_count}),
        report_markdown=markdown,
        pdf_count=pdf_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    # Post to Discord if the bot is wired in via app state
    bot = request.app.get("bot")
    channels_sent = 0
    if bot is not None and settings.discord_channel_ids:
        from report.formatter import format_report_embeds
        from report.models import DailyReport
        from discord_bot.sender import send_embeds

        report = DailyReport(
            report_date=today,
            report_type=report_type,
            pdf_count=pdf_count,
            markdown_content=markdown,
            raw_json={"source": "opus_routine"},
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stats={},
        )
        embeds = format_report_embeds(report)
        for cid in settings.discord_channel_ids:
            try:
                channel = bot.get_channel(cid)
                if channel:
                    ok = await send_embeds(channel, embeds)
                    if ok:
                        channels_sent += 1
                        log.info(f"Opus routine pulse posted to channel {cid} ({channel.name})")
                else:
                    log.warning(f"Discord channel {cid} not found for routine pulse")
            except Exception as e:
                log.error(f"Failed to post routine pulse to channel {cid}: {e}", exc_info=True)
        if channels_sent:
            db.mark_report_sent(report_id)

    return web.json_response({
        "ok": True,
        "report_id": report_id,
        "channels_sent": channels_sent,
    })


def build_app(bot: Any | None = None) -> web.Application:
    app = web.Application(client_max_size=20 * 1024 * 1024)  # 20 MB body cap for big markdown
    app["bot"] = bot
    app.router.add_get("/healthz", _healthz)
    app.router.add_get("/api/pulse/context", _pulse_context)
    app.router.add_post("/api/pulse/submit", _pulse_submit)
    return app


async def start_server(bot: Any | None, port: int) -> web.AppRunner:
    """Start the API server on the given port. Returns the runner so caller can shutdown."""
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info(f"API server listening on 0.0.0.0:{port}")
    return runner
