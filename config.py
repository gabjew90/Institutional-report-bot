"""Central configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Dropbox
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_folder_path: str = "/Current"

    # Google Gemini
    google_api_key: str = ""

    # Finnhub (optional — enables live market news + economic calendar)
    finnhub_api_key: str = ""

    # Daily query cap per Discord user for /ask + @mention. Resets at UTC
    # midnight. Set to 0 to disable the cap (not recommended).
    # The /ask backend is Gemini with Google Search grounding (reuses
    # GOOGLE_API_KEY); free-tier is 5000 grounded prompts/month, shared
    # across the Google AI Studio account.
    ask_daily_quota_per_user: int = 20
    gemini_model: str = "gemini-3.1-lite"
    gemini_triage_model: str = "gemini-3.1-lite"
    gemini_max_tokens: int = 4096
    gemini_max_concurrent: int = 5

    # Discord
    discord_bot_token: str = ""
    # Comma-separated list of channel IDs. Single ID also works (e.g. "123").
    # The scheduled pulse will be posted to every channel listed here.
    discord_channel_id: str = ""
    # Password guard for destructive / token-heavy commands (/load, /reanalyze).
    # Empty = no gate; any string = required to match the `password` slash arg.
    command_password: str = ""
    # Comma-separated channel NAMES (not IDs) where pulse/admin slash commands
    # are allowed. Applies to /pulse, /load, /reanalyze, /clearqueue,
    # /seedcursor, /status, /reprocess. /ask is intentionally NOT gated.
    # Empty = no restriction (commands work in every channel).
    pulse_command_channels: str = "test,tldr"

    # Scheduling
    timezone: str = "America/New_York"
    # Scheduled pulse fires at this time in the configured timezone
    daily_pulse_hour: int = 9  # 9 AM ET
    daily_pulse_minute: int = 0
    dropbox_poll_interval_minutes: int = 15
    process_interval_minutes: int = 5

    # Processing
    max_pages_per_pdf: int = 8
    image_dpi: int = 200
    max_retry_count: int = 3
    pdf_download_dir: str = "data/pdfs"
    db_path: str = "data/reports.db"

    # Logging
    log_level: str = "INFO"

    # HTTP API (used by the Opus routine to fetch pulse context + post results)
    # Railway sets $PORT automatically; this is the fallback for local dev.
    http_port: int = Field(default=8080, alias="PORT")
    # Random shared secret embedded in routine prompts. Empty = HTTP API disabled.
    pulse_api_token: str = ""

    # GitHub-as-bridge config — used because the cloud Claude Code sandbox's
    # egress allowlist blocks Railway and Discord but allows github.com.
    # Empty token = bridge disabled (jobs skip themselves).
    github_token: str = ""
    github_repo: str = "gabjew90/Institutional-report-bot"
    github_bridge_branch: str = "pulse-data"
    bridge_dump_interval_minutes: int = 15
    bridge_post_poll_interval_seconds: int = 60

    # Real-time ingestion feed — posts a Discord embed for each newly-analyzed
    # HIGH/MEDIUM PDF, trickled 1-per-interval. Empty = feed disabled.
    discord_ingest_feed_channel_id: str = ""
    ingest_feed_interval_seconds: int = 60
    # Startup backlog threshold: if more than N HIGH/MEDIUM PDFs are queued
    # for announcement at boot, post a single summary card instead of trickling
    # them all. Treats reboots after extended downtime as "backfill" not "live".
    ingest_feed_backlog_threshold: int = 20

    # HIGH-priority deep analysis backend. Default is the existing Gemini path.
    # Set to "opus_bridge" to route HIGH PDFs through the parallel Opus routine
    # bridge (committed to GitHub, processed by an Anthropic-side cron routine,
    # results pulled back into the same pdf_analyses table). Auto-falls-back to
    # Gemini per-PDF if the bridge stalls > opus_bridge_timeout_minutes.
    # Acceptable values: "gemini" | "opus_bridge"
    high_ingestion_backend: str = "gemini"
    opus_bridge_timeout_minutes: int = 30
    # PDFs over these limits skip the bridge and go straight to Gemini fallback.
    # Anthropic's PDF Read tool caps at ~100 pages / ~32MB.
    opus_bridge_max_pages: int = 80
    opus_bridge_max_size_mb: int = 30

    # Source priority list (comma-separated, highest priority first)
    # Tier 1 sources — always HIGH priority regardless of content
    tier1_sources: str = "Goldman Sachs,JPMorgan,Bank of America,Morgan Stanley"
    # Tier 2 sources — HIGH if topic matches, otherwise MEDIUM
    tier2_sources: str = "Citi,UBS,Barclays,Deutsche Bank,The Market Ear,Bernstein,RBC"
    # HIGH priority topics (applied to all sources)
    high_priority_topics: str = "macro,us_equities,crypto,oil,gold,commodities,derivatives,vol_commentary,morning_briefing,sales_trading,strategy"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def priority_source_list(self) -> list[str]:
        return [s.strip().lower() for s in self.high_priority_sources.split(",") if s.strip()]

    @property
    def pulse_command_channel_names(self) -> list[str]:
        """Lowercase channel-name allowlist for pulse/admin commands.
        Empty list = no restriction.
        """
        return [s.strip().lower() for s in self.pulse_command_channels.split(",") if s.strip()]

    @property
    def discord_channel_ids(self) -> list[int]:
        """Parse DISCORD_CHANNEL_ID (may be a single ID or comma-separated list)."""
        out: list[int] = []
        for raw in self.discord_channel_id.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(int(raw))
            except ValueError:
                continue
        return out


settings = Settings()
