import errno
from pathlib import Path

import pytest

from app.config import Settings
from app.storage.media_store import MediaStore


def test_publish_audio_falls_back_on_cross_device_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        TMP_MEDIA_DIR=str(tmp_path / "tmp-media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
    )
    store = MediaStore(settings)

    temp_dir = store.temp_dir_for_episode(25)
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / "audio.m4a"
    temp_file_path.write_bytes(b"demo-audio")

    calls = []
    original_replace = Path.replace

    def replace_with_exdev_once(self: Path, target: Path) -> Path:
        calls.append((self, target))
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_with_exdev_once)

    final_path = store.publish_audio(
        temp_file_path=temp_file_path,
        channel_dirname="demo-channel",
        webpage_url="https://www.youtube.com/watch?v=demo",
    )

    assert final_path.exists()
    assert final_path.read_bytes() == b"demo-audio"
    assert not temp_file_path.exists()
