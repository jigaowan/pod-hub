class PodHubError(Exception):
    """Base exception for application errors."""


class ValidationError(PodHubError):
    """Raised when user input or config is invalid."""


class SourceResolutionError(PodHubError):
    """Raised when a YouTube source cannot be resolved."""


class DownloaderError(PodHubError):
    """Raised when yt-dlp execution fails."""


class SyncAlreadyRunningError(PodHubError):
    """Raised when a sync trigger collides with an in-flight run."""

