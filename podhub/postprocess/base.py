from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class PostProcessContext:
    video_id: str
    audio_path: Path
    artifacts_dir: Path
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ArtifactResult:
    kind: str
    rel_path: str
    mime_type: str
    public: bool = True


class PostProcessor(Protocol):
    name: str

    def is_enabled_for_source(self, source_policy: dict[str, Any]) -> bool: ...

    async def process(self, ctx: PostProcessContext) -> list[ArtifactResult]: ...
