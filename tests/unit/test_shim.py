from __future__ import annotations

import errno
import json
import os
import signal
import sys
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from podhub.domain.policy import SourcePolicy
from podhub.errors.taxonomy import classify
from podhub.ytdlp import shim
from podhub.storage.cookies import copy_cookie
from podhub.storage.media import atomic_publish
from podhub.ytdlp.runner import RunResult, Runner
from podhub.ytdlp.shim import run

from conftest import COOKIES, VIDEO


@pytest.mark.parametrize("live, duration, media_type, expected", [
    ("is_upcoming", 90, "livestream", "defer"), ("is_live", 90, "livestream", "defer"), ("post_live", 90, "livestream", "defer"),
    ("not_live", 2, "video", "filter"), ("not_live", 90, "video", "result"), ("not_live", 175, "short", "filter"),
])
def test_standalone_fetch_probe_gate_uses_single_ydl(tmp_path, monkeypatch, capsys, live, duration, media_type, expected):
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
                "id": VIDEO, "live_status": live, "duration": duration, "media_type": media_type, "formats": [{"id": "1"}],
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
    assert probe["media_type"] == media_type
    assert probe["chapters"][0]["title"] == "章节"
    assert events[-1].get("action", events[-1]["type"]) == expected
    if media_type == "short":
        assert events[-1]["reason"] == "YouTube Short"
    assert calls == (["instance", "extract", "download", "remux"] if expected == "result" else ["instance", "extract"])


@pytest.fixture
def probe_only(monkeypatch, tmp_path, capsys):
    def run_probe(extract, *, cookie=False):
        class FakeYDL:
            def __init__(self, options):
                assert options["ignore_no_formats_error"] is True
                self.logger = options["logger"]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, *, download, process):
                assert not download and not process
                return extract(self.logger)

            def process_ie_result(self, *args, **kwargs):
                pytest.fail("unplayable video should never reach the downloader")

        monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=FakeYDL))
        monkeypatch.setitem(sys.modules, "yt_dlp.version", SimpleNamespace(__version__="2026.10.26"))
        status = run({
            "mode": "fetch", "url": shim.canonical_video(VIDEO), "workspace": tmp_path,
            "cookiefile": str(tmp_path / "cookies.txt") if cookie else None,
            "policy": {"audio_format": "bestaudio"},
        })
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert not any(event.get("phase") == "download" for event in events)
        return status, events
    return run_probe


@pytest.mark.parametrize("message, cookie, availability, category", [
    ("This video is unavailable", False, None, "soft_unavailable"),
    ("Private video. Sign in if you've been granted access to this video", True, "needs_auth", "soft_unavailable"),
    ("Sign in to confirm you’re not a bot", False, "needs_auth", "rate_limit_or_bot"),
    ("Join this channel to get access to members-only content", False, "subscriber_only", "auth_required"),
])
def test_shim_preserves_youtube_playability_warning(probe_only, message, cookie, availability, category):
    def extract(logger):
        logger.warning(f"[youtube] {VIDEO}: This video is unavailable")
        logger.warning(f"[youtube] {message}")
        logger.warning('[youtube] Skipping client "web" since it does not support cookies')
        logger.warning(f"[youtube] {VIDEO}: n challenge solving failed: Some formats may be missing.")
        logger.warning(f"[youtube] {VIDEO}: Some web client https formats have been skipped as they are missing a URL.")
        logger.warning("[youtube] Incomplete data received in embedded initial data; re-fetching using API.")
        logger.warning("[youtube] Error solving n challenge")
        if cookie:
            logger.warning("[youtube] The provided YouTube account cookies are no longer valid.")
        return {"id": VIDEO, "formats": [], "availability": availability}

    status, events = probe_only(extract, cookie=cookie)
    assert status == 1
    error = events[-1]
    assert (error["type"], error["phase"], error["msg"], error["expected"]) == ("error", "extract", message, True)
    warnings = [event["msg"] for event in events if event["type"] == "warning"]
    assert classify(RunResult(error=error, warnings=warnings), used_cookie=cookie).category == category


