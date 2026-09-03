"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Core
    db_url: str = Field("sqlite:///workspace/snsauto.db", alias="SNSAUTO_DB_URL")
    workspace: Path = Field(Path("workspace"), alias="SNSAUTO_WORKSPACE")

    # LLM
    anthropic_api_key: str | None = Field(None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field("claude-opus-5", alias="SNSAUTO_LLM_MODEL")

    # YouTube
    youtube_api_key: str | None = Field(None, alias="YOUTUBE_API_KEY")
    youtube_client_id: str | None = Field(None, alias="YOUTUBE_CLIENT_ID")
    youtube_client_secret: str | None = Field(None, alias="YOUTUBE_CLIENT_SECRET")
    youtube_refresh_token: str | None = Field(None, alias="YOUTUBE_REFRESH_TOKEN")

    # X
    x_bearer_token: str | None = Field(None, alias="X_BEARER_TOKEN")
    x_api_key: str | None = Field(None, alias="X_API_KEY")
    x_api_secret: str | None = Field(None, alias="X_API_SECRET")
    x_access_token: str | None = Field(None, alias="X_ACCESS_TOKEN")
    x_access_token_secret: str | None = Field(None, alias="X_ACCESS_TOKEN_SECRET")

    # Instagram
    ig_user_id: str | None = Field(None, alias="IG_USER_ID")
    ig_access_token: str | None = Field(None, alias="IG_ACCESS_TOKEN")

    # TikTok
    tiktok_client_key: str | None = Field(None, alias="TIKTOK_CLIENT_KEY")
    tiktok_client_secret: str | None = Field(None, alias="TIKTOK_CLIENT_SECRET")
    tiktok_access_token: str | None = Field(None, alias="TIKTOK_ACCESS_TOKEN")

    # Image generation (stills)
    imagegen_provider: str = Field("placeholder", alias="IMAGEGEN_PROVIDER")
    imagegen_api_key: str | None = Field(None, alias="IMAGEGEN_API_KEY")
    imagegen_endpoint: str | None = Field(None, alias="IMAGEGEN_ENDPOINT")

    # How each shot's picture is produced: still | animate | footage | auto
    visual_mode: str = Field("still", alias="SNSAUTO_VISUAL_MODE")

    # Image -> video generation (the shot actually moves)
    videogen_provider: str = Field("none", alias="VIDEOGEN_PROVIDER")
    videogen_api_key: str | None = Field(None, alias="VIDEOGEN_API_KEY")
    videogen_endpoint: str | None = Field(None, alias="VIDEOGEN_ENDPOINT")
    videogen_status_endpoint: str | None = Field(None, alias="VIDEOGEN_STATUS_ENDPOINT")
    videogen_poll_seconds: float = Field(6.0, alias="VIDEOGEN_POLL_SECONDS")
    videogen_timeout_seconds: float = Field(900.0, alias="VIDEOGEN_TIMEOUT_SECONDS")

    # Real / stock footage
    footage_dir: Path | None = Field(None, alias="SNSAUTO_FOOTAGE_DIR")
    stock_provider: str = Field("none", alias="STOCK_PROVIDER")
    stock_api_key: str | None = Field(None, alias="STOCK_API_KEY")
    stock_endpoint: str | None = Field(None, alias="STOCK_ENDPOINT")

    # Narration (text to speech)
    tts_provider: str = Field("none", alias="TTS_PROVIDER")
    tts_api_key: str | None = Field(None, alias="TTS_API_KEY")
    tts_endpoint: str | None = Field(None, alias="TTS_ENDPOINT")
    tts_voice: str | None = Field(None, alias="TTS_VOICE")
    tts_speed: float = Field(1.0, alias="TTS_SPEED")
    # Fallback pacing when no TTS runs: Japanese narration reads ~7 chars/sec.
    tts_chars_per_sec: float = Field(7.0, alias="TTS_CHARS_PER_SEC")

    # Web authentication (required once the UI leaves localhost)
    auth_enabled: bool = Field(False, alias="SNSAUTO_AUTH_ENABLED")
    secret_key: str | None = Field(None, alias="SNSAUTO_SECRET_KEY")
    session_hours: int = Field(12, alias="SNSAUTO_SESSION_HOURS")
    cookie_secure: bool = Field(True, alias="SNSAUTO_COOKIE_SECURE")

    # Worker
    worker_interval_sec: float = Field(60.0, alias="SNSAUTO_WORKER_INTERVAL")
    # Snapshot cadence after publication: dense early, sparse later.
    metric_schedule_hours: tuple[float, ...] = (1, 3, 6, 12, 24, 48, 72, 168)

    def ensure_workspace(self) -> Path:
        for sub in ("", "renders", "assets", "reports", "cache"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        return self.workspace


@lru_cache
def get_settings() -> Settings:
    return Settings()
