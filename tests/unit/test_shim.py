from __future__ import annotations

import errno
import os
import signal
import sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from podhub.ytdlp import shim
from podhub.storage.cookies import copy_cookie
from podhub.storage.media import atomic_publish
from podhub.ytdlp.runner import Runner
from podhub.ytdlp.shim import run

from conftest import COOKIES, VIDEO


@pytest.mark.parametrize("live, duration, expected", [
    ("is_upcoming", 90, "defer"), ("is_live", 90, "defer"), ("post_live", 90, "defer"),
    ("not_live", 2, "filter"), ("not_live", 90, "result"),
])
def test_standalone_fetch_probe_gate_uses_single_ydl(tmp_path, monkeypatch, capsys, live, duration, expected):
    import json
    import types

    calls = []

    class FakeYDL:
        def __init__(self, options):
            calls.append("instance")
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, *, download, process):
            assert url == f"https://www.youtube.com/watch?v={VIDEO}" and not download and not process
            calls.append("extract")
            return {
                "id": VIDEO, "live_status": live, "duration": duration, "formats": [{"id": "1"}],
                "channel": "原频道", "uploader": "原作者", "chapters": [{"start_time": 0, "end_time": 1, "title": "章节"}],
            }

        def process_ie_result(self, raw, *, download):
            assert download and raw["id"] == VIDEO
            calls.append("download")
            self.options["post_hooks"][0](str(tmp_path / "input.m4a"))
            return {"acodec": "mp4a.40.2"}

    def ffmpeg(args, **kwargs):
        calls.append("remux")
        assert args[-2] == "--" and args[args.index("-c:a") + 1] == "copy"
        return types.SimpleNamespace(returncode=0, stderr="")

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = FakeYDL
    version = types.ModuleType("yt_dlp.version")
    version.__version__ = "2026.10.26"
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    monkeypatch.setitem(sys.modules, "yt_dlp.version", version)
    monkeypatch.setattr("podhub.ytdlp.shim.subprocess.run", ffmpeg)
    assert run({"mode": "fetch", "url": f"https://www.youtube.com/watch?v={VIDEO}",
                "workspace": tmp_path, "policy": {"audio_format": "bestaudio", "min_duration_s": 60}}) == 0
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["type"] == "hello"
    probe = next(event["info"] for event in events if event["type"] == "probe")
    assert probe["formats_count"] == 1 and (probe["channel"], probe["uploader"]) == ("原频道", "原作者")
    assert probe["chapters"][0]["title"] == "章节"
    assert events[-1].get("action", events[-1]["type"]) == expected
    assert calls == (["instance", "extract", "download", "remux"] if expected == "result" else ["instance", "extract"])


@pytest.mark.parametrize("is_aac, codec", [(True, "copy"), (False, "aac")])
def test_mux_keeps_aac_and_only_transcodes_other_audio(tmp_path, monkeypatch, is_aac, codec):
    args = []
    monkeypatch.setattr(shim, "convert", lambda _, *options: args.extend(options))
    shim.mux_audio("ffmpeg", tmp_path / "input", tmp_path / "audio.m4a", is_aac=is_aac)
    assert args[args.index("-c:a") + 1] == codec
    if is_aac:
        assert "-b:a" not in args
    else:
        assert args[args.index("-b:a") + 1] == "128k"


@pytest.mark.parametrize("url", [
    "http://i.ytimg.com/cover.jpg", "https://i.ytimg.com.evil.test/a",
    "https://i.ytimg.com:8080/a", "https://name@i.ytimg.com/a", "https://[broken",
])
def test_thumbnail_rejects_untrusted_urls_before_opening(tmp_path, monkeypatch, url):
    monkeypatch.setattr(shim, "build_opener", lambda *_: pytest.fail("unexpected network call"))
    with pytest.raises(ValueError, match="thumbnail URL"):
        shim.download_thumbnail(url, tmp_path / "cover", None)


def test_thumbnail_uses_proxy_and_refuses_off_domain_redirect(tmp_path, monkeypatch):
    calls = []

    class Opener:
        def open(self, req, timeout):
            assert 0 < timeout <= shim.THUMBNAIL_TIMEOUT
            calls.append(req.full_url)
            headers = Message()
            headers["Location"] = "https://example.test/private"
            raise HTTPError(req.full_url, 302, "Redirect", headers, BytesIO())

    def build(*handlers):
        proxy = next(handler for handler in handlers if isinstance(handler, ProxyHandler))
        assert proxy.proxies == {"https": "http://proxy.test:8080"}
        return Opener()

    monkeypatch.setattr(shim, "build_opener", build)
    url = "https://i.ytimg.com/cover.jpg"
    with pytest.raises(ValueError, match="allowlist"):
        shim.download_thumbnail(url, tmp_path / "cover", "http://proxy.test:8080")
    assert calls == [url]


