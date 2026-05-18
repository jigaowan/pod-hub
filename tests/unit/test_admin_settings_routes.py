from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from app.config import Settings
from app.db.base import Base
from app.db.repositories import settings as settings_repo
from app.runtime_settings.service import RuntimeSettingsService
from app.web.routes import admin


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def get_job(self, job_id: str):
        return None

    def reschedule_job(self, job_id: str, *, trigger: str, minutes: int) -> None:
        self.calls.append((job_id, trigger, minutes))


def build_app_state(tmp_path: Path) -> tuple[SimpleNamespace, sessionmaker[Session], FakeScheduler]:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        BASE_URL="https://env.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    runtime_settings_service = RuntimeSettingsService(settings, session_factory=session_factory)
    scheduler = FakeScheduler()
    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=settings,
            runtime_settings_service=runtime_settings_service,
            scheduler=scheduler,
        )
    )
    return app, session_factory, scheduler


def build_request(app, *, method: str = "GET", path: str = "/admin/settings", data: dict[str, str] | None = None) -> Request:
    body = urlencode(data or {}).encode("utf-8")
    headers = []
    if data is not None:
        headers = [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
            "app": app,
        },
        receive,
    )


def form_data(**overrides: str) -> dict[str, str]:
    values = {
        "base_url": "https://web.example.com",
        "fetch_batch_size": "4",
        "sync_interval": "15",
        "ytdlp_proxy": "",
        "ytdlp_socket_timeout": "30",
        "ytdlp_retries": "5",
        "ytdlp_fragment_retries": "5",
        "alarm_url": "",
        "alarm_custom_psk": "",
        "alarm_tg_user_id": "",
        "alarm_timeout_seconds": "10",
        "cloudflare_ai_gateway_url": "",
        "cloudflare_api_token": "",
    }
    values.update(overrides)
    return values


def test_settings_page_renders_classified_fields(tmp_path: Path) -> None:
    app, _session_factory, _scheduler = build_app_state(tmp_path)

    response = admin.settings_form(build_request(app))

    assert response.status_code == 200
    body = response.body.decode()
    assert "BASE_URL" in body
    assert "DATABASE_PATH" in body
    assert "YTDLP_JS_RUNTIMES" in body
    assert "Cloudflare 转录" in body


def test_settings_post_saves_and_reschedules_sync(tmp_path: Path) -> None:
    app, session_factory, scheduler = build_app_state(tmp_path)

    response = asyncio.run(admin.update_settings(build_request(app, method="POST", data=form_data())))

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/settings?saved=1"
    assert scheduler.calls == [("channel-sync", "interval", 15)]
    with session_factory() as session:
        assert settings_repo.get_setting(session, "BASE_URL").value == "https://web.example.com"
        assert settings_repo.get_setting(session, "SYNC_INTERVAL").value == "15"


def test_settings_validation_error_does_not_echo_secret_values(tmp_path: Path) -> None:
    app, _session_factory, scheduler = build_app_state(tmp_path)

    response = asyncio.run(
        admin.update_settings(
            build_request(
                app,
                method="POST",
                data=form_data(
                    alarm_url="https://alarm.example.com",
                    alarm_custom_psk="secret-value",
                ),
            )
        )
    )

    assert response.status_code == 400
    assert scheduler.calls == []
    body = response.body.decode()
    assert "ALARM_URL" in body
    assert "https://alarm.example.com" in body
    assert "secret-value" not in body
