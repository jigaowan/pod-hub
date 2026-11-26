from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from podhub.config import Config
from podhub.core.services import Services
from podhub.domain.enums import SourceKind
from podhub.domain.policy import SourcePolicy
from podhub.ytdlp.runner import RunResult

VIDEO = "-abcdefghij"
OTHER = "Kabcdefghij"
THIRD = "Tabcdefghij"
CHANNEL = "UC" + "a" * 22
COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tsample-cookie-secret\n"


class FakeRunner:
    def __init__(self, media: Path | None = None):
        self.calls = []
        self.entries = []
        self.media = media
        self.before_fetch = None

    async def run(self, mode: str, *, on_entry=None, **kwargs) -> RunResult:
        self.calls.append((mode, kwargs))
        hello = {"ytdlp_version": "2026.10.26", "ffmpeg_version": "ffmpeg test", "js_runtime_version": "deno test"}
        if mode == "doctor":
            return RunResult(hello=hello, exit_code=0)
        if mode == "resolve":
            return RunResult(hello=hello, result={
                "kind": "channel", "external_id": CHANNEL, "normalized_url": f"https://www.youtube.com/channel/{CHANNEL}",
                "title": "Example Channel", "cover_url": None,
            })
        if mode == "list":
            for entry in self.entries:
                if on_entry and not on_entry(entry):
                    break
            return RunResult(hello=hello, exit_code=0)
        vid = parse_qs(urlsplit(kwargs["url"]).query)["v"][0]
        info = {"id": vid, "title": "中文节目 \x01]]>", "description": "Description & a newline\n", "duration": 2,
                "upload_date": "20260105", "live_status": "not_live", "formats_count": 1,
                "thumbnail": f"https://i.ytimg.com/vi/{vid}/default.jpg"}
        if mode == "probe":
            return RunResult(hello=hello, probe=info)
        if self.before_fetch:
            error = await self.before_fetch()
            if error:
                return error
        assert self.media is not None
        destination = kwargs["workspace"] / "audio.m4a"
        shutil.copyfile(self.media, destination)
        return RunResult(hello=hello, probe=info, phase="postprocess", result={"filepath": str(destination)}, exit_code=0)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config.model_validate({
        "paths": {"state_dir": tmp_path / "state", "output_dir": tmp_path / "output"},
        "toolchain": {"ytdlp_python": sys.executable, "ffprobe_path": "ffprobe", "js_runtime": ""},
        "disk": {"min_free_gb": 0}, "pacing": {"min_launch_gap_s": 0, "launch_jitter_s": 0},
        "defaults": {"min_duration_s": 0},
    })


@pytest.fixture
def services(config: Config) -> Services:
    service = Services(config, runner=FakeRunner())
    service.initialize()
    return service


@pytest.fixture
def source_factory(services: Services):
    async def make(title: str = "Listen Later", **policy_values) -> int:
        preview = await services.preview(title, SourceKind.MANUAL)
        policy = SourcePolicy.model_validate({"min_duration_s": 0, **policy_values})
        return await services.add_source(preview.identity, title, policy)
    return make


@pytest.fixture(scope="session")
def sample_media(tmp_path_factory) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or shutil.which("ffprobe") is None:
        pytest.fail("integration tests require ffmpeg and ffprobe")
    path = tmp_path_factory.mktemp("media") / "fixture.m4a"
    subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=400:duration=2",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "--", str(path),
    ], check=True, capture_output=True)
    assert path.stat().st_size >= 16 * 1024
    return path