def test_thumbnail_checks_stream_size_without_content_length(tmp_path, monkeypatch):
    class OversizeResponse(BytesIO):
        code = 200
        headers = Message()
        headers["Content-Type"] = "image/jpeg"

    class Opener:
        def open(self, req, timeout):
            return OversizeResponse(b"x" * 11)

    monkeypatch.setattr(shim, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(shim, "THUMBNAIL_MAX_BYTES", 10)
    with pytest.raises(ValueError, match="size limit"):
        shim.download_thumbnail("https://i.ytimg.com/cover.jpg", tmp_path / "cover", None)


def test_thumbnail_https_proxy_uses_configured_transport(tmp_path, monkeypatch):
    from contextlib import nullcontext

    url, proxy = "https://i.ytimg.com/cover.jpg", "https://proxy.test:8443"
    request = object()

    def send(prepared, *, proxies, stream, timeout):
        assert prepared is request and proxies == {"https": proxy} and stream and 0 < timeout <= 15
        response = SimpleNamespace(
            status_code=200, headers={"Content-Type": "image/jpeg"},
            raw=SimpleNamespace(stream=lambda *_, **__: [b"test image"]),
        )
        return nullcontext(response)

    session = SimpleNamespace(prepare_request=lambda value: value, get_adapter=lambda _: SimpleNamespace(send=send))
    requests = SimpleNamespace(Request=lambda *_, **__: request, Session=lambda: nullcontext(session))
    monkeypatch.setitem(sys.modules, "yt_dlp.dependencies", SimpleNamespace(requests=requests))
    target = tmp_path / "cover"
    assert shim.download_thumbnail(url, target, proxy) == "mjpeg"
    assert target.read_bytes() == b"test image"


def test_metadata_limits_oversize_descriptions():
    metadata = shim.ffmetadata({"id": VIDEO, "description": "长" * 9000})
    assert metadata.count("长" * 8192) == 2
    assert "长" * 8193 not in metadata


@pytest.mark.parametrize("phase", ["extract", "download"])
async def test_watchdog_terminates_process_group(config, tmp_path, phase):
    pidfile = tmp_path / "child.pid"
    shim = tmp_path / "stall.py"
    shim.write_text(
        "import json, os, subprocess, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        + ("print(json.dumps({'type':'probe','info':{'duration': 1}}),flush=True)\n"
           "print(json.dumps({'type':'phase','phase':'download'}),flush=True)\n" if phase == "download" else "")
        + "import signal\ndef finish(*a):\n child.wait(timeout=3)\n sys.exit(0)\nsignal.signal(signal.SIGTERM, finish)\ntime.sleep(30)\n",
    )
    config.watchdog.probe_timeout_s = 0.8
    config.watchdog.stall_timeout_s = 0.3
    result = await Runner(config, shim).run("fetch")
    assert result.error["exc_class"] == "WatchdogTimeout"
    assert result.error["phase"] == phase
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), signal.Signals.SIGCONT)


def test_isolated_cookie_files(services):
    cid = services.save_cookie("Login", COOKIES)
    root = services.config.paths.state_dir / "cookies"
    assert (root / f"{cid}.txt").stat().st_mode & 0o777 == 0o600
    work = services.config.paths.state_dir / "tmp"
    isolated = copy_cookie(root, f"{cid}.txt", work)
    assert isolated.stat().st_mode & 0o777 == 0o600
    isolated.write_text("attempt data")
    assert (root / f"{cid}.txt").read_text() == COOKIES


def test_exdev_atomic_copy_leaves_no_stage_file(tmp_path, monkeypatch):
    source = tmp_path / "state" / "audio.m4a"
    source.parent.mkdir()
    source.write_bytes(b"complete audio")
    target = tmp_path / "podcasts" / "東京 音訊" / f"{VIDEO}.m4a"
    real_replace = os.replace

    def replace(origin, destination):
        if origin == source:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(origin, destination)

    monkeypatch.setattr(os, "replace", replace)
    atomic_publish(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert list(target.parent.iterdir()) == [target]
