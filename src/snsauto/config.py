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

    # Facebook app behind Instagram publishing (the API is only reachable
    # through a Facebook app with an Instagram Business account attached).
    facebook_app_id: str | None = Field(None, alias="FACEBOOK_APP_ID")
    facebook_app_secret: str | None = Field(None, alias="FACEBOOK_APP_SECRET")

    # Refresh a token this long before it expires. TikTok's is 24h, so the
    # window has to be comfortably inside that.
    token_refresh_margin_hours: float = Field(6.0, alias="SNSAUTO_TOKEN_REFRESH_MARGIN")

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

    # ---- Competitor research ----
    # Collection shaping. The default population is "the 50 most relevant
    # results", which skews old on YouTube; these narrow it deliberately.
    research_published_within_days: int | None = Field(
        None, alias="SNSAUTO_RESEARCH_WITHIN_DAYS"
    )
    research_video_duration: str | None = Field(
        None, alias="SNSAUTO_RESEARCH_DURATION"      # short | medium | long
    )
    research_order: str = Field("relevance", alias="SNSAUTO_RESEARCH_ORDER")
    research_min_views: int = Field(0, alias="SNSAUTO_RESEARCH_MIN_VIEWS")
    # Comma-separated author handles to drop - your own accounts, mostly.
    research_exclude_authors: str | None = Field(
        None, alias="SNSAUTO_RESEARCH_EXCLUDE_AUTHORS"
    )
    research_exclude_pattern: str | None = Field(
        None, alias="SNSAUTO_RESEARCH_EXCLUDE_PATTERN"
    )

    # Telop / frame analysis
    telop_reader: str = Field("auto", alias="SNSAUTO_TELOP_READER")  # auto|tesseract|vision|off
    telop_interval_sec: float = Field(0.8, alias="SNSAUTO_TELOP_INTERVAL")
    telop_max_frames: int = Field(45, alias="SNSAUTO_TELOP_MAX_FRAMES")
    telop_vision_budget: int = Field(6, alias="SNSAUTO_TELOP_VISION_BUDGET")

    # Competitor video acquisition. Downloading another account's video is
    # governed by each platform's terms of service, so there is no default
    # command: the operator sets one deliberately or the feature stays off.
    video_fetch_cmd: str | None = Field(None, alias="SNSAUTO_VIDEO_FETCH_CMD")
    video_fetch_timeout: float = Field(300.0, alias="SNSAUTO_VIDEO_FETCH_TIMEOUT")

    # Comment mining
    comment_fetch_limit: int = Field(50, alias="SNSAUTO_COMMENT_LIMIT")

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

    # Public asset hosting (required for Instagram publishing)
    storage_backend: str = Field("none", alias="STORAGE_BACKEND")  # s3 | local | none
    public_base_url: str | None = Field(None, alias="SNSAUTO_PUBLIC_BASE_URL")
    s3_bucket: str | None = Field(None, alias="S3_BUCKET")
    s3_endpoint_url: str | None = Field(None, alias="S3_ENDPOINT_URL")
    s3_region: str | None = Field(None, alias="S3_REGION")
    s3_access_key: str | None = Field(None, alias="S3_ACCESS_KEY")
    s3_secret_key: str | None = Field(None, alias="S3_SECRET_KEY")
    s3_public_base_url: str | None = Field(None, alias="S3_PUBLIC_BASE_URL")
    s3_expires_in: int = Field(21600, alias="S3_EXPIRES_IN")

    # Failure notifications
    alert_email_to: str | None = Field(None, alias="ALERT_EMAIL_TO")
    smtp_host: str | None = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: str | None = Field(None, alias="SMTP_USER")
    smtp_password: str | None = Field(None, alias="SMTP_PASSWORD")
    smtp_from: str | None = Field(None, alias="SMTP_FROM")
    smtp_starttls: bool = Field(True, alias="SMTP_STARTTLS")

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
