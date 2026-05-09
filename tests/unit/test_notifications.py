from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.notifications.client import AlarmNotifier


def build_settings(tmp_path: Path) -> Settings:
    return Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
        ALARM_URL="https://alarm.ogiso.me",
        ALARM_CUSTOM_PSK="12345",
        ALARM_TG_USER_ID="67890",
    )


def test_alarm_notifier_sends_expected_post(monkeypatch, tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    notifier = AlarmNotifier(settings)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

    def fake_urlopen(alarm_request, timeout: int):
        captured["url"] = alarm_request.full_url
        captured["method"] = alarm_request.get_method()
        captured["headers"] = alarm_request.headers
        captured["body"] = alarm_request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("app.notifications.client.request.urlopen", fake_urlopen)

    notifier.notify_sync_errors(channel_name="TED", errors=["Sign in to confirm you are not a bot"])

    assert captured["url"] == "https://alarm.ogiso.me"
    assert captured["method"] == "POST"
    assert captured["headers"]["X-custom-psk"] == "12345"
    assert captured["headers"]["X-tguser-id"] == "67890"
    assert captured["headers"]["Content-type"] == "application/json; charset=utf-8"
    assert captured["timeout"] == 10
    assert json.loads(captured["body"].decode("utf-8")) == {
        "title": "Pod Hub 抓取异常: TED",
        "message": "频道: TED\n错误:\n- Sign in to confirm you are not a bot",
    }


def test_alarm_notifier_failure_does_not_raise(monkeypatch, tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    notifier = AlarmNotifier(settings)

    def fake_urlopen(alarm_request, timeout: int):
        raise OSError("network down")

    monkeypatch.setattr("app.notifications.client.request.urlopen", fake_urlopen)

    notifier.notify_sync_errors(channel_name="TED", errors=["Unable to extract data"])
