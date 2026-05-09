from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import os
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse
from pathlib import Path

from app.common.errors import DownloaderError, SourceResolutionError
from app.common.media import AUDIO_FILE_EXTENSION, AUDIO_MIME_TYPE, episode_storage_filename
from app.common.time import parse_upload_date, published_at_from_upload_date
from app.config import Settings
from app.downloader.schemas import DiscoveredEpisode, FetchedAudio, ResolvedSource

MAX_ERROR_LENGTH = 4096


class YtDlpClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("podhub.downloader")

    def resolve_channel(self, source_input: str, cookie_text: str | None = None) -> ResolvedSource:
        normalized = self._normalize_source_input(source_input)
        payload = self._run_json(
            "resolve_channel",
            ["--flat-playlist", "--playlist-end", "1", "--dump-single-json", normalized],
            cookie_text=cookie_text,
        )

        external_channel_id = (
            payload.get("channel_id")
            or payload.get("uploader_id")
            or self._extract_channel_id_from_input(source_input)
        )
        if not external_channel_id:
            raise SourceResolutionError("Unable to resolve a stable YouTube channel id")

        display_name = self._first_non_url_string(
            payload.get("channel"),
            payload.get("uploader"),
            payload.get("title"),
            external_channel_id,
        )
        source_url = payload.get("channel_url") or normalized
        cover_url = self._select_cover_url(payload)
        return ResolvedSource(
            source_kind="youtube_channel",
            external_id=external_channel_id,
            source_url=source_url,
            display_name=display_name,
            cover_url=cover_url,
        )

    def resolve_playlist(self, source_url: str, cookie_text: str | None = None) -> ResolvedSource:
        payload = self._run_json(
            "resolve_playlist",
            ["--flat-playlist", "--playlist-end", "1", "--dump-single-json", source_url],
            cookie_text=cookie_text,
        )
        playlist_id = payload.get("id") or self._extract_playlist_id_from_input(source_url)
        if not playlist_id:
            raise SourceResolutionError("Unable to resolve a stable YouTube playlist id")

        display_name = self._first_non_url_string(
            payload.get("playlist_title"),
            payload.get("title"),
            f"Playlist {playlist_id}",
        )
        resolved_url = payload.get("webpage_url") or source_url
        cover_url = self._select_cover_url(payload)
        return ResolvedSource(
            source_kind="youtube_playlist",
            external_id=playlist_id,
            source_url=resolved_url,
            display_name=display_name,
            cover_url=cover_url,
        )

    def list_channel_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        payload = self._run_json(
            "list_channel",
            ["--flat-playlist", "--dump-single-json", source_url],
            cookie_text=cookie_text,
        )
        entries = payload.get("entries") or []

        episodes: list[DiscoveredEpisode] = []
        for source_position, entry in enumerate(entries, start=1):
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
                    source_position=source_position,
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

    def list_playlist_episodes(self, source_url: str, cookie_text: str | None = None) -> list[DiscoveredEpisode]:
        payload = self._run_json(
            "list_playlist",
            ["--flat-playlist", "--dump-single-json", source_url],
            cookie_text=cookie_text,
        )
        entries = payload.get("entries") or []

        episodes: list[DiscoveredEpisode] = []
        for source_position, entry in enumerate(entries, start=1):
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
                    source_position=source_position,
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

    def fetch_episode_audio(
        self,
        webpage_url: str,
        episode_id: int,
        temp_dir: Path,
        cookie_text: str | None = None,
    ) -> FetchedAudio:
        metadata = self._run_json(
            "fetch_metadata",
            ["--no-playlist", "--dump-single-json", webpage_url],
            cookie_text=cookie_text,
        )

        temp_dir.mkdir(parents=True, exist_ok=True)
        # Use the final storage basename while still downloading into the per-episode temp directory.
        # This keeps file names stable and avoids an extra rename step during publish.
        storage_filename = episode_storage_filename(webpage_url)
        output_template = str(temp_dir / f"{Path(storage_filename).stem}.%(ext)s")
        temp_file_path = temp_dir / storage_filename
        download_args = [
            "--no-playlist",
            "--format",
            "m4a/bestaudio/best",
            "--extract-audio",
            "--audio-format",
            AUDIO_FILE_EXTENSION,
            "--audio-quality",
            "0",
            "--add-metadata",
            "--embed-thumbnail",
            "--convert-thumbnails",
            "jpg",
            "--output",
            output_template,
            webpage_url,
        ]
        try:
            self._run_command("download_audio", download_args, timeout=3600, cookie_text=cookie_text)
        except DownloaderError as exc:
            if temp_file_path.exists() and temp_file_path.stat().st_size >= 16 * 1024:
                self.logger.warning(
                    "yt-dlp download_audio returned failure but output exists; proceeding episode_id=%s url=%s error=%s",
                    episode_id,
                    webpage_url,
                    exc,
                )
            else:
                combined = f"{getattr(exc, 'stderr', '')}\n{getattr(exc, 'stdout', '')}"
                if self._looks_like_thumbnail_or_embed_failure(combined):
                    self.logger.warning(
                        "yt-dlp download_audio failed during thumbnail/embed; retrying without thumbnail episode_id=%s url=%s error=%s",
                        episode_id,
                        webpage_url,
                        exc,
                    )
                    retry_args = [
                        "--no-playlist",
                        "--format",
                        "m4a/bestaudio/best",
                        "--extract-audio",
                        "--audio-format",
                        AUDIO_FILE_EXTENSION,
                        "--audio-quality",
                        "0",
                        "--add-metadata",
                        "--output",
                        output_template,
                        webpage_url,
                    ]
                    self._run_command(
                        "download_audio_no_thumbnail",
                        retry_args,
                        timeout=3600,
                        cookie_text=cookie_text,
                    )
                else:
                    raise

        if not temp_file_path.exists():
            raise DownloaderError(
                f"yt-dlp did not produce expected {storage_filename} for episode {episode_id}"
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

    def _run_json(self, command_type: str, extra_args: list[str], cookie_text: str | None = None) -> dict:
        completed = self._run_command(command_type, [*extra_args], timeout=1800, cookie_text=cookie_text)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            error = DownloaderError(f"yt-dlp returned invalid JSON for {command_type}")
            error.stdout = completed.stdout
            error.stderr = completed.stderr
            error.returncode = completed.returncode
            error.command_type = command_type
            raise error from exc

    def _run_command(
        self,
        command_type: str,
        extra_args: list[str],
        *,
        timeout: int,
        cookie_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.settings.ytdlp_bin]
        if self.settings.ytdlp_proxy:
            command.extend(["--proxy", self.settings.ytdlp_proxy])
        js_runtimes = self._select_js_runtimes()
        if js_runtimes:
            command.extend(["--js-runtimes", js_runtimes])
        if self.settings.ytdlp_socket_timeout:
            command.extend(["--socket-timeout", str(self.settings.ytdlp_socket_timeout)])
        if self.settings.ytdlp_retries:
            command.extend(["--retries", str(self.settings.ytdlp_retries)])
        if self.settings.ytdlp_fragment_retries:
            command.extend(["--fragment-retries", str(self.settings.ytdlp_fragment_retries)])
        with self._temporary_cookie_file(cookie_text) as cookie_path:
            if cookie_path is not None:
                command.extend(["--cookies", str(cookie_path)])
            command.extend(extra_args)
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
            error = DownloaderError(f"{command_type} failed: {stderr}")
            error.stdout = completed.stdout
            error.stderr = completed.stderr
            error.returncode = completed.returncode
            error.command_type = command_type
            raise error

        return completed

    @contextmanager
    def _temporary_cookie_file(self, cookie_text: str | None) -> Iterator[Path | None]:
        if cookie_text is None:
            yield None
            return

        self.settings.tmp_media_dir.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.settings.tmp_media_dir,
                prefix="yt-dlp-cookies-",
                suffix=".txt",
                delete=False,
            ) as temp_file:
                temp_file.write(cookie_text)
                temp_path = Path(temp_file.name)
            os.chmod(temp_path, 0o600)
            yield temp_path
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

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

    def _extract_playlist_id_from_input(self, source_input: str) -> str | None:
        parsed = urlparse(source_input)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        if not parsed.path.rstrip("/").endswith("/playlist"):
            return None
        query = parse_qs(parsed.query)
        list_ids = query.get("list") or []
        return list_ids[0] if list_ids else None

    def _summarize_error(self, value: str) -> str:
        cleaned = " ".join(value.split())
        return cleaned[:MAX_ERROR_LENGTH]

    def _select_js_runtimes(self) -> str | None:
        return self.settings.ytdlp_js_runtimes

    def _looks_like_thumbnail_or_embed_failure(self, stderr: str) -> bool:
        lowered = (stderr or "").lower()
        patterns = (
            "unable to download video thumbnail",
            "unable to download thumbnail",
            "unable to embed using ffprobe",
            "unable to embed thumbnail",
            "convert thumbnails",
            "conversion failed",
            "ssl.c:993",
            "handshake operation timed out",
        )
        return any(pattern in lowered for pattern in patterns)

    def _first_non_url_string(self, *values: object) -> str:
        for value in values:
            if not isinstance(value, str):
                continue
            cleaned = value.strip()
            if not cleaned or self._looks_like_url(cleaned):
                continue
            return cleaned

        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()

        raise SourceResolutionError("Unable to resolve a display name for the source")

    def _looks_like_url(self, value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _select_cover_url(self, payload: dict) -> str | None:
        thumbnails = payload.get("thumbnails") or []
        if thumbnails:
            selected = self._pick_avatar_thumbnail(thumbnails)
            if selected:
                return selected

        direct = payload.get("thumbnail")
        if isinstance(direct, str) and direct:
            return direct

        return self._pick_largest_thumbnail(thumbnails)

    def _pick_avatar_thumbnail(self, thumbnails: list[dict]) -> str | None:
        squareish: list[dict] = []
        neutral: list[dict] = []

        for thumb in thumbnails:
            if not isinstance(thumb, dict):
                continue
            url = thumb.get("url")
            if not url:
                continue
            width = thumb.get("width") or 0
            height = thumb.get("height") or 0
            preference = thumb.get("preference")
            ratio = (width / height) if width and height else None
            if ratio is not None and 0.8 <= ratio <= 1.25:
                squareish.append(thumb)
            if preference is None or preference >= 0:
                neutral.append(thumb)

        if squareish:
            return self._pick_best_by_preference(squareish)
        if neutral:
            return self._pick_best_by_preference(neutral)
        return None

    def _pick_best_by_preference(self, thumbnails: list[dict]) -> str | None:
        best = None
        for thumb in thumbnails:
            width = thumb.get("width") or 0
            height = thumb.get("height") or 0
            area = width * height if width and height else -1
            preference = thumb.get("preference")
            score = (preference if isinstance(preference, (int, float)) else 0, area)
            if best is None or score > best[0]:
                best = (score, thumb)
        if best:
            return best[1].get("url")
        return None

    def _pick_largest_thumbnail(self, thumbnails: list[dict]) -> str | None:
        best_url = None
        best_area = -1
        for thumb in thumbnails:
            if not isinstance(thumb, dict):
                continue
            url = thumb.get("url")
            if not url:
                continue
            width = thumb.get("width") or 0
            height = thumb.get("height") or 0
            area = width * height if width and height else -1
            if area > best_area:
                best_area = area
                best_url = url
            elif best_area < 0:
                best_url = url
        return best_url
