from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from http.client import IncompleteRead

import pytest

from podhub.ytdlp import shim
from podhub.ytdlp.ffprobe import faststart, validate_media

from conftest import VIDEO


@pytest.mark.parametrize("failure", [None, "download", "decode", "embed", "metadata"])
async def test_real_m4a_mux_preserves_audio_with_optional_metadata_and_cover(
    sample_media, tmp_path, monkeypatch, capsys, failure,
):
    title = "中文 = 音乐; #春\\夏\n冬"
    description = "原始介绍 = 春; #记录\\试听\n第二行"
    info = {
        "id": VIDEO, "title": title, "channel": "原频道" if failure is None else None, "uploader": "原作者",
        "description": description, "upload_date": "20260203" if failure is None else None,
        "timestamp": datetime(2026, 2, 4, tzinfo=UTC).timestamp(), "duration": 2,
        "thumbnail": f"https://i.ytimg.com/vi/{VIDEO}/default.jpg",
        "chapters": [
            {"start_time": 0, "end_time": 1.125, "title": "第一章 = 开篇"},
            {"start_time": 1.125, "end_time": 2, "title": "第二章 #终"},
            {"start_time": 0, "end_time": -1, "title": "ignored"},
            {"start_time": 0, "end_time": float("nan"), "title": "ignored"},
            {"start_time": True, "end_time": 2, "title": "ignored"},
        ],
    }
    photo = tmp_path / "fixture.png"
    if failure is None or failure in ("embed", "metadata"):
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32",
            "-frames:v", "1", "--", str(photo),
        ], check=True, capture_output=True)

    def download(url, target, proxy):
        assert (url, proxy) == (info["thumbnail"], "http://proxy.test:8080")
        if failure == "download":
            raise IncompleteRead(b"incomplete")
        if failure == "decode":
            target.write_bytes(b"unknown image format")
        else:
            shutil.copyfile(photo, target)
        return "png"

    original_mux = shim.mux_audio

    def mux(ffmpeg, audio, target, **options):
        if failure == "embed" and options.get("cover"):
            raise RuntimeError("cannot embed cover")
        if failure == "metadata" and options.get("metadata"):
            raise RuntimeError("cannot embed metadata")
        original_mux(ffmpeg, audio, target, **options)

    monkeypatch.setattr(shim, "download_thumbnail", download)
    monkeypatch.setattr(shim, "mux_audio", mux)
    final = shim.finish_audio({"proxy": "http://proxy.test:8080"}, info, sample_media, tmp_path, is_aac=True)
    warnings = [json.loads(line)["msg"] for line in capsys.readouterr().out.splitlines()]
    assert len(warnings) == (2 if failure == "metadata" else int(failure is not None))
    if failure:
        assert "thumbnail" in warnings[0]
    if failure == "metadata":
        assert "metadata tags" in warnings[-1]
    assert faststart(final)
    assert (await validate_media("ffprobe", final, expected=2)).duration == 2
    result = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters", "-of", "json", "--", str(final),
    ]))
    audio = next(stream for stream in result["streams"] if stream["codec_type"] == "audio")
    assert audio["codec_name"] == "aac"
    covers = [stream for stream in result["streams"] if stream["disposition"]["attached_pic"]]
    tags = result["format"]["tags"]
    if failure is None:
        assert len(covers) == 1 and covers[0]["codec_name"] == "mjpeg"
        assert tags["artist"] == "原频道" and tags["date"] == "2026-02-03"
    else:
        assert not covers
    if failure == "metadata":
        assert "title" not in tags and not result["chapters"]
    else:
        assert tags["title"] == title
        assert tags["comment"] == tags["description"] == f"https://www.youtube.com/watch?v={VIDEO}\n\n{description}"
        assert [chapter["tags"]["title"] for chapter in result["chapters"]] == ["第一章 = 开篇", "第二章 #终"]
        assert float(result["chapters"][1]["start_time"]) == pytest.approx(1.125, abs=0.001)
        if failure:
            assert tags["artist"] == "原作者" and tags["date"] == "2026-02-04"
