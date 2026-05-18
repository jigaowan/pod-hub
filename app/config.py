from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from pydantic import Field, field_validator, model_validator
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
    ytdlp_js_runtimes: str | None = Field(default=None, alias="YTDLP_JS_RUNTIMES")
    ytdlp_socket_timeout: int = Field(default=30, alias="YTDLP_SOCKET_TIMEOUT")
    ytdlp_retries: int = Field(default=5, alias="YTDLP_RETRIES")
    ytdlp_fragment_retries: int = Field(default=5, alias="YTDLP_FRAGMENT_RETRIES")
    tmp_media_dir: Path = Field(default=Path("/tmp/pod-hub"), alias="TMP_MEDIA_DIR")
    alarm_url: str | None = Field(default=None, alias="ALARM_URL")
    alarm_custom_psk: str | None = Field(default=None, alias="ALARM_CUSTOM_PSK")
    alarm_tg_user_id: str | None = Field(default=None, alias="ALARM_TG_USER_ID")
    alarm_timeout_seconds: int = Field(default=10, alias="ALARM_TIMEOUT_SECONDS")
    cloudflare_ai_gateway_url: str | None = Field(default=None, alias="CLOUDFLARE_AI_GATEWAY_URL")
    cloudflare_api_token: str | None = Field(default=None, alias="CLOUDFLARE_API_TOKEN")

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

    @field_validator("ytdlp_socket_timeout", "ytdlp_retries", "ytdlp_fragment_retries")
    @classmethod
    def validate_non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("value must be a non-negative integer")
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

    @field_validator("ytdlp_js_runtimes")
    @classmethod
    def normalize_js_runtimes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("alarm_url")
    @classmethod
    def normalize_alarm_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("ALARM_URL must include a valid scheme and host")
        return normalized

    @field_validator("cloudflare_ai_gateway_url")
    @classmethod
    def normalize_cloudflare_ai_gateway_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        if not normalized:
            return None
        cls.build_cloudflare_whisper_url(normalized)
        return normalized

    @field_validator("alarm_custom_psk", "alarm_tg_user_id")
    @classmethod
    def normalize_optional_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("alarm_timeout_seconds")
    @classmethod
    def validate_alarm_timeout_seconds(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("ALARM_TIMEOUT_SECONDS must be a positive integer")
        return value

    @model_validator(mode="after")
    def validate_alarm_config(self) -> "Settings":
        alarm_values = (self.alarm_url, self.alarm_custom_psk, self.alarm_tg_user_id)
        configured = [value is not None for value in alarm_values]
        if any(configured) and not all(configured):
            raise ValueError("ALARM_URL, ALARM_CUSTOM_PSK, and ALARM_TG_USER_ID must be set together")
        cloudflare_values = (self.cloudflare_ai_gateway_url, self.cloudflare_api_token)
        cloudflare_configured = [value is not None for value in cloudflare_values]
        if any(cloudflare_configured) and not all(cloudflare_configured):
            raise ValueError("CLOUDFLARE_AI_GATEWAY_URL and CLOUDFLARE_API_TOKEN must be set together")
        return self

    @property
    def transcription_configured(self) -> bool:
        return self.cloudflare_ai_gateway_url is not None and self.cloudflare_api_token is not None

    @property
    def cloudflare_whisper_url(self) -> str | None:
        if self.cloudflare_ai_gateway_url is None:
            return None
        return self.build_cloudflare_whisper_url(self.cloudflare_ai_gateway_url)

    @staticmethod
    def build_cloudflare_whisper_url(gateway_url: str) -> str:
        parsed = urlparse(gateway_url.strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"} or parsed.netloc != "gateway.ai.cloudflare.com":
            raise ValueError("CLOUDFLARE_AI_GATEWAY_URL must be a Cloudflare AI Gateway URL")

        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "v1":
            raise ValueError("CLOUDFLARE_AI_GATEWAY_URL must include /v1/{account}/{gateway}")

        account_id = parts[1]
        gateway_id = parts[2]
        if not account_id or not gateway_id:
            raise ValueError("CLOUDFLARE_AI_GATEWAY_URL must include account and gateway ids")

        whisper_path = f"/v1/{account_id}/{gateway_id}/workers-ai/@cf/openai/whisper-large-v3-turbo"
        return urlunparse((parsed.scheme, parsed.netloc, whisper_path, "", "", ""))

    def ensure_runtime_dirs(self) -> None:
        self.media_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_media_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_runtime_dirs()
    return settings
