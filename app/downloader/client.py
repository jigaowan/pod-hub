from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from app.common.errors import DownloaderError, SourceResolutionError
from app.common.media import AUDIO_FILE_EXTENSION, AUDIO_MIME_TYPE
from app.common.time import parse_upload_date, published_at_from_upload_date
from app.config import Settings
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio, ResolvedChannel

MAX_ERROR_LENGTH = 4096


class YtDlpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("podhub.downloader")

    def resolve_channel(self, source_input: str) -> ResolvedChannel:
        normalized = self._normalize_source_input(source_input)
        payload = self._run_json(
            "resolve_channel",
            ["--flat-playlist", "--playlist-end", "1", "--dump-single-json", normalized],
        )

        external_channel_id = (
            payload.get("channel_id")
            or payload.get("uploader_id")
            or self._extract_channel_id_from_input(source_input)
        )
        if not external_channel_id:
            raise SourceResolutionError("Unable to resolve a stable YouTube channel id")

        display_name = (
            payload.get("channel")
            or payload.get("uploader")
            or payload.get("title")
            or external_channel_id
        )
        source_url = payload.get("channel_url") or normalized
        return ResolvedChannel(
            external_channel_id=external_channel_id,
            source_url=source_url,
            display_name=display_name,
        )

    def list_channel_episodes(self, source_url: str) -> list[DiscoveredEpisode]:
        payload = self._run_json(
            "list_channel",
            ["--flat-playlist", "--dump-single-json", source_url],
        )
        entries = payload.get("entries") or []

        episodes: list[DiscoveredEpisode] = []
        for entry in entries:
            source_video_id = entry.get("id")
            if not source_video_id:
                continue

            webpage_url = (
                entry.get("webpage_url")
                or entry.get("url")
                or f"https://www.youtube.com/watch?v={source_video_id}"
            )
            upload_date = parse_upload_date(entry.get("upload_date"))
            episodes.append(
                DiscoveredEpisode(
                    source_video_id=source_video_id,
                    webpage_url=webpage_url,
                    title=entry.get("title") or source_video_id,
                    description=entry.get("description"),
                    upload_date=upload_date,
                    published_at=published_at_from_upload_date(entry.get("upload_date")),
                    duration_seconds=entry.get("duration"),
                    thumbnail_url=entry.get("thumbnail"),
                )
            )
        return episodes

    def fetch_episode_audio(self, webpage_url: str, episode_id: int, temp_dir: Path) -> FetchedAudio:
        metadata = self._run_json(
            "fetch_metadata",
            ["--no-playlist", "--dump-single-json", webpage_url],
        )

        temp_dir.mkdir(parents=True, exist_ok=True)
        output_template = str(temp_dir / "audio.%(ext)s")
        self._run_command(
            "download_audio",
            [
                "--no-playlist",
                "--format",
                "m4a/bestaudio/best",
                "--extract-audio",
                "--audio-format",
                AUDIO_FILE_EXTENSION,
                "--audio-quality",
                "0",
                "--output",
                output_template,
                webpage_url,
            ],
            timeout=3600,
        )

        temp_file_path = temp_dir / f"audio.{AUDIO_FILE_EXTENSION}"
        if not temp_file_path.exists():
            raise DownloaderError(
                f"yt-dlp did not produce expected {AUDIO_FILE_EXTENSION} for episode {episode_id}"
            )

        return FetchedAudio(
            title=metadata.get("title") or str(episode_id),
            description=metadata.get("description"),
            upload_date=parse_upload_date(metadata.get("upload_date")),
            published_at=published_at_from_upload_date(metadata.get("upload_date")),
            duration_seconds=metadata.get("duration"),
            thumbnail_url=metadata.get("thumbnail"),
            audio_mime_type=AUDIO_MIME_TYPE,
            audio_bytes=temp_file_path.stat().st_size,
            temp_file_path=temp_file_path,
        )

    def _run_json(self, command_type: str, extra_args: list[str]) -> dict:
        completed = self._run_command(command_type, [*extra_args], timeout=1800)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise DownloaderError(f"yt-dlp returned invalid JSON for {command_type}") from exc

    def _run_command(self, command_type: str, extra_args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        command = [self.settings.ytdlp_bin, *extra_args]
        started = time.monotonic()
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        duration = time.monotonic() - started
        self.logger.info("yt-dlp command=%s duration=%.3fs returncode=%s", command_type, duration, completed.returncode)

        if completed.returncode != 0:
            stderr = self._summarize_error(completed.stderr or completed.stdout)
            raise DownloaderError(f"{command_type} failed: {stderr}")

        return completed

    def _normalize_source_input(self, source_input: str) -> str:
        value = source_input.strip()
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("@"):
            return f"https://www.youtube.com/{value}"
        if value.startswith("UC"):
            return f"https://www.youtube.com/channel/{value}"
        return value

    def _extract_channel_id_from_input(self, source_input: str) -> str | None:
        value = source_input.strip()
        if value.startswith("UC"):
            return value
        if "/channel/" in value:
            return value.rstrip("/").split("/channel/")[-1]
        return None

    def _summarize_error(self, value: str) -> str:
        cleaned = " ".join(value.split())
        return cleaned[:MAX_ERROR_LENGTH]
