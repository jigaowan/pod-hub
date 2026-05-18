from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.transcription.client import CloudflareTranscriptionClient, TranscriptionError


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
        CLOUDFLARE_AI_GATEWAY_URL="https://gateway.ai.cloudflare.com/v1/account/default/compat/chat/completions",
        CLOUDFLARE_API_TOKEN="secret",
    )


def test_transcription_client_posts_audio_to_fixed_workers_ai_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "audio.m4a"
    audio_path.write_bytes(b"audio")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse({"vtt": "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello\n"})

    monkeypatch.setattr("app.transcription.client.request.urlopen", fake_urlopen)
    client = CloudflareTranscriptionClient(build_settings(tmp_path))

    vtt = client.transcribe_to_vtt(audio_path)

    assert captured["url"] == (
        "https://gateway.ai.cloudflare.com/v1/account/default/"
        "workers-ai/@cf/openai/whisper-large-v3-turbo"
    )
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert set(captured["payload"]) == {"audio"}
    assert captured["payload"]["audio"] == "YXVkaW8="
    assert captured["timeout"] == 600
    assert vtt.startswith("WEBVTT")


def test_transcription_client_rejects_response_without_vtt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio_path = tmp_path / "audio.m4a"
    audio_path.write_bytes(b"audio")

    def fake_urlopen(req, timeout):
        return FakeResponse({"text": "Hello"})

    monkeypatch.setattr("app.transcription.client.request.urlopen", fake_urlopen)
    client = CloudflareTranscriptionClient(build_settings(tmp_path))

    with pytest.raises(TranscriptionError, match="did not include VTT"):
        client.transcribe_to_vtt(audio_path)
