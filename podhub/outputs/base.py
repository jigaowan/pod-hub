from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from podhub.domain.policy import SourcePolicy


@dataclass(frozen=True)
class Artifact:
    kind: str
    filename: str
    mime: str


@dataclass(frozen=True)
class Episode:
    video_id: str
    title: str
    description: str | None
    published_at: str | None
    media_bytes: int
    media_duration_s: int
    sort_key: int | None
    artifacts: tuple[Artifact, ...] = ()


@dataclass(frozen=True)
class RenderSourceContext:
    base_url: str
    feed_token: str
    title: str
    link: str
    cover: str | None
    policy: SourcePolicy
    episodes: tuple[Episode, ...]


@dataclass(frozen=True)
class RenderedOutput:
    content: bytes
    content_type: str


class OutputRenderer(Protocol):
    name: str
    content_type: str

    def render_source(self, ctx: RenderSourceContext) -> RenderedOutput: ...


def feed_url(base: str, token: str) -> str:
    return f"{base}/feeds/{token}.xml"


def file_url(ctx: RenderSourceContext, kind: str, name: str) -> str:
    return f"{ctx.base_url}/{kind}/{ctx.feed_token}/{quote(name, safe='')}"
