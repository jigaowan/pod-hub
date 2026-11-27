from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import Field, PositiveInt, field_validator, model_validator

from podhub.domain.policy import SettingsModel, SourcePolicy


class Paths(SettingsModel):
    state_dir: Path = Path("/var/lib/pod-hub")
    output_dir: Path = Path("/srv/podcasts")

    @model_validator(mode="after")
    def separate(self) -> Self:
        self.state_dir = self.state_dir.expanduser().resolve()
        self.output_dir = self.output_dir.expanduser().resolve()
        state, output = self.state_dir, self.output_dir
        if state.is_relative_to(output) or any(
            output.is_relative_to(state / name) or (state / name).is_relative_to(output)
            for name in ("tmp", "cookies", "backups", "app.db", "app.db-wal", "app.db-shm", "service.lock")
        ):
            raise ValueError("output directory overlaps private state")
        return self


class Web(SettingsModel):
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(default=8000, gt=0, le=65535)
    base_url: str = "http://127.0.0.1:8000"

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("base_url must be an HTTP(S) URL without userinfo, query, or fragment")
        return value.rstrip("/")


class Toolchain(SettingsModel):
    ytdlp_python: str = Field(default="python3", min_length=1)
    ffmpeg_path: str = Field(default="ffmpeg", min_length=1)
    ffprobe_path: str = Field(default="ffprobe", min_length=1)
    js_runtime: str = "deno"
    min_ytdlp_version: str = Field(default="2025.01.01", pattern=r"^\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?$")
    ytdlp_proxy: str = ""
    ytdlp_extractor_args: str = ""


class Pacing(SettingsModel):
    max_concurrent_downloads: PositiveInt = 1
    min_launch_gap_s: float = Field(default=20, ge=0, allow_inf_nan=False)
    launch_jitter_s: float = Field(default=25, ge=0, allow_inf_nan=False)
    backfill_min_gap_s: float = Field(default=60, ge=0, allow_inf_nan=False)
    backfill_jitter_s: float = Field(default=90, ge=0, allow_inf_nan=False)
    backfill_daily_cap: int = Field(default=30, ge=0)
    daily_download_cap: int = Field(default=0, ge=0)
    default_check_interval_s: PositiveInt = 1800
    playlist_check_interval_s: PositiveInt = 3600


PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Backoffs = Annotated[list[PositiveInt], Field(min_length=1)]


class Watchdog(SettingsModel):
    probe_timeout_s: PositiveSeconds = 120
    stall_timeout_s: PositiveSeconds = 180
    total_timeout_base_s: PositiveSeconds = 1800
    total_timeout_duration_factor: PositiveSeconds = 2.0


class Retry(SettingsModel):
    give_up_after_days: PositiveInt = 14
    transient_backoff_s: Backoffs = Field(default_factory=lambda: [120, 600, 1800, 7200, 21600])
    download_403_backoff_s: Backoffs = Field(default_factory=lambda: [120, 900, 3600])
    unclassified_backoff_s: Backoffs = Field(default_factory=lambda: [600, 3600, 21600])
    soft_unavailable_recheck_days: Backoffs = Field(default_factory=lambda: [1, 7, 30])


class Breaker(SettingsModel):
    correlation_window_s: PositiveInt = 600
    correlation_distinct_sources: PositiveInt = 3
    cooldown_steps_s: Backoffs = Field(default_factory=lambda: [1800, 7200, 21600])
    max_trips_per_24h: PositiveInt = 3


class Disk(SettingsModel):
    min_free_gb: float = Field(default=2.0, ge=0, allow_inf_nan=False)


class Notify(SettingsModel):
    webhook_url: str = ""
    webhook_headers_json: str = ""

    @field_validator("webhook_headers_json")
    @classmethod
    def validate_headers(cls, value: str) -> str:
        headers = json.loads(value) if value else {}
        if not isinstance(headers, dict) or any(not isinstance(item, str) for item in headers.values()):
            raise ValueError("webhook headers must be a JSON object of strings")
        return value


class Config(SettingsModel):
    paths: Paths = Field(default_factory=Paths)
    web: Web = Field(default_factory=Web)
    toolchain: Toolchain = Field(default_factory=Toolchain)
    pacing: Pacing = Field(default_factory=Pacing)
    watchdog: Watchdog = Field(default_factory=Watchdog)
    retry: Retry = Field(default_factory=Retry)
    breaker: Breaker = Field(default_factory=Breaker)
    defaults: SourcePolicy = Field(default_factory=SourcePolicy)
    disk: Disk = Field(default_factory=Disk)
    notify: Notify = Field(default_factory=Notify)

    def redacted(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        for section, key in (
            ("toolchain", "ytdlp_proxy"), ("toolchain", "ytdlp_extractor_args"),
            ("notify", "webhook_url"), ("notify", "webhook_headers_json"),
        ):
            if data[section][key]:
                data[section][key] = "<redacted>"
        return data


def load_config(path: str | Path | None = None) -> Config:
    configured = path or os.environ.get("PODHUB_CONFIG")
    data: dict[str, Any] = {}
    if configured:
        with Path(configured).expanduser().open("rb") as config_file:
            data = tomllib.load(config_file)
    for section, field in Config.model_fields.items():
        model = field.annotation
        for key, option in model.model_fields.items():
            short = f"PODHUB_{key.upper()}"
            qualified = f"PODHUB_{section.upper()}__{key.upper()}"
            raw = os.environ.get(qualified, os.environ.get(short))
            if raw is not None:
                if option.annotation is str or option.annotation is Path:
                    data.setdefault(section, {})[key] = raw
                else:
                    try:
                        data.setdefault(section, {})[key] = json.loads(raw)
                    except ValueError:
                        data.setdefault(section, {})[key] = raw
    wrapped_python = os.environ.get("PODHUB_DEFAULT_YTDLP_PYTHON")
    if wrapped_python:
        data.setdefault("toolchain", {}).setdefault("ytdlp_python", wrapped_python)
    return Config.model_validate(data)
