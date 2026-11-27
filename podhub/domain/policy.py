from __future__ import annotations

import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcePolicy(SettingsModel):
    audio_format: str = Field(default="bestaudio[ext=m4a]/bestaudio/best", min_length=1)
    fetch_history: bool = False
    include_shorts: bool = False
    include_live_archives: bool = True
    min_duration_s: int = Field(default=60, ge=0)
    max_duration_s: int = Field(default=0, ge=0)
    title_include_regex: str = Field(default="", max_length=256)
    title_exclude_regex: str = Field(default="", max_length=256)
    keep_count: int = Field(default=0, ge=0)
    keep_days: int = Field(default=0, ge=0)
    unpublish_grace_hours: float = Field(default=48, ge=0, allow_inf_nan=False)
    max_feed_items: int = Field(default=300, gt=0)
    feed_order: Literal["published_desc", "playlist_serial"] = "published_desc"
    playlist_scan_mode: Literal["head_early_stop", "full_flat"] = "full_flat"
    max_scan_items: int = Field(default=500, gt=0)
    early_stop_known: int = Field(default=10, gt=0)
    postprocess: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_filters(self) -> Self:
        if self.max_duration_s and self.max_duration_s < self.min_duration_s:
            raise ValueError("maximum duration must be at least the minimum")
        for pattern in (self.title_include_regex, self.title_exclude_regex):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid title pattern: {exc}") from exc
        return self

    def rejection(self, info: dict[str, Any]) -> str | None:
        # Discovery and the isolated probe share media_type and URL-based filtering.
        from podhub.ytdlp.shim import rejection

        return rejection(info, self.model_dump())
