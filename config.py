"""Central configuration loaded from environment variables."""

from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Dropbox
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_folder_path: str = "/InstitutionalResearch"

    # Google Gemini
    google_api_key: str = ""
    gemini_model: str = "gemini-3.1-lite"
    gemini_triage_model: str = "gemini-3.1-lite"
    gemini_max_tokens: int = 4096
    gemini_max_concurrent: int = 5

    # Discord
    discord_bot_token: str = ""
    discord_channel_id: int = 0

    # Scheduling
    timezone: str = "America/New_York"
    morning_pulse_hour: int = 8
    morning_pulse_minute: int = 30
    afternoon_pulse_hour: int = 15
    afternoon_pulse_minute: int = 0
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

    # Source priority list (comma-separated, highest priority first)
    high_priority_sources: str = "Goldman Sachs,JPMorgan,Morgan Stanley,Bank of America,Citadel,Bridgewater,UBS,Barclays"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def priority_source_list(self) -> list[str]:
        return [s.strip().lower() for s in self.high_priority_sources.split(",") if s.strip()]


settings = Settings()
