from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, populated from environment variables / .env."""

    database_url: str = "postgresql+asyncpg://patchwatch:patchwatch@localhost:5432/patchwatch"

    # Background scheduler: check all sources every N hours, independent of visitors.
    fetch_interval_hours: float = 6.0
    # Kick off one fetch shortly after the app container starts.
    fetch_on_startup: bool = True
    # Debounce for the "check on page load" behaviour, so ten visitors in a row
    # don't start ten scrapes.
    min_refresh_interval_minutes: float = 15.0

    request_timeout_seconds: float = 30.0
    http_user_agent: str = (
        "WindowsPatchWatch/1.0 (+https://github.com/Jteve-Sobs/WindowsPatchWatch)"
    )

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
