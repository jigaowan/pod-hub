from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    base_url: str = Field(alias="BASE_URL")
    fetch_batch_size: int = Field(default=3, alias="FETCH_BATCH_SIZE")
    sync_interval: int = Field(default=30, alias="SYNC_INTERVAL")
    media_dir: Path = Field(default=Path("/data/media"), alias="MEDIA_DIR")
    database_path: Path = Field(default=Path("/data/db/podhub.db"), alias="DATABASE_PATH")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    ytdlp_bin: str = Field(default="yt-dlp", alias="YTDLP_BIN")
    ytdlp_proxy: str | None = Field(default=None, alias="YTDLP_PROXY")
    tmp_media_dir: Path = Field(default=Path("/tmp/pod-hub"), alias="TMP_MEDIA_DIR")

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("BASE_URL must include a valid scheme and host")
        return normalized

    @field_validator("fetch_batch_size", "sync_interval")
    @classmethod
    def validate_positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("value must be a positive integer")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("ytdlp_proxy")
    @classmethod
    def normalize_proxy(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def ensure_runtime_dirs(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_media_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
