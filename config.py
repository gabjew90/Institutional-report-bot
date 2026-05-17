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
    # Optional separate API key used ONLY for /ask + @mention Gemini calls.
    # Falls back to google_api_key when empty. Useful when /ask should run on
    # a free-tier Google AI Studio account while the PDF pipeline keeps using
    # a paid-tier key on a different account. Same access shape; just a
    # different billing/quota bucket.
    google_ask_api_key: str = ""
    # Optional model override for /ask only. Empty = use settings.gemini_model.
    # Set to e.g. "gemini-2.5-flash" if the primary model is preview-only and
    # not available on the free-tier ask key.
    ask_gemini_model: str = ""

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

    # Analyst alert-log watcher. When a message is posted in
    # analyst_channel_name with image attachments, the watcher OCRs each
    # image via Gemini and writes a structured row to analyst_trades.
    # See analyst_log/ for the implementation. Empty = watcher disabled.
    analyst_channel_name: str = ""
    # Optional username filter. If set, only messages from this Discord
    # username (matched case-insensitively against author.name) get
    # OCR'd. Empty = log every image posted in the channel. Useful when
    # the analyst's channel occasionally has posts from co-admins that
    # aren't his trade calls.
    analyst_primary_author: str = ""
    # How many days past expiry to keep trade rows before hard-deleting.
    # The daily auto-expire job marks rows as expired_unknown; the weekly
    # purge job (Sundays 04:30 local) deletes rows whose expiry was more
    # than this many days ago. Set to 0 to disable purging entirely.
    analyst_trade_retention_days: int = 14

    # User-profile system: comma-separated channel names where the bot
    # scans for personality signal during weekly profile refresh. Default
    # is the two "yapping" channels — high message volume, lots of
    # personality. Empty = profile system disabled (no scheduled refresh).
    profile_channels: str = "💬-stonks-yapping-💬,₿-crypto-yapping-₿"
    # How many days of history to use for each refresh pass.
    profile_window_days: int = 30
    # Hard cap on total profiles (top N by message count). 0 = no cap;
    # use the message-count threshold instead. Defaulted to 0 since the
    # threshold approach is cleaner — it adapts to user activity rather
    # than imposing an arbitrary count limit.
    max_user_profiles: int = 0
    # Minimum messages a user must have in the refresh window to be
    # profiled. Below this they're treated as lurkers and skipped — the
    # profile would be generic guesswork without enough signal. Default
    # 100 over the 30-day window ≈ 3 messages/day, which is the rough
    # cutoff for "established regular vs casual passer-by." Adjust based
    # on how many profiles you want to maintain.
    profile_min_messages: int = 100
    # How many of each user's most-recent messages to feed into Gemini
    # for profile generation. Higher = richer profile but more tokens.
    # At 500 even a 4000+ msg/week heavy yapper gets ~3 days of context
    # captured (vs ~6-12 hours at the prior 100). Token cost is still
    # trivial (~$0.01 per user).
    profile_sample_size: int = 500
    # Delta-based skip threshold for the daily refresh: a user with an
    # existing profile is only re-profiled if they have at least this
    # many NEW messages (timestamp > stored last_seen_message_at) since
    # their last profile. Below the threshold, the existing profile is
    # treated as still-fresh and the user is skipped. Brand-new users
    # (no profile yet) bypass this check and use profile_min_messages
    # as their cold-start gate. Lower = more frequent refreshes but more
    # token spend; higher = less drift-tracking but cheaper. 50 ≈ ~1.5
    # days of new material for a moderately active yapper.
    profile_delta_threshold: int = 50
    # Image OCR for user profiles. When True, the backfill downloads up
    # to profile_image_cap most-recent images per user and sends them
    # alongside the text to Gemini (multipart). Vision extracts specific
    # tickers, dollar PnLs, and positions from screenshots that the
    # text-only path leaves as generic "[image]" markers. Costs ~2x more
    # per profile than text-only (~$4-5/month total at 37 profiled users
    # vs ~$2/month text-only); validated against a live BK probe that
    # surfaced MSTR-specific quotes and a $4,500 PnL from screenshots.
    profile_image_ocr_enabled: bool = True
    profile_image_cap: int = 20
    # Channel name where the watcher posts log-change announcements
    # ("📝 Logged: abe CLOSE NVDA 150C 5/29 ..."). Same announcements
    # are also used by the daily auto-expire cron. Empty = no announcements
    # (DB writes still happen).
    analyst_test_announce_channel: str = "test"

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
