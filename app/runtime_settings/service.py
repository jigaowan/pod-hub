from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse, urlunparse

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import ValidationError
from app.config import Settings
from app.db.repositories import settings as settings_repo
from app.db.session import get_session_factory


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    base_url: str
    fetch_batch_size: int
    sync_interval: int
    ytdlp_proxy: str | None
    ytdlp_socket_timeout: int
    ytdlp_retries: int
    ytdlp_fragment_retries: int
    alarm_url: str | None
    alarm_custom_psk: str | None
    alarm_tg_user_id: str | None
    alarm_timeout_seconds: int
    cloudflare_ai_gateway_url: str | None
    cloudflare_api_token: str | None

    @property
    def alarm_configured(self) -> bool:
        return bool(self.alarm_url and self.alarm_custom_psk and self.alarm_tg_user_id)

    @property
    def transcription_configured(self) -> bool:
        return self.cloudflare_ai_gateway_url is not None and self.cloudflare_api_token is not None

    @property
    def cloudflare_whisper_url(self) -> str | None:
        if self.cloudflare_ai_gateway_url is None:
            return None
        return build_cloudflare_whisper_url(self.cloudflare_ai_gateway_url)


@dataclass(frozen=True, slots=True)
class SettingFieldView:
    name: str
    key: str
    label: str
    value: str
    source: str
    secret: bool = False
    configured: bool = False


@dataclass(frozen=True, slots=True)
class EnvOnlySettingView:
    key: str
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class RuntimeSettingsAdminView:
    fields: dict[str, SettingFieldView]
    env_only: list[EnvOnlySettingView]


WEB_SETTING_FIELDS = {
    "base_url": ("BASE_URL", "发布地址", False),
    "fetch_batch_size": ("FETCH_BATCH_SIZE", "每轮抓取数量", False),
    "sync_interval": ("SYNC_INTERVAL", "同步间隔（分钟）", False),
    "ytdlp_proxy": ("YTDLP_PROXY", "yt-dlp 代理", False),
    "ytdlp_socket_timeout": ("YTDLP_SOCKET_TIMEOUT", "yt-dlp socket 超时（秒）", False),
    "ytdlp_retries": ("YTDLP_RETRIES", "yt-dlp 重试次数", False),
    "ytdlp_fragment_retries": ("YTDLP_FRAGMENT_RETRIES", "yt-dlp 分片重试次数", False),
    "alarm_url": ("ALARM_URL", "告警 URL", False),
    "alarm_custom_psk": ("ALARM_CUSTOM_PSK", "告警 PSK", True),
    "alarm_tg_user_id": ("ALARM_TG_USER_ID", "Telegram 用户 ID", True),
    "alarm_timeout_seconds": ("ALARM_TIMEOUT_SECONDS", "告警超时（秒）", False),
    "cloudflare_ai_gateway_url": ("CLOUDFLARE_AI_GATEWAY_URL", "Cloudflare AI Gateway URL", False),
    "cloudflare_api_token": ("CLOUDFLARE_API_TOKEN", "Cloudflare API Token", True),
}

WEB_SETTING_KEYS = [spec[0] for spec in WEB_SETTING_FIELDS.values()]
SECRET_FIELD_NAMES = {name for name, spec in WEB_SETTING_FIELDS.items() if spec[2]}
OPTIONAL_FIELD_NAMES = {
    "ytdlp_proxy",
    "alarm_url",
    "alarm_custom_psk",
    "alarm_tg_user_id",
    "cloudflare_ai_gateway_url",
    "cloudflare_api_token",
}
POSITIVE_INT_FIELD_NAMES = {"fetch_batch_size", "sync_interval", "alarm_timeout_seconds"}
NON_NEGATIVE_INT_FIELD_NAMES = {"ytdlp_socket_timeout", "ytdlp_retries", "ytdlp_fragment_retries"}

ENV_ONLY_FIELDS = {
    "DATABASE_PATH": ("数据库路径", "database_path"),
    "MEDIA_DIR": ("媒体目录", "media_dir"),
    "TMP_MEDIA_DIR": ("临时目录", "tmp_media_dir"),
    "LOG_LEVEL": ("日志级别", "log_level"),
    "YTDLP_BIN": ("yt-dlp 可执行文件", "ytdlp_bin"),
    "YTDLP_JS_RUNTIMES": ("yt-dlp JS 运行时", "ytdlp_js_runtimes"),
}


