from __future__ import annotations

from app.common.errors import DownloaderError
from app.sync.error_messages import collect_unique_errors, extract_ytdlp_error_message


def downloader_error(*, stderr: str = "", stdout: str = "") -> DownloaderError:
    error = DownloaderError("download_audio failed: fallback message")
    error.stderr = stderr
    error.stdout = stdout
    error.returncode = 1
    error.command_type = "download_audio"
    return error


def test_extracts_single_error_line() -> None:
    error = downloader_error(stderr="ERROR: Sign in to confirm you are not a bot\n")

    assert extract_ytdlp_error_message(error) == "Sign in to confirm you are not a bot"


def test_extracts_error_line_without_youtube_context() -> None:
    error = downloader_error(stderr="ERROR: [youtube] abc123: Unable to extract yt initial data; please update yt-dlp")

    assert extract_ytdlp_error_message(error) == "Unable to extract yt initial data; please update yt-dlp"


def test_extracts_core_error_from_multiline_output() -> None:
    error = downloader_error(
        stderr="\n".join(
            [
                "[youtube] Extracting URL: https://www.youtube.com/watch?v=abc123",
                "[youtube] abc123: Downloading webpage",
                "ERROR: [youtube] abc123: Private video. Sign in if you have been granted access",
            ]
        )
    )

    assert extract_ytdlp_error_message(error) == "Private video. Sign in if you have been granted access"


def test_extracts_failure_line_without_error_prefix() -> None:
    error = downloader_error(stderr="WARNING: unable to download video thumbnail")

    assert extract_ytdlp_error_message(error) == "WARNING: unable to download video thumbnail"


def test_extract_falls_back_to_exception_summary() -> None:
    error = downloader_error()

    assert extract_ytdlp_error_message(error) == "download_audio failed: fallback message"


def test_collect_unique_errors_preserves_first_seen_order() -> None:
    assert collect_unique_errors(["A", "B", "A", " C "]) == ["A", "B", "C"]
