"""Entry point: starts the Discord bot, scheduler, and processing pipeline."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from config import settings


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

    # Start Discord bot (blocks)
    log.info("Starting Discord bot...")
    await bot.start(settings.discord_bot_token)


if __name__ == "__main__":
    asyncio.run(main())
