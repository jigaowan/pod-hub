"""Standalone JSONL tool. Its interpreter only needs yt-dlp and the stdlib."""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from http.client import HTTPSConnection
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener


PROBE_FIELDS = (
    "id", "title", "description", "duration", "upload_date", "timestamp",
    "release_timestamp", "live_status", "availability", "thumbnail", "channel_id", "channel", "uploader", "chapters",
    "_type", "webpage_url", "url", "media_type", "is_short", "playlist_index",
)
CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{2,128}")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
THUMBNAIL_TIMEOUT = 15
THUMBNAIL_MAX_BYTES = 5_000_000
THUMBNAIL_FORMATS = {
    "image/jpeg": "mjpeg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
}
METADATA_ESCAPES = str.maketrans({char: "\\" + char for char in "\\=;#\n"})
YOUTUBE_WARNING_PREFIX = re.compile(r"^\[youtube\]\s+(?:[A-Za-z0-9_-]{11}:\s*)?")
YOUTUBE_WARNING_DIAGNOSTICS = (
    "skipping client", "skipping unsupported client", "skipping player responses",
    "signature solving failed", "nsig extraction failed", "signature extraction failed",
    "n challenge solving failed", "error solving n challenge", "incomplete data received",
    "cookies are no longer valid", "formats have been skipped", "some formats may be missing",
    "no supported javascript runtime", "no title found in player responses", "malformed storyboard",
    "po token", "formats are possibly damaged", "requested format is not available", "no video formats found",
)
RESTRICTED_AVAILABILITY = {
    "private": "Private video",
    "needs_auth": "Sign in to watch this video",
    "subscriber_only": "This video is available to this channel's members only",
    "premium_only": "This video is only available to YouTube Premium members",
}


def canonical_video(video_id: str) -> str:
    if not VIDEO_ID.fullmatch(video_id):
        raise ValueError("invalid YouTube video ID")
    return f"https://www.youtube.com/watch?v={video_id}"


def youtube_cover(url: str | None, *, thumbnail_only: bool = False) -> str | None:
    if not isinstance(url, str) or not url.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.username is not None or parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = parsed.hostname or ""
    domains = ("ytimg.com",) if thumbnail_only else ("ytimg.com", "ggpht.com", "googleusercontent.com")
    if host == "img.youtube.com" or any(host == domain or host.endswith(f".{domain}") for domain in domains):
        return url
    return None


def emit(event_type: str, **fields: Any) -> None:
    print(json.dumps({"type": event_type, **fields}, default=str), flush=True)


def rejection(info: dict[str, Any], policy: dict[str, Any]) -> str | None:
    if not policy.get("include_shorts", False) and (
        info.get("media_type") == "short" or info.get("is_short")
        or any("/shorts/" in (info.get(key) or "") for key in ("url", "webpage_url"))
    ):
        return "YouTube Short"
    if not policy.get("include_live_archives", True) and info.get("live_status") == "was_live":
        return "Live archive"
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        if duration < policy.get("min_duration_s", 0):
            return "Below minimum duration"
        if policy.get("max_duration_s") and duration > policy["max_duration_s"]:
            return "Above maximum duration"
    title = info.get("title")
    if title:
        include = policy.get("title_include_regex")
        exclude = policy.get("title_exclude_regex")
        if include and not re.search(include, title):
            return "Title does not match inclusion pattern"
        if exclude and re.search(exclude, title):
            return "Title matches exclusion pattern"
    return None


def runtime_options(runtime: str) -> dict[str, dict[str, str]]:
    if not runtime:
        return {}
    name, separator, path = runtime.partition(":")
    if separator:
        return {name: {"path": path}}
    if "/" in runtime:
        return {Path(runtime).name: {"path": runtime}}
    return {runtime: {}}


def version_parts(value: str) -> tuple[int, ...]:
    digits = re.match(r"(\d+(?:\.\d+)+)", value)
    parts = tuple(map(int, digits[0].split("."))) if digits else ()
    return parts + (0,) * max(0, 4 - len(parts))


def executable_version(executable: str) -> str | None:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10)
        return result.stdout.splitlines()[0] if result.returncode == 0 and result.stdout else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def hello(version: str | None, request: dict[str, Any]) -> dict[str, Any]:
    options = runtime_options(request.get("js_runtime", ""))
    runtime, config = next(iter(options.items()), ("", {}))
    ffmpeg = request.get("ffmpeg_path", "ffmpeg")
    ffmpeg_version = None
    if shutil.which(ffmpeg):
        try:
            result = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=10)
            if result.returncode == 0 and result.stdout:
                ffmpeg_version = result.stdout.splitlines()[0]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "ytdlp_version": version,
        "python_version": platform.python_version(),
        "ejs_available": importlib.util.find_spec("yt_dlp_ejs") is not None,
        "js_runtime": runtime,
        "js_runtime_version": executable_version(config.get("path", runtime)) if runtime else None,
        "ffmpeg_version": ffmpeg_version,
    }


