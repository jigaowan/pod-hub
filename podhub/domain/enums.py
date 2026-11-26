from __future__ import annotations

from enum import StrEnum


class SourceKind(StrEnum):
    CHANNEL = "channel"
    PLAYLIST = "playlist"
    MANUAL = "manual"


class VideoState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    READY = "ready"
    DEFERRED = "deferred"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    PURGED = "purged"


class EntryState(StrEnum):
    CANDIDATE = "candidate"
    FILTERED = "filtered"
    WANTED = "wanted"
    PUBLISHED = "published"
    RETIRING = "retiring"
    EVICTED = "evicted"


class Category(StrEnum):
    TRANSIENT_NETWORK = "transient_network"
    DOWNLOAD_403 = "download_403"
    RATE_LIMIT_OR_BOT = "rate_limit_or_bot"
    TOOLCHAIN_BROKEN = "toolchain_broken"
    COOKIE_INVALID = "cookie_invalid"
    AUTH_REQUIRED = "auth_required"
    NOT_READY = "not_ready"
    PERMANENT_UNAVAILABLE = "permanent_unavailable"
    SOFT_UNAVAILABLE = "soft_unavailable"
    DEGRADABLE_POSTPROCESS = "degradable_postprocess"
    CORRUPTED_ARTIFACT = "corrupted_artifact"
    LOCAL_RESOURCE = "local_resource"
    UNCLASSIFIED = "unclassified"


class Scope(StrEnum):
    VIDEO = "video"
    GLOBAL = "global"
    TOOLCHAIN = "toolchain"
    COOKIE = "cookie"
    LOCAL = "local"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
