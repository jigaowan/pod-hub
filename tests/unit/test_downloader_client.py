import subprocess
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

    def fake_run_json(command_type: str, extra_args: list[str], cookie_text: str | None = None) -> dict:
        assert command_type == "resolve_playlist"
        assert "https://www.youtube.com/playlist?list=PL123" in extra_args
        assert cookie_text is None
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

    def fake_run_json(command_type: str, extra_args: list[str], cookie_text: str | None = None) -> dict:
        assert command_type == "resolve_playlist"
        assert "https://www.youtube.com/playlist?list=PL123" in extra_args
        assert cookie_text is None
        return {
            "id": "PL123",
            "playlist_title": "中文播放列表",
            "title": "http://localhost:8000/feeds/channel.xml",
            "webpage_url": "https://www.youtube.com/playlist?list=PL123",
        }

    monkeypatch.setattr(client, "_run_json", fake_run_json)

    resolved = client.resolve_playlist("https://www.youtube.com/playlist?list=PL123")

    assert resolved.display_name == "中文播放列表"


def test_list_playlist_episodes_preserves_youtube_order(monkeypatch, tmp_path: Path) -> None:
    client = build_client(tmp_path)

    def fake_run_json(command_type: str, extra_args: list[str], cookie_text: str | None = None) -> dict:
        assert command_type == "list_playlist"
        assert "https://www.youtube.com/playlist?list=PL123" in extra_args
        assert cookie_text is None
        return {
            "entries": [
                {"id": "video-a", "title": "Video A", "upload_date": "20260101"},
                {"id": "video-b", "title": "Video B", "upload_date": "20260102"},
            ],
        }

    monkeypatch.setattr(client, "_run_json", fake_run_json)

    episodes = client.list_playlist_episodes("https://www.youtube.com/playlist?list=PL123")

    assert [episode.source_video_id for episode in episodes] == ["video-a", "video-b"]
    assert [episode.source_position for episode in episodes] == [1, 2]


def test_run_command_does_not_auto_add_js_runtimes(monkeypatch, tmp_path: Path) -> None:
    client = build_client(tmp_path)
    captured_command: list[str] = []

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("app.downloader.runner.subprocess.run", fake_run)

    client._run_command("test", ["--dump-single-json", "https://example.com"], timeout=1)

    assert "--js-runtimes" not in captured_command


def test_run_command_uses_explicit_js_runtimes(monkeypatch, tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
        YTDLP_JS_RUNTIMES="node",
    )
    client = YtDlpClient(settings)
    captured_command: list[str] = []

    def fake_run(command, **kwargs):
        captured_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("app.downloader.runner.subprocess.run", fake_run)

    client._run_command("test", ["--dump-single-json", "https://example.com"], timeout=1)

    assert "--js-runtimes" in captured_command
    assert "node" in captured_command


def test_run_command_writes_cookie_text_to_temporary_file(monkeypatch, tmp_path: Path) -> None:
    client = build_client(tmp_path)
    cookie_text = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsecret\n"
    captured_cookie_path: Path | None = None

    def fake_run(command, **kwargs):
        nonlocal captured_cookie_path
        cookie_arg_index = command.index("--cookies") + 1
        captured_cookie_path = Path(command[cookie_arg_index])
        assert captured_cookie_path.exists()
        assert captured_cookie_path.read_text() == cookie_text
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr("app.downloader.runner.subprocess.run", fake_run)

    client._run_command(
        "test",
        ["--dump-single-json", "https://example.com"],
        timeout=1,
        cookie_text=cookie_text,
    )

    assert captured_cookie_path is not None
    assert not captured_cookie_path.exists()
