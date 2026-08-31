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

    # Image generation
    imagegen_provider: str = Field("placeholder", alias="IMAGEGEN_PROVIDER")
    imagegen_api_key: str | None = Field(None, alias="IMAGEGEN_API_KEY")
    imagegen_endpoint: str | None = Field(None, alias="IMAGEGEN_ENDPOINT")

    def ensure_workspace(self) -> Path:
        for sub in ("", "renders", "assets", "reports", "cache"):
            (self.workspace / sub).mkdir(parents=True, exist_ok=True)
        return self.workspace


@lru_cache
def get_settings() -> Settings:
    return Settings()
