from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

AUDIO_FILE_EXTENSION = "m4a"
AUDIO_MIME_TYPE = "audio/mp4"


def episode_media_filename(episode_id: int) -> str:
    return f"{episode_id}.{AUDIO_FILE_EXTENSION}"


def _extract_youtube_video_id(webpage_url: str) -> str | None:
    try:
        parsed = urlparse(webpage_url)
    except ValueError:
        return None

    host = parsed.netloc.lower()
    if not host:
        return None

    if "youtu.be" in host:
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return video_id or None

    if "youtube.com" in host:
        query = parse_qs(parsed.query)
        video_id = query.get("v", [None])[0]
        if video_id:
            return video_id

        path_parts = [part for part in parsed.path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "embed"}:
            return path_parts[1]

    return None


def episode_storage_filename(webpage_url: str) -> str:
    video_id = _extract_youtube_video_id(webpage_url)
    if video_id:
        return f"{video_id}.{AUDIO_FILE_EXTENSION}"

    encoded_url = quote(webpage_url, safe="")
    return f"{encoded_url}.{AUDIO_FILE_EXTENSION}"
