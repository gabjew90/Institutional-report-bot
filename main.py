"""Entry point: starts the Discord bot, scheduler, and processing pipeline."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

import discord

from config import settings


# Login retry: when Railway redeploys frequently OR after multiple bot
# reconnect attempts in a short window, Discord 429s the login (error
# code 40062 = "Service resource is being rate limited"). Without a
# retry, the worker crashes; Railway restarts it; same 429 → crashloop.
# Railway gives up after restartPolicyMaxRetries=5, leaving the worker
# dead and pending pulses undelivered until manual redeploy.
#
# Retry locally with exponential backoff so a transient login 429 just
# delays startup instead of killing the deployment.
LOGIN_MAX_ATTEMPTS = 8
LOGIN_BACKOFF_SECONDS = (30, 60, 120, 300, 600, 900, 1200, 1800)  # 30s → 30min


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


async def main() -> None:
    setup_logging()
    log = logging.getLogger("main")
    log.info("Starting Institutional Report Bot...")

    # Ensure data directories exist
    Path(settings.pdf_download_dir).mkdir(parents=True, exist_ok=True)

    # Initialize database
    import db
    db.get_connection()
    log.info("Database initialized")

    # Import after config/db are ready
    from discord_bot.bot import create_bot
    from scheduler.jobs import setup_scheduler

    # Create Discord bot
    bot = create_bot()

    # Setup scheduler (attaches to bot's event loop)
    scheduler = setup_scheduler(bot)
    scheduler.start()
    log.info("Scheduler started")

    # Optional HTTP API for the Opus pulse routine. Only starts if a token is set.
    api_runner = None
    if settings.pulse_api_token:
        from api.server import start_server
        api_runner = await start_server(bot, settings.http_port)
    else:
        log.info("PULSE_API_TOKEN not set — HTTP API disabled")

    # Handle shutdown
    def handle_shutdown(sig, frame):
        log.info(f"Received signal {sig}, shutting down...")
        scheduler.shutdown(wait=False)
        loop = asyncio.get_event_loop()
        if api_runner is not None:
            loop.create_task(api_runner.cleanup())
        loop.create_task(bot.close())

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Start Discord bot with login retry for 429s. bot.start() does
    # login + connect; we wrap login separately so we can retry on
    # rate limits, then call connect to enter the run loop.
    log.info("Starting Discord bot...")
    last_err: Exception | None = None
    for attempt in range(LOGIN_MAX_ATTEMPTS):
        try:
            await bot.login(settings.discord_bot_token)
            log.info(f"Discord login succeeded on attempt {attempt + 1}")
            break
        except discord.HTTPException as e:
            last_err = e
            if e.status != 429:
                # Non-429 HTTP errors are not retryable here (bad token, etc.)
                log.error(f"Discord login failed with non-429 HTTPException: {e}")
                raise
            backoff = LOGIN_BACKOFF_SECONDS[min(attempt, len(LOGIN_BACKOFF_SECONDS) - 1)]
            log.warning(
                f"Discord login 429 (attempt {attempt + 1}/{LOGIN_MAX_ATTEMPTS}); "
                f"backing off {backoff}s before retry"
            )
            await asyncio.sleep(backoff)
        except Exception as e:
            # Network errors / other transient issues — retry too, with
            # the same backoff. Lets us ride out brief Discord outages.
            last_err = e
            backoff = LOGIN_BACKOFF_SECONDS[min(attempt, len(LOGIN_BACKOFF_SECONDS) - 1)]
            log.warning(
                f"Discord login error (attempt {attempt + 1}/{LOGIN_MAX_ATTEMPTS}): "
                f"{type(e).__name__}: {e}; backing off {backoff}s"
            )
            await asyncio.sleep(backoff)
    else:
        log.error(f"Discord login exhausted {LOGIN_MAX_ATTEMPTS} attempts; last error: {last_err}")
        raise RuntimeError(f"Discord login retry exhausted: {last_err}")

    # Login succeeded; enter the Discord event loop.
    await bot.connect()


if __name__ == "__main__":
    asyncio.run(main())
