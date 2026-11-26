from __future__ import annotations

import errno
import os
import signal
import sys

import pytest

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
            return {"id": VIDEO, "live_status": live, "duration": duration, "formats": [{"id": "1"}]}

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
    assert next(event["info"]["formats_count"] for event in events if event["type"] == "probe") == 1
    assert events[-1].get("action", events[-1]["type"]) == expected
    assert calls == (["instance", "extract", "download", "remux"] if expected == "result" else ["instance", "extract"])


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
