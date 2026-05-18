from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_fetch_batch_size_defaults_to_three(tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
    )

    assert settings.fetch_batch_size == 3


def test_alarm_config_defaults_to_disabled(tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
    )

    assert settings.alarm_url is None
    assert settings.alarm_custom_psk is None
    assert settings.alarm_tg_user_id is None
    assert settings.alarm_timeout_seconds == 10


def test_alarm_config_accepts_complete_values(tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
        ALARM_URL="https://alarm.ogiso.me",
        ALARM_CUSTOM_PSK="12345",
        ALARM_TG_USER_ID="67890",
        ALARM_TIMEOUT_SECONDS=5,
    )

    assert settings.alarm_url == "https://alarm.ogiso.me"
    assert settings.alarm_custom_psk == "12345"
    assert settings.alarm_tg_user_id == "67890"
    assert settings.alarm_timeout_seconds == 5


def test_alarm_config_rejects_partial_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(
            BASE_URL="https://podhub.example.com",
            MEDIA_DIR=str(tmp_path / "media"),
            DATABASE_PATH=str(tmp_path / "podhub.db"),
            ALARM_URL="https://alarm.ogiso.me",
        )


def test_cloudflare_gateway_config_builds_fixed_whisper_url(tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
        CLOUDFLARE_AI_GATEWAY_URL=(
            "https://gateway.ai.cloudflare.com/v1/52b5ab387ad5f461c620fa6c038b1cc0/default/"
            "compat/chat/completions"
        ),
        CLOUDFLARE_API_TOKEN="secret",
    )

    assert settings.transcription_configured is True
    assert settings.cloudflare_whisper_url == (
        "https://gateway.ai.cloudflare.com/v1/52b5ab387ad5f461c620fa6c038b1cc0/default/"
        "workers-ai/@cf/openai/whisper-large-v3-turbo"
    )


def test_cloudflare_gateway_config_rejects_partial_values(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must be set together"):
        Settings(
            BASE_URL="https://podhub.example.com",
            MEDIA_DIR=str(tmp_path / "media"),
            DATABASE_PATH=str(tmp_path / "podhub.db"),
            CLOUDFLARE_AI_GATEWAY_URL="https://gateway.ai.cloudflare.com/v1/account/default/compat",
        )


def test_cloudflare_gateway_config_rejects_unparseable_url(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Cloudflare AI Gateway URL"):
        Settings(
            BASE_URL="https://podhub.example.com",
            MEDIA_DIR=str(tmp_path / "media"),
            DATABASE_PATH=str(tmp_path / "podhub.db"),
            CLOUDFLARE_AI_GATEWAY_URL="https://example.com/v1/account/default/compat",
            CLOUDFLARE_API_TOKEN="secret",
        )
