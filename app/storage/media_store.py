from __future__ import annotations

import errno
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import Settings


class MediaStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_runtime_dirs()

    def temp_dir_for_episode(self, episode_id: int) -> Path:
        return self.settings.tmp_media_dir / str(episode_id)

    def publish_audio(self, temp_file_path: Path, channel_dirname: str) -> Path:
        final_path = self.settings.media_dir / channel_dirname / temp_file_path.name
        final_path.parent.mkdir(parents=True, exist_ok=True)
        self._publish_temp_file(temp_file_path, final_path)
        return final_path

    def publish_transcript(self, transcript_text: str, channel_dirname: str, source_video_id: str) -> Path:
        final_path = self.settings.media_dir / channel_dirname / f"{source_video_id}.vtt"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.settings.tmp_media_dir,
            prefix=f"{source_video_id}-",
            suffix=".vtt",
            delete=False,
        ) as temp_file:
            temp_file.write(transcript_text)
            temp_path = Path(temp_file.name)
        self._publish_temp_file(temp_path, final_path)
        return final_path

    def cleanup_temp_dir(self, episode_id: int) -> None:
        temp_dir = self.temp_dir_for_episode(episode_id)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    def save_channel_cover(self, cover_url: str, channel_dirname: str) -> Path:
        channel_dir = self.settings.media_dir / channel_dirname
        channel_dir.mkdir(parents=True, exist_ok=True)

        existing = next(channel_dir.glob("cover.*"), None)
        if existing is not None:
            return existing

        request = Request(cover_url, headers={"User-Agent": "PodHub/0.1"})
        with urlopen(request, timeout=20) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            extension = self._infer_cover_extension(cover_url, content_type)
            final_path = channel_dir / f"cover{extension}"

            with tempfile.NamedTemporaryFile(dir=self.settings.tmp_media_dir, delete=False) as temp_file:
                shutil.copyfileobj(response, temp_file)
                temp_path = Path(temp_file.name)

        self._publish_temp_file(temp_path, final_path)
        return final_path

    def _infer_cover_extension(self, cover_url: str, content_type: str) -> str:
        url_ext = Path(urlparse(cover_url).path).suffix.lower()
        if url_ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            return ".jpg" if url_ext == ".jpeg" else url_ext

        if content_type == "image/jpeg":
            return ".jpg"
        if content_type == "image/png":
            return ".png"
        if content_type == "image/webp":
            return ".webp"
        if content_type == "image/gif":
            return ".gif"

        return ".jpg"

    def _publish_temp_file(self, temp_path: Path, final_path: Path) -> None:
        try:
            temp_path.replace(final_path)
            return
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise

        # Cross-device renames are not supported (e.g. TMP_MEDIA_DIR on /tmp, MEDIA_DIR on a mounted volume).
        # We copy into the destination directory first, then use an atomic rename within that filesystem.
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_dest_path: Path | None = None
        published = False
        try:
            with tempfile.NamedTemporaryFile(
                dir=final_path.parent,
                prefix=f"{final_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_dest:
                with temp_path.open("rb") as src:
                    shutil.copyfileobj(src, temp_dest)
                temp_dest.flush()
                os.fsync(temp_dest.fileno())
                temp_dest_path = Path(temp_dest.name)

            temp_dest_path.replace(final_path)
            published = True
        finally:
            if temp_dest_path is not None and temp_dest_path.exists():
                try:
                    temp_dest_path.unlink()
                except OSError:
                    pass
            if published and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass
