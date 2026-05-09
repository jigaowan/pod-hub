from __future__ import annotations

import re

MAX_NOTIFICATION_ERROR_LENGTH = 500

ERROR_LINE_RE = re.compile(r"\bERROR:\s*(?P<message>.+)", re.IGNORECASE)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
YTDLP_CONTEXT_RE = re.compile(r"^\[[^\]]+\]\s+[^:]+:\s*")

FAILURE_KEYWORDS = (
    "failed",
    "unable",
    "unsupported",
    "private",
    "sign in",
    "cookie",
    "cookies",
    "update yt-dlp",
)


def extract_ytdlp_error_message(error: Exception) -> str:
    output = _error_output(error)
    error_line = _first_error_line(output)
    if error_line:
        return error_line

    failure_line = _first_failure_line(output)
    if failure_line:
        return failure_line

    return _limit(" ".join(str(error).split()))


def collect_unique_errors(errors: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for error in errors:
        cleaned = _limit(" ".join(error.split()))
        if not cleaned or cleaned in seen:
            continue
        unique.append(cleaned)
        seen.add(cleaned)
    return unique


def _error_output(error: Exception) -> str:
    stderr = getattr(error, "stderr", None) or ""
    stdout = getattr(error, "stdout", None) or ""
    return f"{stderr}\n{stdout}"


def _first_error_line(output: str) -> str | None:
    for line in _iter_clean_lines(output):
        match = ERROR_LINE_RE.search(line)
        if match:
            return _clean_message(match.group("message"))
    return None


def _first_failure_line(output: str) -> str | None:
    for line in _iter_clean_lines(output):
        lowered = line.lower()
        if any(keyword in lowered for keyword in FAILURE_KEYWORDS):
            return _clean_message(line)
    return None


def _iter_clean_lines(output: str):
    for line in output.splitlines():
        cleaned = _collapse(line)
        if cleaned:
            yield cleaned


def _clean_message(message: str) -> str:
    cleaned = _collapse(message)
    while True:
        stripped = YTDLP_CONTEXT_RE.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    return _limit(cleaned)


def _collapse(value: str) -> str:
    return " ".join(ANSI_RE.sub("", value).split())


def _limit(value: str) -> str:
    return value[:MAX_NOTIFICATION_ERROR_LENGTH]