class RuntimeSettingsService:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory or get_session_factory(settings)

    def get_effective_settings(self) -> RuntimeSettings:
        values = self._default_values()
        values.update(self._load_overrides())
        return self._build_runtime_settings(values)

    def get_admin_view(self) -> RuntimeSettingsAdminView:
        effective = self.get_effective_settings()
        overrides = self._load_overrides()

        fields: dict[str, SettingFieldView] = {}
        for name, (key, label, secret) in WEB_SETTING_FIELDS.items():
            value = getattr(effective, name)
            display_value = "" if value is None else str(value)
            fields[name] = SettingFieldView(
                name=name,
                key=key,
                label=label,
                value="" if secret else display_value,
                source="database" if key in overrides else "env",
                secret=secret,
                configured=bool(value),
            )

        env_only = [
            EnvOnlySettingView(
                key=key,
                label=label,
                value=self._display_env_only_value(getattr(self.settings, attr)),
            )
            for key, (label, attr) in ENV_ONLY_FIELDS.items()
        ]
        return RuntimeSettingsAdminView(fields=fields, env_only=env_only)

    def _load_overrides(self) -> dict[str, str]:
        try:
            with self.session_factory() as session:
                return settings_repo.list_settings(session, WEB_SETTING_KEYS)
        except SQLAlchemyError:
            return {}

    def save_form(self, form_data: Mapping[str, object]) -> RuntimeSettings:
        defaults = self._default_values()
        with self.session_factory() as session:
            overrides = settings_repo.list_settings(session, WEB_SETTING_KEYS)

        candidate = dict(defaults)
        candidate.update(overrides)
        to_set: dict[str, str] = {}
        to_delete: set[str] = set()

        for name, (key, _label, secret) in WEB_SETTING_FIELDS.items():
            if secret:
                if self._checkbox_enabled(form_data, f"clear_{name}"):
                    candidate[key] = defaults.get(key)
                    to_delete.add(key)
                    continue

                submitted = self._clean_string(form_data.get(name))
                if submitted:
                    candidate[key] = submitted
                    to_set[key] = submitted
                continue

            submitted = self._clean_string(form_data.get(name))
            if name in OPTIONAL_FIELD_NAMES and submitted is None:
                candidate[key] = defaults.get(key)
                to_delete.add(key)
                continue
            if submitted is None:
                raise ValidationError(f"{key} is required")
            candidate[key] = submitted
            to_set[key] = submitted

        effective = self._build_runtime_settings(candidate)

        with self.session_factory() as session:
            try:
                for key in to_delete:
                    if key not in to_set:
                        settings_repo.delete_setting(session, key)
                for key, value in to_set.items():
                    settings_repo.set_setting(session, key, value)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return effective

    def _default_values(self) -> dict[str, object]:
        return {
            "BASE_URL": self.settings.base_url,
            "FETCH_BATCH_SIZE": self.settings.fetch_batch_size,
            "SYNC_INTERVAL": self.settings.sync_interval,
            "YTDLP_PROXY": self.settings.ytdlp_proxy,
            "YTDLP_SOCKET_TIMEOUT": self.settings.ytdlp_socket_timeout,
            "YTDLP_RETRIES": self.settings.ytdlp_retries,
            "YTDLP_FRAGMENT_RETRIES": self.settings.ytdlp_fragment_retries,
            "ALARM_URL": self.settings.alarm_url,
            "ALARM_CUSTOM_PSK": self.settings.alarm_custom_psk,
            "ALARM_TG_USER_ID": self.settings.alarm_tg_user_id,
            "ALARM_TIMEOUT_SECONDS": self.settings.alarm_timeout_seconds,
            "CLOUDFLARE_AI_GATEWAY_URL": self.settings.cloudflare_ai_gateway_url,
            "CLOUDFLARE_API_TOKEN": self.settings.cloudflare_api_token,
        }

    def _build_runtime_settings(self, values: Mapping[str, object]) -> RuntimeSettings:
        base_url = self._validate_base_url(self._required_string(values, "BASE_URL"))
        fetch_batch_size = self._positive_int(values, "FETCH_BATCH_SIZE")
        sync_interval = self._positive_int(values, "SYNC_INTERVAL")
        ytdlp_proxy = self._optional_string(values.get("YTDLP_PROXY"))
        ytdlp_socket_timeout = self._non_negative_int(values, "YTDLP_SOCKET_TIMEOUT")
        ytdlp_retries = self._non_negative_int(values, "YTDLP_RETRIES")
        ytdlp_fragment_retries = self._non_negative_int(values, "YTDLP_FRAGMENT_RETRIES")
        alarm_url = self._validate_optional_url(values.get("ALARM_URL"), "ALARM_URL")
        alarm_custom_psk = self._optional_string(values.get("ALARM_CUSTOM_PSK"))
        alarm_tg_user_id = self._optional_string(values.get("ALARM_TG_USER_ID"))
        alarm_timeout_seconds = self._positive_int(values, "ALARM_TIMEOUT_SECONDS")
        cloudflare_ai_gateway_url = self._validate_cloudflare_gateway(values.get("CLOUDFLARE_AI_GATEWAY_URL"))
        cloudflare_api_token = self._optional_string(values.get("CLOUDFLARE_API_TOKEN"))

        alarm_values = (alarm_url, alarm_custom_psk, alarm_tg_user_id)
        if any(value is not None for value in alarm_values) and not all(value is not None for value in alarm_values):
            raise ValidationError("ALARM_URL, ALARM_CUSTOM_PSK, and ALARM_TG_USER_ID must be set together")

        cloudflare_values = (cloudflare_ai_gateway_url, cloudflare_api_token)
        if any(value is not None for value in cloudflare_values) and not all(
            value is not None for value in cloudflare_values
        ):
            raise ValidationError("CLOUDFLARE_AI_GATEWAY_URL and CLOUDFLARE_API_TOKEN must be set together")

        return RuntimeSettings(
            base_url=base_url,
            fetch_batch_size=fetch_batch_size,
            sync_interval=sync_interval,
            ytdlp_proxy=ytdlp_proxy,
            ytdlp_socket_timeout=ytdlp_socket_timeout,
            ytdlp_retries=ytdlp_retries,
            ytdlp_fragment_retries=ytdlp_fragment_retries,
            alarm_url=alarm_url,
            alarm_custom_psk=alarm_custom_psk,
            alarm_tg_user_id=alarm_tg_user_id,
            alarm_timeout_seconds=alarm_timeout_seconds,
            cloudflare_ai_gateway_url=cloudflare_ai_gateway_url,
            cloudflare_api_token=cloudflare_api_token,
        )

    def _required_string(self, values: Mapping[str, object], key: str) -> str:
        value = self._optional_string(values.get(key))
        if value is None:
            raise ValidationError(f"{key} is required")
        return value

    def _positive_int(self, values: Mapping[str, object], key: str) -> int:
        value = self._parse_int(values.get(key), key)
        if value <= 0:
            raise ValidationError(f"{key} must be a positive integer")
        return value

    def _non_negative_int(self, values: Mapping[str, object], key: str) -> int:
        value = self._parse_int(values.get(key), key)
        if value < 0:
            raise ValidationError(f"{key} must be a non-negative integer")
        return value

    def _parse_int(self, value: object, key: str) -> int:
        if isinstance(value, int):
            return value
        cleaned = self._optional_string(value)
        if cleaned is None:
            raise ValidationError(f"{key} is required")
        try:
            return int(cleaned)
        except ValueError as exc:
            raise ValidationError(f"{key} must be an integer") from exc

    def _validate_base_url(self, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("BASE_URL must include a valid scheme and host")
        return normalized

    def _validate_optional_url(self, value: object, key: str) -> str | None:
        normalized = self._optional_string(value)
        if normalized is None:
            return None
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError(f"{key} must include a valid scheme and host")
        return normalized

    def _validate_cloudflare_gateway(self, value: object) -> str | None:
        normalized = self._optional_string(value)
        if normalized is None:
            return None
        normalized = normalized.rstrip("/")
        build_cloudflare_whisper_url(normalized)
        return normalized

    def _optional_string(self, value: object) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _clean_string(self, value: object) -> str | None:
        return self._optional_string(value)

    def _checkbox_enabled(self, form_data: Mapping[str, object], key: str) -> bool:
        return str(form_data.get(key, "")).lower() in {"1", "true", "on", "yes"}

    def _display_env_only_value(self, value: object) -> str:
        if isinstance(value, Path):
            return str(value)
        return "" if value is None else str(value)


def build_cloudflare_whisper_url(gateway_url: str) -> str:
    parsed = urlparse(gateway_url.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or parsed.netloc != "gateway.ai.cloudflare.com":
        raise ValidationError("CLOUDFLARE_AI_GATEWAY_URL must be a Cloudflare AI Gateway URL")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[0] != "v1":
        raise ValidationError("CLOUDFLARE_AI_GATEWAY_URL must include /v1/{account}/{gateway}")

    account_id = parts[1]
    gateway_id = parts[2]
    if not account_id or not gateway_id:
        raise ValidationError("CLOUDFLARE_AI_GATEWAY_URL must include account and gateway ids")

    whisper_path = f"/v1/{account_id}/{gateway_id}/workers-ai/@cf/openai/whisper-large-v3-turbo"
    return urlunparse((parsed.scheme, parsed.netloc, whisper_path, "", "", ""))
