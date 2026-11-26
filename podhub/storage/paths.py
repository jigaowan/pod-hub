from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from pathvalidate import ValidationError, sanitize_filename, validate_filename

from podhub.domain.models import parse_date
from podhub.ytdlp.shim import VIDEO_ID as VIDEO_ID, canonical_video as canonical_video, youtube_cover as youtube_cover

COVER = re.compile(r"cover\.[a-f0-9]{16,64}\.jpg")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
FOLDER_NAME_MAX_BYTES = 120


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


def _normalize_folder_name(name: str) -> str:
    return " ".join(unicodedata.normalize("NFC", name).split())


def _invalid_folder_character(char: str) -> bool:
    return unicodedata.category(char) in {"Cc", "Cs"}


def validate_folder_name(name: str) -> str:
    if any(_invalid_folder_character(char) for char in name):
        raise ValueError("folder name contains a control character or invalid Unicode")
    name = _normalize_folder_name(name)
    if not name or name.startswith("."):
        raise ValueError("folder name must be non-empty and cannot begin with a dot")
    try:
        validate_filename(name, platform="universal", max_len=FOLDER_NAME_MAX_BYTES, fs_encoding="utf-8")
        stem, dot, _ = name.partition(".")
        if dot and stem.endswith(" "):
            # Windows also treats a space-padded device name before an extension as reserved.
            validate_filename(stem.rstrip(), platform="universal", fs_encoding="utf-8")
    except ValidationError as exc:
        raise ValueError(f"invalid folder name: {exc}") from exc
    return name


def sanitize_folder_name(raw: str) -> str:
    name = "".join(char for char in _normalize_folder_name(raw) if not _invalid_folder_character(char))
    name = sanitize_filename(name, platform="universal", max_len=FOLDER_NAME_MAX_BYTES, fs_encoding="utf-8")
    try:
        return validate_folder_name(name.lstrip(". "))
    except ValueError:
        return ""


def default_source_slug(title: str, original: str, external_id: str) -> str:
    if slug := sanitize_folder_name(title):
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
    if handle.startswith("@") and (slug := sanitize_folder_name(handle[1:])):
        return slug
    return sanitize_folder_name(external_id)


def folder_name_key(name: str) -> str:
    """Compare names independent of case and Unicode composition."""
    return unicodedata.normalize("NFC", name.casefold())


def safe_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or ".." in parts:
        raise ValueError("invalid relative path")
    result = root.joinpath(*parts)
    if result.resolve() != result:
        raise ValueError("symlinks are not served or modified")
    return result


def source_dir(root: Path, slug: str) -> Path:
    return safe_path(root, validate_folder_name(slug))


def audio_filename(
    video_id: str, title: str | None = None, published_at: str | None = None, upload_date: str | None = None,
) -> str:
    canonical_video(video_id)
    clean_title = sanitize_folder_name(title) if title else ""
    published = parse_date(published_at) or parse_date(upload_date)
    day = published[:10].replace("-", "") if published else ""
    prefix = " - ".join(part for part in (day, clean_title) if part)
    name = f"{prefix} [{video_id}].m4a" if prefix else f"{video_id}.m4a"
    validate_filename(name, platform="universal", max_len=200, fs_encoding="utf-8")
    return name


def audio_path(root: Path, slug: str, video_id: str, title: str | None = None,
               published_at: str | None = None, upload_date: str | None = None) -> Path:
    return safe_path(source_dir(root, slug), audio_filename(video_id, title, published_at, upload_date))
