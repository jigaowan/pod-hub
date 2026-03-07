from pathlib import Path

from app.config import Settings


def test_fetch_batch_size_defaults_to_three(tmp_path: Path) -> None:
    settings = Settings(
        BASE_URL="https://podhub.example.com",
        MEDIA_DIR=str(tmp_path / "media"),
        DATABASE_PATH=str(tmp_path / "podhub.db"),
    )

    assert settings.fetch_batch_size == 3
