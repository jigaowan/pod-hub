from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
COVER = re.compile(r"cover\.[a-f0-9]{16,64}\.jpg")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


def canonical_video(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("invalid YouTube video ID")
    return f"https://www.youtube.com/watch?v={video_id}"


def parse_video(value: str) -> str:
    value = value.strip()
    if VIDEO_ID.fullmatch(value):
        return value
    url = urlsplit(value if "://" in value else f"https://{value}")
    parts = url.path.strip("/").split("/")
    if url.scheme not in ("https", "http") or url.username or url.password:
        raise ValueError("enter a YouTube video URL or ID")
    if url.hostname == "youtu.be":
        video_id = parts[0]
    elif url.hostname in YOUTUBE_HOSTS:
        video_id = parse_qs(url.query).get("v", [""])[0] if url.path == "/watch" else ""
        if len(parts) == 2 and parts[0] in ("shorts", "live", "embed"):
            video_id = parts[1]
    else:
        video_id = ""
    canonical_video(video_id)
    return video_id


def slugify(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", normalized)[:80].strip("-")


def default_source_slug(title: str, original: str, external_id: str) -> str:
    if slug := slugify(title):
        return slug
    original = original.strip()
    if original.startswith("@"):
        handle = original
    else:
        try:
            url = urlsplit(original if "://" in original else f"https://{original}")
            handle = unquote(url.path.strip("/").split("/", 1)[0]) if url.hostname in YOUTUBE_HOSTS else ""
        except ValueError:
            handle = ""
    if handle.startswith("@") and (slug := slugify(handle[1:])):
        return slug
    return slugify(external_id)


def safe_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("invalid relative path")
    result = root.joinpath(*parts)
    if result.resolve() != result:
        raise ValueError("symlinks are not served or modified")
    return result


def source_dir(root: Path, slug: str) -> Path:
    if not SLUG.fullmatch(slug):
        raise ValueError("invalid source slug")
    return safe_path(root, slug)


def audio_path(root: Path, slug: str, video_id: str) -> Path:
    canonical_video(video_id)
    return safe_path(source_dir(root, slug), f"{video_id}.m4a")


def youtube_cover(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    domains = ("ytimg.com", "ggpht.com", "googleusercontent.com")
    if parsed.scheme == "https" and not parsed.username and any(
        host == domain or host.endswith(f".{domain}") for domain in domains
    ):
        return url
    return None
