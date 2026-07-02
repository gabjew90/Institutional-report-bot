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

    # FRED / St. Louis Fed (optional — free key from
    # fred.stlouisfed.org → My Account → API Keys). Extends the economic
    # calendar beyond ForexFactory's this-week window (US release dates
    # weeks ahead) and fills actual printed values for released US data.
    # Added 2026-06-13 after Finnhub gated /calendar/economic behind a
    # paid entitlement. Empty = FRED layer dormant.
    fred_api_key: str = ""

    # Daily query cap per Discord user for /ask + @mention. Resets at UTC
    # midnight. Set to 0 to disable the cap (not recommended).
    # The /ask backend is Gemini with Google Search grounding (reuses
    # GOOGLE_API_KEY); free-tier is 5000 grounded prompts/month, shared
    # across the Google AI Studio account.
    ask_daily_quota_per_user: int = 40
    # gemini-3.1-flash-lite (GA). Previously defaulted to a non-existent
    # "gemini-3.1-lite" alias — that name 404s on the v1beta API; only
    # downstream paths that overrode via env var were working. Preview
    # variants (-preview suffix) are deprecated for our use case and
    # have historically introduced silent regressions when Google
    # rolls them forward.
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_triage_model: str = "gemini-3.1-flash-lite"
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

    # Analyst alert-log watcher. When a message is posted in any
    # configured caller's channel with image attachments, the watcher
    # OCRs each image via Gemini and writes a structured row to
    # analyst_trades. See analyst_log/ for the implementation.
    #
    # LEGACY single-caller config — still honored as a fallback when
    # analyst_callers (below) is empty. If you set both, the registry
    # takes precedence and these are ignored.
    analyst_channel_name: str = ""
    analyst_primary_author: str = ""
    # How many days past expiry to keep trade rows before hard-deleting.
    # The daily auto-expire job marks rows as expired_unknown; the weekly
    # purge job (Sundays 04:30 local) deletes rows whose expiry was more
    # than this many days ago. Set to 0 to disable purging entirely.
    analyst_trade_retention_days: int = 14
    # Multi-caller registry. Each caller dict carries:
    #   name     — canonical lowercase ID stored in analyst_trades.caller
    #              and used for filtering query functions. NEVER change
    #              an existing caller's name without migrating the DB.
    #   display  — human-readable name used in /ask context blocks
    #              ("ABE'S RECENT TRADES" etc).
    #   username — Discord username (author.name) to match for incoming
    #              messages. Case-insensitive.
    #   channel  — channel name where this caller's alerts appear. Bot
    #              listens on this exact channel name.
    #   enabled  — when False, the caller is wired up but messages are
    #              ignored. Useful for staged rollout — add a caller,
    #              verify infra, then flip to true.
    #
    # Each call to `_resolve_analyst_callers()` returns this list with
    # the legacy single-caller config synthesized in as a fallback.
    #   announce_channel — optional override for where this caller's
    #              log embeds get posted. If unset, falls back to the
    #              global `analyst_test_announce_channel`. Useful when
    #              a caller should announce to a dedicated channel
    #              (e.g. f.jamal's own test-channel sandbox).
    analyst_callers: list[dict] = [
        {
            "name": "abe",
            "display": "Abe",
            "username": "abullish_xyz",
            "channel": "🥷🏽-abe-alerts-🥷🏽",
            "enabled": True,
        },
        {
            "name": "bankerkyle",
            "display": "BK",
            "username": "bankerkyle",
            "channel": "💅🏾-kyle-alerts-💅🏾",
            "enabled": True,
        },
        {
            "name": "f.jamal",
            "display": "Jamal",
            "username": "f.jamal",
            "channel": "test-channel",
            "announce_channel": "test-channel",
            # Disabled 2026-05-28 — Jamal was a test caller; /ask
            # was showing his RECENT TRADES block in every response
            # alongside Abe and BK. Removing him from /ask context
            # while keeping the config intact in case he comes back.
            # Effect: analyst_log watcher no longer scans his
            # channel; chat_messages ingestion of test-channel
            # stops too (unless test-channel is added to
            # chat_ingestion_channels or chat_eager_ocr_channels
            # explicitly).
            "enabled": False,
        },
    ]

    # Chat-message ingestion: comma-separated channel names whose
    # messages get persisted to chat_messages. Empty string = derive
    # from profile_channels + analyst_callers' channels (the union of
    # every channel the bot already cares about). Used by:
    #   - /ask verbatim quote lookups (find_user_messages_matching)
    #   - Future: profile-refresh pipeline (read local DB instead of
    #     re-scanning Discord history every cron)
    #   - Future: claim-verification helpers in the bot's responses
    chat_ingestion_channels: str = ""

    # Retention for chat_messages — daily cron deletes rows older than
    # this many days to keep the table bounded. 0 = no purge (table
    # grows forever). 180 default = ~6 months of history retained,
    # plenty for profile-refresh windows + claim verification without
    # bloating the DB.
    chat_retention_days: int = 180

    # Comma-separated channel names where ingest_message OCRs image
    # attachments immediately (Phase 2) rather than waiting for /ask
    # to lazily fetch them. Targeted at channels where image content
    # carries the primary signal (gain/loss screenshots, charts, etc.)
    # and is worth surfacing to downstream consumers ASAP.
    #
    # Channels in this list are auto-added to the chat ingestion union
    # (no need to also list them in chat_ingestion_channels). OCR runs
    # as a background asyncio task so on_message returns immediately.
    chat_eager_ocr_channels: str = (
        # Closed-trade P&L screenshots (everyone posts here)
        "💲-gain-loss-porn-💲,"
        # Caller-owned alert channels (1:1 user → channel — every post is
        # an entry commitment from that user, structurally no-cherry-pick)
        "🦉-kloh-alerts-🦉,"
        "🫦-zhawk-thawghts-🗣,"
        # Shared alert channels (multiple posters; each post is still an
        # entry commitment, just not 1:1 to a single caller)
        "🕰️-member-alerts-🕰️,"
        "🐄-spot-bag-alerts-🐄,"
        "🚨-0dte-lotto-alerts-🚨,"
        "🪙-crypto-alerts-🪙"
    )

    # Per-/ask cap on lazy OCR — how many image-bearing messages will
    # be OCR'd inline during a single /ask. Each OCR call adds ~1-3s
    # latency. Cache hits don't count toward this cap (already done).
    ask_image_ocr_max_per_call: int = 3

    # Backpressure cap on EAGER OCR — the live-on_message path spawns
    # one asyncio.create_task per image attachment. Without a cap, a
    # burst of 50+ images (common during open / close / earnings) would
    # fire 50+ concurrent Gemini OCR calls and either trip rate limits
    # or starve other Gemini work (PDF analysis, /ask). The semaphore
    # caps in-flight eager OCR at this value — tasks queue rather than
    # being dropped, so coverage is preserved.
    eager_ocr_max_concurrent: int = 3

    # How long Discord CDN attachment URLs stay fresh after issuance.
    # The OCR helper uses this to decide whether to attempt the direct
    # URL first or skip straight to channel.fetch_message() for a
    # fresh signed URL. Discord's current expiry is 24h; we use a
    # conservative 20h to leave headroom for clock skew.
    chat_attachment_url_freshness_hours: int = 20

    # Default chat-ingestion channels — the bot persists messages from
    # these into chat_messages by default (when chat_ingestion_channels
    # is not set as an override). Four "yapping" rooms + four alert
    # rooms, all the channels the bot already cares about.
    #
    # Historically this field also scoped the profile-builder to these
    # 8 channels. That filter was removed: the profile builder now
    # reads ALL chat_messages (including image_ocr_text), so this field
    # only controls ingestion defaults today. Kept the name for
    # backward-compat with the PROFILE_CHANNELS env var on existing
    # Railway deploys.
    profile_channels: str = (
        "💬-stonks-yapping-💬,"
        "₿-crypto-yapping-₿,"
        "🏃-fitness-yapping-🏋,"
        "🎲-gambling-yapping-🎲,"
        "🦉-kloh-alerts-🦉,"
        "🫦-zhawk-thawghts-🗣,"
        "🕰️-member-alerts-🕰️,"
        "🐄-spot-bag-alerts-🐄,"
        "🚨-0dte-lotto-alerts-🚨,"
        "🪙-crypto-alerts-🪙"
    )
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
    profile_min_messages: int = 30
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
    # token spend; higher = less drift-tracking but cheaper.
    #
    # Lowered 50 → 20 because at 50 active callers who post mostly in
    # their own caller channels (not in profile_channels) were going 2+
    # days without a refresh — profile_channels covers yapping channels
    # but not caller alert channels, so caller activity didn't count
    # toward delta. 20 = ~half a day of typical yapping for an active
    # member, catches drift sooner without much extra token spend.
    profile_delta_threshold: int = 20
    # Image OCR for user profiles. When True, the backfill downloads up
    # to profile_image_cap most-recent images per user and sends them
    # alongside the text to Gemini (multipart). Vision extracts specific
    # tickers, dollar PnLs, and positions from screenshots that the
    # text-only path leaves as generic "[image]" markers.
    #
    # DISABLED 2026-05-18 — vision A/B showed truncation issues.
    # Note: the original comment claimed gemini-3.1-flash-lite (GA) was
    # strictly worse than -preview for long structured output; that was
    # corrected on 2026-05-28 after re-testing both models. Both produce
    # full 8K-char structured output now. The vision-OFF preference
    # remains for cost / token-output simplicity, NOT because the GA
    # model is broken. The image-OCR code path stays intact in case
    # it's re-enabled later.
    profile_image_ocr_enabled: bool = False
    profile_image_cap: int = 20
    # Vision-capable model — currently UNUSED (profile_image_ocr_enabled
    # is False). Kept as configuration so re-enabling vision is a
    # one-line setting flip rather than a code change.
    gemini_vision_model: str = "gemini-3.1-flash-lite"
    # Channel name where the watcher posts log-change announcements
    # ("📝 Logged: abe CLOSE NVDA 150C 5/29 ..."). Same announcements
    # are also used by the daily auto-expire cron. Empty = no announcements
    # (DB writes still happen).
    analyst_test_announce_channel: str = "test"

    # Scheduling
    timezone: str = "America/New_York"
    # Scheduled pulse fires at this time in the configured timezone
    daily_pulse_hour: int = 10  # 10 AM ET (moved from 9, 2026-07-02 — jobs-day actuals land pre-pulse)
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

    # Channel reminder system — the daily 3:45 PM ET job posts due
    # calendar reminders (reminders/calendar.json) to this channel.
    # Empty = reminders disabled. Single channel id (not comma list).
    reminder_channel_id: str = ""
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

    def resolve_analyst_callers(self) -> list[dict]:
        """Return the effective list of analyst-caller dicts.

        Behavior:
        - If `analyst_callers` is non-empty, return entries with `enabled=True`.
        - Otherwise (legacy single-caller deployment): synthesize a one-entry
          list from `analyst_channel_name` + `analyst_primary_author`. Both
          must be set for the legacy fallback to fire; otherwise return [].

        The returned dicts always have the four required keys present:
        name, display, username, channel. `enabled` is True for all
        returned entries (already filtered).
        """
        if self.analyst_callers:
            out = []
            for c in self.analyst_callers:
                if not c.get("enabled", True):
                    continue
                # Defensive: ensure all required keys are populated
                if not all(c.get(k) for k in ("name", "username", "channel")):
                    continue
                announce = c.get("announce_channel")
                out.append({
                    "name": str(c["name"]).strip().lower(),
                    "display": str(c.get("display") or c["name"]),
                    "username": str(c["username"]).strip(),
                    "channel": str(c["channel"]).strip(),
                    "announce_channel": (
                        str(announce).strip() if announce else None
                    ),
                    "enabled": True,
                })
            return out
        # Legacy fallback: synthesize from the two old fields if both set
        if self.analyst_channel_name and self.analyst_primary_author:
            return [{
                "name": self.analyst_primary_author.strip().lower(),
                "display": self.analyst_primary_author,
                "username": self.analyst_primary_author.strip(),
                "channel": self.analyst_channel_name.strip(),
                "enabled": True,
            }]
        return []

    def resolve_chat_ingestion_channels(self) -> set[str]:
        """Channel names to ingest into chat_messages.

        Behavior:
          - If chat_ingestion_channels is set (comma-separated), use that
            verbatim.
          - Otherwise default to the union of profile_channels and every
            registered analyst caller's channel — every channel the bot
            already cares about. New caller channels and profile channels
            get ingested automatically without a config change.
        """
        if self.chat_ingestion_channels.strip():
            return {
                s.strip() for s in self.chat_ingestion_channels.split(",")
                if s.strip()
            }
        channels: set[str] = set()
        for raw in (self.profile_channels or "").split(","):
            raw = raw.strip()
            if raw:
                channels.add(raw)
        for c in self.resolve_analyst_callers():
            ch = (c.get("channel") or "").strip()
            if ch:
                channels.add(ch)
        # Eager-OCR channels are also auto-included — they need their
        # messages stored so the OCR pass has a chat_messages row to
        # write back to.
        for raw in (self.chat_eager_ocr_channels or "").split(","):
            raw = raw.strip()
            if raw:
                channels.add(raw)
        return channels

    def resolve_chat_eager_ocr_channels(self) -> set[str]:
        """Channel names that get image attachments OCR'd at ingest time
        (Phase 2). Empty by default for everywhere else; lazy OCR
        handles the rest on demand.
        """
        return {
            s.strip() for s in (self.chat_eager_ocr_channels or "").split(",")
            if s.strip()
        }

    def caller_by_channel(self, channel_name: str) -> dict | None:
        """Look up which caller owns this channel name (case-insensitive)."""
        if not channel_name:
            return None
        chan_lower = channel_name.strip().lower()
        for c in self.resolve_analyst_callers():
            if c["channel"].lower() == chan_lower:
                return c
        return None

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
