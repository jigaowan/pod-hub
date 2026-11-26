from __future__ import annotations

import pytest

from podhub.config import Config, load_config
from podhub.domain.policy import SourcePolicy
from podhub.storage.paths import canonical_video, parse_video

from conftest import VIDEO


def test_config_file_env_and_redaction(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('[web]\nlisten_port = 9000\nbase_url = "https://pod.example/"\n[toolchain]\nytdlp_python = "/system/python"\n')
    monkeypatch.setenv("PODHUB_CONFIG", str(path))
    monkeypatch.setenv("PODHUB_BASE_URL", "https://other.example")
    monkeypatch.setenv("PODHUB_WEB__BASE_URL", "https://preferred.example/")
    monkeypatch.setenv("PODHUB_DEFAULT_YTDLP_PYTHON", "/wrapped/python")
    monkeypatch.setenv("PODHUB_WEBHOOK_URL", "https://hook.example/SECRET")
    monkeypatch.setenv("PODHUB_RETRY__UNCLASSIFIED_BACKOFF_S", "[5, 10]")
    config = load_config()
    assert config.web.listen_port == 9000
    assert config.web.base_url == "https://preferred.example"
    assert config.toolchain.ytdlp_python == "/system/python"
    assert config.retry.unclassified_backoff_s == [5, 10]
    assert config.redacted()["notify"]["webhook_url"] == "<redacted>"


@pytest.mark.parametrize("relative", [".", "tmp", "cookies/nested", "app.db"])
def test_output_cannot_overlap_private_state(tmp_path, relative):
    with pytest.raises(ValueError, match="overlaps private state"):
        Config.model_validate({"paths": {"state_dir": tmp_path, "output_dir": tmp_path / relative}})


def test_default_nixos_nested_output_is_valid(tmp_path):
    config = Config.model_validate({"paths": {"state_dir": tmp_path, "output_dir": tmp_path / "output"}})
    assert config.paths.output_dir == tmp_path / "output"


def test_input_validation():
    assert parse_video(f"https://youtu.be/{VIDEO}") == VIDEO
    assert canonical_video(VIDEO).endswith(f"v={VIDEO}")
    with pytest.raises(ValueError):
        parse_video(f"https://youtube.com.evil.example/watch?v={VIDEO}")
    with pytest.raises(ValueError, match="invalid title pattern"):
        SourcePolicy(title_include_regex="[")
