from __future__ import annotations

from dataclasses import dataclass, replace

from podhub.domain.enums import Category, Confidence, Scope
from podhub.ytdlp.runner import RunResult
from podhub.ytdlp.shim import version_parts

APOSTROPHES = str.maketrans("’‘", "''")
COOKIE_ERRORS = (
    "cookies are no longer valid", "cookies have expired", "cookies are invalid", "cookies have likely been rotated",
    "cookie file must be in netscape",
)

@dataclass(frozen=True)
class Classification:
    category: Category
    scope: Scope
    confidence: Confidence
    retryable: bool
    reason: str
    correlation_key: str | None = None

    def escalate(self, category: Category, scope: Scope) -> Classification:
        return replace(self, category=category, scope=scope, confidence=Confidence.HIGH)


def classify(run: RunResult, *, used_cookie: bool = False, minimum: str = "2025.01.01") -> Classification:
    reason = str(run.error.get("msg") or run.gate.get("reason") or (run.warnings[-1] if run.warnings else "Unclassified failure"))
    message = reason.translate(APOSTROPHES).lower()
    warnings = " ".join(run.warnings).translate(APOSTROPHES).lower()
    status = run.error.get("http_status")
    exc = run.error.get("exc_class")
    phase = run.error.get("phase", run.phase)

    def rule(cat: Category, scope: Scope = Scope.VIDEO, confidence: Confidence = Confidence.HIGH,
             retry: bool = True, correlation: str | None = None) -> Classification:
        return Classification(cat, scope, confidence, retry, reason, correlation)

    if (run.exit_code in (137, -9) and exc != "WatchdogTimeout") or exc in ("PermissionError", "LocalResource"):
        return rule(Category.LOCAL_RESOURCE, Scope.LOCAL)
    if exc == "FileNotFoundError" or (run.version and version_parts(run.version) < version_parts(minimum)):
        reason = f"yt-dlp {run.version} is older than {minimum}" if run.version and exc != "FileNotFoundError" else reason
        return rule(Category.TOOLCHAIN_BROKEN, Scope.TOOLCHAIN)
    if any(text in message for text in ("no space left", "permission denied", "read-only file system", "disk quota exceeded")):
        return rule(Category.LOCAL_RESOURCE, Scope.LOCAL)
    if status == 429 or any(text in message for text in (
        "not a bot", "not a robot", "captcha challenge", "this content isn't available",
        "rate-limited", "too many requests", "try again later", "http error 429",
    )):
        return rule(Category.RATE_LIMIT_OR_BOT, Scope.GLOBAL, correlation="rate_limit_or_bot")
    if exc == "GeoRestrictedError" or any(text in message for text in (
        "private video", "video is private", "not available in your country", "not available from your location",
        "not made this video available in your country", "geo-blocked",
    )):
        return rule(Category.SOFT_UNAVAILABLE, confidence=Confidence.MEDIUM)
    if run.gate.get("action") == "defer" or run.probe.get("live_status") in ("is_upcoming", "is_live", "post_live") or any(
        text in message for text in ("premieres in", "live event will begin", "live stream will begin", "live archive is processing")
    ):
        return rule(Category.NOT_READY)
    if exc == "CorruptedArtifact" or any(text in message for text in (
        "ffprobe could not", "duration mismatch", "smaller than 16 kib", "invalid audio duration", "no audio stream",
    )):
        return rule(Category.CORRUPTED_ARTIFACT)
    if phase == "download" and (status == 403 or "http error 403" in message):
        return rule(Category.DOWNLOAD_403, correlation="download_403")
    if any(text in message for text in (
        "video has been removed", "video was removed", "video has been deleted", "copyright", "account has been terminated",
        "account associated with this video has been terminated", "drm protected", "drm-protected",
        "live stream recording is not available", "this video is no longer available",
    )):
        return rule(Category.PERMANENT_UNAVAILABLE, retry=False)
    if any(text in message for text in (
        "members-only", "join this channel", "available to this channel's members", "available to youtube premium members",
    )):
        if used_cookie and any(text in message or text in warnings for text in COOKIE_ERRORS):
            return rule(Category.COOKIE_INVALID, Scope.COOKIE, Confidence.HIGH)
        return rule(Category.AUTH_REQUIRED, retry=False)
    if any(text in message for text in (
        "confirm your age", "sign in", "login required", "authentication required",
    )):
        if used_cookie:
            confidence = Confidence.HIGH if any(text in message or text in warnings for text in COOKIE_ERRORS) else Confidence.MEDIUM
            return rule(Category.COOKIE_INVALID, Scope.COOKIE, confidence)
        return rule(Category.AUTH_REQUIRED, retry=False)
    if any(text in message for text in ("this video is unavailable", "video unavailable", "video is unavailable")):
        return rule(Category.SOFT_UNAVAILABLE, confidence=Confidence.MEDIUM)
    # Diagnostic warnings can clarify a generic failure without overriding a specific playability reason.
    if used_cookie and any(text in message or text in warnings for text in COOKIE_ERRORS):
        return rule(Category.COOKIE_INVALID, Scope.COOKIE)
    if any(text in message for text in ("requested format is not available", "no video formats found")):
        return rule(Category.UNCLASSIFIED, confidence=Confidence.LOW, correlation="format_unavailable")
    if exc in ("ImportError", "ModuleNotFoundError", "ToolchainError") or any(text in message + " " + warnings for text in (
        "nsig extraction failed", "signature extraction failed", "n challenge solving failed", "unable to extract",
        "no supported javascript runtime", "ffmpeg not found", "ffprobe not found",
    )) or ("please report" in message and bool(run.error.get("traceback_tail"))):
        return rule(Category.TOOLCHAIN_BROKEN, Scope.TOOLCHAIN)
    if phase == "postprocess" and any(text in message for text in ("thumbnail", "metadata tag", "embed artwork")):
        return rule(Category.DEGRADABLE_POSTPROCESS, confidence=Confidence.MEDIUM, retry=False)
    if (isinstance(status, int) and status >= 500) or exc in (
        "WatchdogTimeout", "TimeoutError", "ConnectError", "RemoteProtocolError", "ConnectionResetError", "SSLError",
    ) or any(text in message for text in (
        "timed out", "timeout", "connection reset", "temporary failure", "name resolution", "name or service not known",
        "certificate verify",
        "fragment not found", "unable to download fragment", "tls", "http error 5",
    )):
        return rule(Category.TRANSIENT_NETWORK, confidence=Confidence.HIGH if status or exc else Confidence.MEDIUM)
    return rule(Category.UNCLASSIFIED, confidence=Confidence.LOW)