class Logger:
    def __init__(self) -> None:
        self.last_error = ""
        self.playability_reason: str | None = None

    def debug(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        msg = str(msg)
        reason, n = YOUTUBE_WARNING_PREFIX.subn("", msg, count=1)
        if n and (reason := reason.strip()) and not any(text in reason.lower() for text in YOUTUBE_WARNING_DIAGNOSTICS):
            self.playability_reason = reason
        emit("warning", msg=msg)

    def error(self, msg: str) -> None:
        self.last_error = msg


def picked(info: dict[str, Any]) -> dict[str, Any]:
    return {key: info[key] for key in PROBE_FIELDS if key in info}


def flat_entries(raw: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for entry in raw.get("entries") or ():
        if not entry:
            continue
        if "entries" in entry:
            yield from flat_entries(entry)
        else:
            yield entry


def resolve(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    if kind == "channel":
        external_id = raw.get("channel_id") or raw.get("id") or ""
        if not CHANNEL_ID.fullmatch(external_id):
            raise ValueError("could not resolve a canonical YouTube channel ID")
        normalized = f"https://www.youtube.com/channel/{external_id}"
    else:
        external_id = raw.get("id") or ""
        if not PLAYLIST_ID.fullmatch(external_id):
            raise ValueError("could not resolve a canonical YouTube playlist ID")
        normalized = f"https://www.youtube.com/playlist?list={external_id}"
    thumbnails = [item for item in raw.get("thumbnails", []) if item.get("url")]
    avatar = [item for item in thumbnails if item.get("id") in ("avatar_uncropped", "avatar")]
    cover = raw.get("thumbnail") or ((avatar or thumbnails)[-1]["url"] if thumbnails else None)
    return {
        "kind": kind, "external_id": external_id, "normalized_url": normalized,
        "title": raw.get("title") or raw.get("channel") or external_id, "cover_url": cover,
    }


class NoPlayableFormats(ValueError):
    expected = True


def exception_chain(exc: BaseException) -> Iterator[BaseException]:
    pending = [exc]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        info = getattr(current, "exc_info", None)
        for cause in (current.__cause__, current.__context__, getattr(current, "cause", None), info[1] if info else None):
            if isinstance(cause, BaseException):
                pending.append(cause)


def http_status(exc: BaseException) -> int | None:
    for error in exception_chain(exc):
        for name in ("status", "status_code", "code"):
            value = getattr(error, name, None)
            if isinstance(value, int) and 400 <= value < 600:
                return value
    return None


def metadata_value(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]", "", value)
    return cleaned.translate(METADATA_ESCAPES)


def video_date(info: dict[str, Any]) -> str | None:
    upload_date = info.get("upload_date")
    if isinstance(upload_date, str):
        try:
            return date.fromisoformat(upload_date).isoformat()
        except ValueError:
            pass
    timestamp = info.get("timestamp")
    if isinstance(timestamp, (int, float)):
        try:
            return datetime.fromtimestamp(timestamp, UTC).date().isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def milliseconds(value: Any) -> int | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 2**53:
        return round(value * 1000)
    return None


def ffmetadata(info: dict[str, Any]) -> str:
    video_url = canonical_video(info["id"])
    description = info.get("description")
    comment = f"{video_url}\n\n{description[:8192]}" if isinstance(description, str) and description else video_url
    tags = {
        "title": info.get("title"), "artist": info.get("channel") or info.get("uploader"),
        "date": video_date(info), "description": comment, "comment": comment,
    }
    lines = [";FFMETADATA1"]
    lines.extend(f"{key}={metadata_value(value)}" for key, value in tags.items() if isinstance(value, str) and value)
    chapters = info.get("chapters")
    duration = milliseconds(info.get("duration"))
    if isinstance(chapters, list):
        entries = []
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            start = milliseconds(chapter.get("start_time"))
            end = milliseconds(chapter.get("end_time"))
            title = chapter.get("title")
            if start is None or end is None or not isinstance(title, str) or not title.strip():
                continue
            if duration:
                end = min(end, duration)
            if start < end:
                entries.append((start, end, metadata_value(title)))
        for start, end, title in sorted(entries):
            lines.extend(("[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={title}"))
    return "\n".join(lines) + "\n"


class ThumbnailRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Handle redirects explicitly without reading their bodies.
        return None


@contextmanager
def thumbnail_response(opener, url: str, proxy: str | None, timeout: float):
    if opener is None:
        # yt-dlp provides Requests when HTTPS proxies are supported by its installation.
        from yt_dlp.dependencies import requests

        if requests is None:
            raise ValueError("HTTPS thumbnail proxy requires yt-dlp's Requests backend")
        with requests.Session() as session:
            request = requests.Request("GET", url, headers={"Accept-Encoding": "identity"})
            prepared = session.prepare_request(request)
            # Use the adapter to keep redirect handling and its body limits under our control.
            with session.get_adapter(url).send(prepared, proxies={"https": proxy}, stream=True, timeout=timeout) as response:
                yield response.status_code, response.headers, response.raw.stream(64 * 1024, decode_content=False)
        return

    try:
        response = opener.open(Request(url), timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        yield response.code, response.headers, iter(lambda: response.read1(64 * 1024), b"")


def download_thumbnail(url: str, target: Path, proxy: str | None) -> str:
    if not youtube_cover(url, thumbnail_only=True):
        raise ValueError("invalid YouTube thumbnail URL")
    if proxy and "://" not in proxy:
        proxy = f"http://{proxy}"
    # Use yt-dlp's transport for SOCKS, with the same HTTPS and redirect restrictions.
    handlers: list[Any] = [ThumbnailRedirectHandler()]
    scheme = urlsplit(proxy).scheme.lower() if proxy else ""
    if scheme in ("socks4", "socks4a", "socks5", "socks5h"):
        from yt_dlp.networking._urllib import make_socks_conn_class

        class SocksHTTPSHandler(HTTPSHandler):
            def https_open(self, req):
                return self.do_open(make_socks_conn_class(HTTPSConnection, proxy), req, context=self._context)

        handlers.extend((ProxyHandler({}), SocksHTTPSHandler()))
    elif proxy and scheme != "https":
        if scheme != "http":
            raise ValueError("unsupported thumbnail proxy protocol")
        handlers.append(ProxyHandler({"https": proxy}))
    opener = None if scheme == "https" else build_opener(*handlers)
    deadline = monotonic() + THUMBNAIL_TIMEOUT
    for _ in range(4):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("thumbnail download timed out")
        with thumbnail_response(opener, url, proxy, remaining) as (status, headers, chunks):
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    raise ValueError("thumbnail redirect is missing its destination")
                url = urljoin(url, location)
                if not youtube_cover(url, thumbnail_only=True):
                    raise ValueError("thumbnail redirect is outside the YouTube thumbnail allowlist")
                continue
            if status != 200:
                raise ValueError(f"thumbnail download returned HTTP {status}")
            decoder = THUMBNAIL_FORMATS.get(headers.get("Content-Type", "").split(";", 1)[0].strip().lower())
            length = headers.get("Content-Length")
            if decoder is None or (length is not None and int(length) > THUMBNAIL_MAX_BYTES):
                raise ValueError("invalid thumbnail type or size")
            with target.open("wb") as output:
                for chunk in chunks:
                    if monotonic() >= deadline:
                        raise TimeoutError("thumbnail download timed out")
                    if output.tell() + len(chunk) > THUMBNAIL_MAX_BYTES:
                        raise ValueError("thumbnail exceeds the download size limit")
                    output.write(chunk)
        return decoder
    raise ValueError("too many thumbnail redirects")


def convert(ffmpeg: str, *args: str, timeout: int | None = None) -> None:
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"ffmpeg conversion failed: {result.stderr[-1500:]}")


def mux_audio(ffmpeg: str, audio: Path, target: Path, *, is_aac: bool,
              metadata: Path | None = None, cover: Path | None = None) -> None:
    inputs = ["-i", str(audio)]
    mapping = ["-map", "0:a:0"]
    if cover is not None:
        inputs += ["-i", str(cover)]
        mapping += ["-map", "1:v:0", "-c:v", "mjpeg", "-disposition:v:0", "attached_pic"]
    metadata_index = "-1"
    if metadata is not None:
        inputs += ["-f", "ffmetadata", "-i", str(metadata)]
        metadata_index = "2" if cover is not None else "1"
    codec = ["-c:a", "copy"] if is_aac else ["-c:a", "aac", "-b:a", "128k"]
    convert(
        ffmpeg, *inputs, *mapping, *codec, "-map_metadata", metadata_index, "-map_chapters", metadata_index,
        "-movflags", "+faststart", "-f", "ipod", "--", str(target),
    )


def finish_audio(request: dict[str, Any], info: dict[str, Any], audio: Path, workspace: Path, *, is_aac: bool) -> Path:
    ffmpeg = request.get("ffmpeg_path", "ffmpeg")
    target = workspace / "audio.m4a"
    metadata = workspace / "episode.ffmetadata"
    try:
        metadata.write_text(ffmetadata(info), encoding="utf-8")
    except Exception as exc:
        emit("warning", msg=f"Could not write episode metadata tags ({type(exc).__name__}); using audio only")
    else:
        if thumbnail := info.get("thumbnail"):
            downloaded = workspace / "thumbnail.bin"
            cover = workspace / "thumbnail.jpg"
            try:
                decoder = download_thumbnail(thumbnail, downloaded, request.get("proxy"))
                convert(
                    ffmpeg, "-protocol_whitelist", "file", "-max_pixels", "16000000",
                    "-f", "image2", "-pattern_type", "none", "-c:v", decoder, "-i", str(downloaded),
                    "-map", "0:v:0", "-frames:v", "1", "-map_metadata", "-1", "-c:v", "mjpeg",
                    "--", str(cover), timeout=THUMBNAIL_TIMEOUT,
                )
                mux_audio(ffmpeg, audio, target, is_aac=is_aac, metadata=metadata, cover=cover)
                return target
            except Exception as exc:
                emit("warning", msg=f"Could not embed episode thumbnail ({type(exc).__name__}); using audio and metadata")
        try:
            mux_audio(ffmpeg, audio, target, is_aac=is_aac, metadata=metadata)
            return target
        except Exception as exc:
            emit("warning", msg=f"Could not embed episode metadata tags ({type(exc).__name__}); using audio only")
    mux_audio(ffmpeg, audio, target, is_aac=is_aac)
    return target


def run(request: dict[str, Any]) -> int:
    logger = Logger()
    phase = "extract"
    try:
        try:
            from yt_dlp import YoutubeDL
            from yt_dlp.version import __version__
        except ImportError:
            emit("hello", **hello(None, request))
            raise
        report = hello(__version__, request) if request["mode"] == "doctor" else {
            "ytdlp_version": __version__,
            "python_version": platform.python_version(),
            "ejs_available": importlib.util.find_spec("yt_dlp_ejs") is not None,
            "js_runtime": request.get("js_runtime", ""),
        }
        emit("hello", **report)
        if request["mode"] == "doctor":
            return 0
        minimum = request.get("min_ytdlp_version")
        if minimum and version_parts(__version__) < version_parts(minimum):
            raise RuntimeError(f"yt-dlp {__version__} is older than {minimum}")
        if request.get("cookiefile"):
            request["cookiefile"] = str(Path(request["cookiefile"]).absolute())
        fetch = request["mode"] == "fetch"
        listing = request["mode"] in ("list", "resolve")
        params: dict[str, Any] = {
            "logger": logger, "quiet": True, "no_warnings": False, "noprogress": True,
            "ignoreconfig": True, "socket_timeout": 30, "extractor_retries": 1,
            "fragment_retries": 5, "retries": 0, "skip_unavailable_fragments": False,
            "noplaylist": not listing, "ignore_no_formats_error": fetch,
            "ffmpeg_location": request.get("ffmpeg_path", "ffmpeg"),
            "js_runtimes": runtime_options(request.get("js_runtime", "")),
        }
        if not Path(params["ffmpeg_location"]).is_absolute():
            params["ffmpeg_location"] = shutil.which(params["ffmpeg_location"]) or params["ffmpeg_location"]
        for key in ("cookiefile", "proxy"):
            if request.get(key):
                params[key] = request[key]
        extractor_args: dict[str, Any] = {}
        if request.get("extractor_args"):
            for section in request["extractor_args"].split("|"):
                extractor, arguments = section.split(":", 1)
                extractor_args[extractor.strip().lower()] = {
                    key.strip().lower(): value.split(",") if equals else [""]
                    for option in arguments.split(";")
                    for key, equals, value in [option.partition("=")]
                    if key.strip()
                }
        if listing:
            extractor_args.setdefault("youtubetab", {}).setdefault("approximate_date", [""])
            start = max(1, int(request.get("start") or 1))
            limit = request.get("limit")
            playlistend = (start + int(limit) - 1) if limit else None
            params.update(extract_flat="in_playlist", lazy_playlist=True, playliststart=start, playlistend=playlistend)
        if request["mode"] == "resolve":
            params["playlist_items"] = "0"
        params["extractor_args"] = extractor_args
        workspace = Path(request.get("workspace") or ".").absolute()
        final_paths: list[str] = []
        if fetch:
            def progress(info: dict[str, Any]) -> None:
                emit("progress", downloaded_bytes=info.get("downloaded_bytes"),
                     total_bytes=info.get("total_bytes") or info.get("total_bytes_estimate"), speed=info.get("speed"))

            params.update(format=request["policy"]["audio_format"], outtmpl=str(workspace / "input.%(ext)s"),
                          progress_hooks=[progress], post_hooks=[final_paths.append])
        with YoutubeDL(params) as ydl:
            emit("phase", phase=phase)
            raw = ydl.extract_info(request["url"], download=False, process=False)
            if listing:
                for _ in range(3):
                    if not raw or raw.get("_type") not in ("url", "url_transparent"):
                        break
                    redirected = raw.get("url", "")
                    target = urlsplit(redirected)
                    if target.scheme != "https" or target.hostname not in (
                        "www.youtube.com", "youtube.com", "music.youtube.com", "m.youtube.com",
                    ) or target.path == "/watch":
                        raise ValueError("source lookup redirected away from a channel or playlist")
                    raw = ydl.extract_info(redirected, download=False, process=False)
                if raw and raw.get("_type") in ("url", "url_transparent"):
                    raise ValueError("source lookup exceeded the redirect limit")
            if not raw:
                raise RuntimeError(logger.last_error or "extractor returned no metadata")
            if request["mode"] == "resolve":
                emit("result", **resolve(raw, request["kind"]))
                return 0
            if request["mode"] == "list":
                start = max(1, int(request.get("start") or 1))
                for index, entry in enumerate(flat_entries(raw), 1):
                    if request.get("limit") and index > request["limit"]:
                        break
                    emit("entry", **picked(entry), sort_key=start + index - 1)
                return 0
            info = picked(raw)
            info["formats_count"] = len(raw.get("formats") or [])
            emit("probe", info=info)
            if not fetch:
                return 0
            if raw.get("live_status") in ("is_upcoming", "is_live", "post_live"):
                emit("phase", phase="gate")
                emit("gate", action="defer", reason=raw["live_status"], release_timestamp=raw.get("release_timestamp"))
                return 0
            if not raw.get("formats") and not raw.get("url"):
                raise NoPlayableFormats(
                    logger.playability_reason or RESTRICTED_AVAILABILITY.get(raw.get("availability"))
                    or "Requested format is not available"
                )
            emit("phase", phase="gate")
            policies = request.get("entry_policies") or {"source": request["policy"]}
            rejected = {key: reason for key, policy in policies.items() if (reason := rejection(info, policy))}
            if rejected and len(rejected) == len(policies):
                emit("gate", action="filter", reason=next(iter(rejected.values())), rejected=rejected)
                return 0
            phase = "download"
            emit("phase", phase=phase)
            result = ydl.process_ie_result(raw, download=True)
            if not result:
                raise RuntimeError(logger.last_error or "downloader returned no result")
            downloads = result.get("requested_downloads") or []
            input_name = final_paths[-1] if final_paths else (downloads[-1] if downloads else result).get("filepath")
            if not input_name:
                raise RuntimeError("download result did not include a filepath")
            phase = "postprocess"
            emit("phase", phase=phase)
            stream = downloads[-1] if downloads else result
            is_aac = str(stream.get("acodec", "")).startswith(("mp4a", "aac"))
            target = finish_audio(request, info, Path(input_name).absolute(), workspace, is_aac=is_aac)
            emit("result", filepath=str(target), info=info, rejected=rejected)
            return 0
    except Exception as exc:
        # YoutubeDL can wrap the extractor exception in DownloadError.
        error = next((cause for cause in exception_chain(exc) if type(cause).__name__ == "GeoRestrictedError"), exc)
        message = str(exc) if isinstance(exc, NoPlayableFormats) else logger.last_error or str(exc)
        emit("error", phase=phase, exc_class=type(error).__name__, expected=bool(getattr(error, "expected", False)),
             http_status=http_status(exc), msg=message,
             traceback_tail=traceback.format_exc()[-2000:])
        return 1


if __name__ == "__main__":
    try:
        request_data = json.load(sys.stdin)
        status = run(request_data)
    except KeyboardInterrupt:
        status = 130
    sys.exit(status)
