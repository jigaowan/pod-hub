from __future__ import annotations

import shutil
from pathlib import Path

from app.common.media import episode_storage_filename
from app.config import Settings


class MediaStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.ensure_runtime_dirs()

    def final_path_for_episode(self, channel_dirname: str, webpage_url: str) -> Path:
        return self.settings.media_dir / channel_dirname / episode_storage_filename(webpage_url)

    def temp_dir_for_episode(self, episode_id: int) -> Path:
        return self.settings.tmp_media_dir / str(episode_id)

    def publish_audio(self, temp_file_path: Path, channel_dirname: str, webpage_url: str) -> Path:
        final_path = self.final_path_for_episode(channel_dirname, webpage_url)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file_path.replace(final_path)
        return final_path

    def cleanup_temp_dir(self, episode_id: int) -> None:
        temp_dir = self.temp_dir_for_episode(episode_id)
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