@pytest.mark.parametrize("availability, message, category", [
    ("private", "Private video", "soft_unavailable"),
    ("needs_auth", "Sign in to watch this video", "auth_required"),
    ("subscriber_only", "This video is available to this channel's members only", "auth_required"),
    ("premium_only", "This video is only available to YouTube Premium members", "auth_required"),
    ("public", "Requested format is not available", "unclassified"),
])
def test_shim_no_playability_reason_uses_availability_then_generic_fallback(probe_only, availability, message, category):
    def extract(logger):
        logger.warning(f"[youtube] {VIDEO}: n challenge solving failed: Some formats may be missing.")
        return {"id": VIDEO, "formats": [], "availability": availability}

    status, events = probe_only(extract)
    assert status == 1
    assert events[-1]["msg"] == message and events[-1]["phase"] == "extract"
    assert classify(RunResult(error=events[-1])).category == category


def test_shim_defers_upcoming_without_formats(probe_only):
    status, events = probe_only(lambda _: {
        "id": VIDEO, "formats": [], "live_status": "is_upcoming", "release_timestamp": 1796000000,
    })
    assert status == 0 and events[-1]["type"] == "gate" and events[-1]["action"] == "defer"
    assert events[-1]["release_timestamp"] == 1796000000


def test_shim_preserves_wrapped_geo_restriction(probe_only):
    class GeoRestrictedError(Exception):
        expected = True

    class DownloadError(Exception):
        pass

    def extract(logger):
        try:
            raise GeoRestrictedError("Available elsewhere")
        except GeoRestrictedError as exc:
            error = DownloadError(f"ERROR: {exc}")
            error.exc_info = sys.exc_info()
            raise error from None

    status, events = probe_only(extract)
    assert status == 1
    assert events[-1]["exc_class"] == "GeoRestrictedError" and events[-1]["expected"] is True
    assert classify(RunResult(error=events[-1])).category == "soft_unavailable"


@pytest.mark.parametrize("hints", [{"media_type": "short"}, {"is_short": True}, {"url": "https://www.youtube.com/shorts/id"}])
def test_source_policy_recognizes_long_shorts(hints):
    info = {"duration": 175, **hints}
    assert SourcePolicy().rejection(info) == "YouTube Short"
    assert SourcePolicy(include_shorts=True).rejection(info) is None


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


def test_list_sets_truthy_approximate_date_and_paginates_sort_key(monkeypatch, capsys):
    import json
    import types

    captured = {}

    class FakeYDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, *, download, process):
            return {"entries": [{"id": VIDEO, "title": "Page 2 Item"}]}

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = FakeYDL
    version = types.ModuleType("yt_dlp.version")
    version.__version__ = "2026.10.26"
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    monkeypatch.setitem(sys.modules, "yt_dlp.version", version)
    assert run({"mode": "list", "url": "https://www.youtube.com/playlist?list=UU" + "a" * 22, "start": 501, "limit": 50}) == 0
    assert captured["extractor_args"]["youtubetab"]["approximate_date"] == [""]
    assert captured["playliststart"] == 501
    assert captured["playlistend"] == 550
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    entry = next(event for event in events if event["type"] == "entry")
    assert entry["sort_key"] == 501


async def test_stop_group_sends_sigkill_under_repeated_cancellation(monkeypatch):
    import asyncio
    from podhub.ytdlp import runner

    signals: list[int] = []
    waited = False

    class StubProc:
        pid = 999999
        returncode = None

        async def wait(self):
            nonlocal waited
            waited = True
            self.returncode = -9
            return -9

    def fake_killpg(pid, sig):
        signals.append(sig)

    monkeypatch.setattr(os, "killpg", fake_killpg)
    task = asyncio.create_task(runner.stop_group(StubProc(), grace=5.0))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert signal.SIGTERM in signals and signal.SIGKILL in signals and waited
