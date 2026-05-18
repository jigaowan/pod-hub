from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.common.errors import ValidationError
from app.config import Settings
from app.db.base import Base
from app.db.repositories import settings as settings_repo
from app.runtime_settings.service import RuntimeSettingsService


def build_service(tmp_path: Path) -> tuple[RuntimeSettingsService, sessionmaker[Session]]:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        BASE_URL="https://env.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
    )
    return RuntimeSettingsService(settings, session_factory=session_factory), session_factory


def base_form(**overrides: str) -> dict[str, str]:
    values = {
        "base_url": "https://web.example.com",
        "fetch_batch_size": "4",
        "sync_interval": "12",
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


def test_runtime_settings_prefers_database_over_env(tmp_path: Path) -> None:
    service, session_factory = build_service(tmp_path)
    with session_factory() as session:
        settings_repo.set_setting(session, "BASE_URL", "https://db.example.com")
        settings_repo.set_setting(session, "FETCH_BATCH_SIZE", "9")
        session.commit()

    effective = service.get_effective_settings()

    assert effective.base_url == "https://db.example.com"
    assert effective.fetch_batch_size == 9
    assert effective.sync_interval == 30


def test_save_form_clears_optional_and_keeps_blank_secret_values(tmp_path: Path) -> None:
    service, session_factory = build_service(tmp_path)
    service.save_form(
        base_form(
            ytdlp_proxy="http://127.0.0.1:7890",
            alarm_url="https://alarm.example.com",
            alarm_custom_psk="secret",
            alarm_tg_user_id="100",
        )
    )

    service.save_form(
        base_form(
            ytdlp_proxy="",
            alarm_url="https://alarm2.example.com",
            alarm_custom_psk="",
            alarm_tg_user_id="",
        )
    )

    effective = service.get_effective_settings()
    assert effective.ytdlp_proxy is None
    assert effective.alarm_url == "https://alarm2.example.com"
    assert effective.alarm_custom_psk == "secret"
    assert effective.alarm_tg_user_id == "100"
    with session_factory() as session:
        assert settings_repo.get_setting(session, "YTDLP_PROXY") is None


def test_blank_optional_field_falls_back_to_env_default(tmp_path: Path) -> None:
    database_path = tmp_path / "podhub.db"
    engine = create_engine(f"sqlite:///{database_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    settings = Settings(
        BASE_URL="https://env.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(database_path),
        YTDLP_PROXY="http://env-proxy:7890",
    )
    service = RuntimeSettingsService(settings, session_factory=session_factory)

    service.save_form(base_form(ytdlp_proxy=""))

    assert service.get_effective_settings().ytdlp_proxy == "http://env-proxy:7890"


def test_save_form_rejects_partial_alarm_config(tmp_path: Path) -> None:
    service, _session_factory = build_service(tmp_path)

    with pytest.raises(ValidationError, match="ALARM_URL"):
        service.save_form(base_form(alarm_url="https://alarm.example.com"))


def test_save_form_rejects_partial_cloudflare_config(tmp_path: Path) -> None:
    service, _session_factory = build_service(tmp_path)

    with pytest.raises(ValidationError, match="CLOUDFLARE_AI_GATEWAY_URL"):
        service.save_form(
            base_form(
                cloudflare_ai_gateway_url="https://gateway.ai.cloudflare.com/v1/account/default/compat",
            )
        )
