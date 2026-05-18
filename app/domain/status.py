from __future__ import annotations

from enum import StrEnum


class ChannelStatus(StrEnum):
    PENDING_INITIAL_SYNC = "pending_initial_sync"
    ACTIVE = "active"
    SYNCING = "syncing"
    ERROR = "error"


class EpisodeStatus(StrEnum):
    DISCOVERED = "discovered"
    QUEUED = "queued"
    FETCHING = "fetching"
    AUDIO_READY = "audio_ready"
    FAILED = "failed"
    SKIPPED = "skipped"


class TranscriptStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class SyncRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
