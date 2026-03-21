from pathlib import Path

from app.config import Settings
from app.downloader.client import YtDlpClient


def build_client(tmp_path: Path) -> YtDlpClient:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
    )
    return YtDlpClient(settings)


def test_resolve_playlist_ignores_url_title(monkeypatch, tmp_path: Path) -> None:
    client = build_client(tmp_path)

    def fake_run_json(command_type: str, extra_args: list[str]) -> dict:
        assert command_type == "resolve_playlist"
        assert "https://www.youtube.com/playlist?list=PL123" in extra_args
        return {
            "id": "PL123",
            "title": "http://localhost:8000/feeds/channel.xml",
            "webpage_url": "https://www.youtube.com/playlist?list=PL123",
        }

    monkeypatch.setattr(client, "_run_json", fake_run_json)

    resolved = client.resolve_playlist("https://www.youtube.com/playlist?list=PL123")

    assert resolved.display_name == "Playlist PL123"


def test_resolve_playlist_prefers_playlist_title(monkeypatch, tmp_path: Path) -> None:
    client = build_client(tmp_path)

    def fake_run_json(command_type: str, extra_args: list[str]) -> dict:
        assert command_type == "resolve_playlist"
        assert "https://www.youtube.com/playlist?list=PL123" in extra_args
        return {
            "id": "PL123",
            "playlist_title": "中文播放列表",
            "title": "http://localhost:8000/feeds/channel.xml",
            "webpage_url": "https://www.youtube.com/playlist?list=PL123",
        }

    monkeypatch.setattr(client, "_run_json", fake_run_json)

    resolved = client.resolve_playlist("https://www.youtube.com/playlist?list=PL123")

    assert resolved.display_name == "中文播放列表"
