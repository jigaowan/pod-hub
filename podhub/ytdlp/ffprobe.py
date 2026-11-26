from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path

from podhub.ytdlp.runner import stop_group


class CorruptedArtifact(ValueError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    size: int
    duration: int
    mime: str = "audio/mp4"


def faststart(path: Path) -> bool:
    size = path.stat().st_size
    with path.open("rb") as stream:
        while stream.tell() + 8 <= size:
            start = stream.tell()
            header = stream.read(8)
            length = int.from_bytes(header[:4], "big")
            if length == 1:
                length = int.from_bytes(stream.read(8), "big")
            if length < 8 or start + length > size:
                return False
            if header[4:] in (b"moov", b"mdat"):
                return header[4:] == b"moov"
            stream.seek(start + length)
    return False


async def validate_media(ffprobe: str, path: Path, expected: float | None = None) -> MediaInfo:
    if not path.is_file() or path.stat().st_size < 16 * 1024:
        raise CorruptedArtifact("missing audio or file smaller than 16 KiB")
    process = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", "--", str(path.absolute()),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 30)
    except TimeoutError as exc:
        await stop_group(process)
        raise CorruptedArtifact("ffprobe could not read audio within 30 seconds") from exc
    except asyncio.CancelledError:
        await stop_group(process)
        raise
    if process.returncode:
        raise CorruptedArtifact(f"ffprobe could not read audio: {error.decode(errors='replace')[-500:]}")
    try:
        data = json.loads(output)
        audio = next((stream for stream in data["streams"] if stream["codec_type"] == "audio"), None)
        if audio is None:
            raise ValueError("no audio stream")
        duration = float(audio.get("duration") or data["format"]["duration"])
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid audio duration")
    except (TypeError, KeyError, ValueError) as exc:
        raise CorruptedArtifact(str(exc)) from exc
    if expected and expected >= 60 and abs(duration - expected) / expected > 0.05:
        raise CorruptedArtifact(f"duration mismatch: measured {duration:.2f}s, expected {expected}s")
    if not faststart(path):
        raise CorruptedArtifact("M4A has no faststart header")
    return MediaInfo(path.stat().st_size, max(1, round(duration)))
