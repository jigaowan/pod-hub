from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.responses import FileResponse, Response

from podhub.core.services import Services
from podhub.db.repositories import policy_for
from podhub.outputs.base import Artifact, Episode, RenderSourceContext
from podhub.outputs.rss import PodcastRSS
from podhub.storage.paths import COVER, VIDEO_ID, safe_path, source_dir

router = APIRouter()
ROBOTS = {"X-Robots-Tag": "noindex"}


def not_found() -> Response:
    return Response(status_code=404, headers=ROBOTS)


def matches_etag(request: Request, etag: str) -> bool:
    return any(
        token.strip().removeprefix("W/") in ("*", etag)
        for token in request.headers.get("if-none-match", "").split(",")
    )


@router.api_route("/feeds/{token}.xml", methods=["GET", "HEAD"])
def podcast_feed(token: str, request: Request) -> Response:
    services: Services = request.app.state.services
    output = services.config.paths.output_dir
    with services.db.connect() as conn:
        conn.execute("BEGIN")
        source = conn.execute("SELECT * FROM sources WHERE feed_token=?", (token,)).fetchone()
        if source is None:
            return not_found()
        policy = policy_for(source)
        order = (
            "e.sort_key IS NULL,e.sort_key,v.video_id"
            if policy.feed_order == "playlist_serial"
            else "v.published_at DESC,v.video_id DESC"
        )
        episodes = conn.execute(
            f"""SELECT v.video_id,v.title,v.description,v.published_at,v.media_bytes,v.media_duration_s,
                       v.thumbnail_url,e.sort_key
                FROM source_entries e JOIN videos v USING(video_id)
                WHERE e.source_id=? AND e.state='published' AND v.state='ready'
                ORDER BY {order} LIMIT ?""", (source["id"], policy.max_feed_items),
        ).fetchall()
        artifacts: dict[str, list[Artifact]] = {}
        for row in conn.execute(
            """SELECT a.* FROM video_artifacts a JOIN source_entries e USING(source_id,video_id)
               WHERE a.source_id=? AND a.is_public=1 AND e.state='published'""", (source["id"],),
        ):
            if (path := existing_file(output, row["rel_path"])) is not None:
                artifacts.setdefault(row["video_id"], []).append(Artifact(row["kind"], path.name, row["mime_type"]))
    items = tuple(
        Episode(**dict(row), artifacts=tuple(artifacts.get(row["video_id"], ()))) for row in episodes
    )
    ctx = RenderSourceContext(services.config.web.base_url, token, source["title"], source["url"],
                              source["cover_filename"], policy, items)
    rendered = PodcastRSS().render_source(ctx)
    etag = f'"{sha256(rendered.content).hexdigest()}"'
    headers = {**ROBOTS, "ETag": etag, "Cache-Control": "private, no-cache"}
    if matches_etag(request, etag):
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(rendered.content))
    return Response(b"" if request.method == "HEAD" else rendered.content, media_type=rendered.content_type, headers=headers)


def existing_file(root: Path, relative: str) -> Path | None:
    try:
        file = safe_path(root, relative)
        return file if file.is_file() else None
    except (ValueError, OSError):
        return None


def serve(request: Request, path: Path | None, mime: str, cache: str = "private, no-cache") -> Response:
    if path is None:
        return not_found()
    try:
        stat = path.stat()
    except OSError:
        return not_found()
    headers = {**ROBOTS, "Cache-Control": cache}
    response = FileResponse(path, media_type=mime, headers=headers, stat_result=stat)
    if matches_etag(request, response.headers["etag"]):
        return Response(status_code=304, headers={**headers, "ETag": response.headers["etag"]})
    return response


@router.api_route("/media/{token}/{filename}", methods=["GET", "HEAD"])
def media(token: str, filename: str, request: Request) -> Response:
    vid, dot, ext = filename.rpartition(".")
    if not dot or ext != "m4a" or not VIDEO_ID.fullmatch(vid):
        return not_found()
    services: Services = request.app.state.services
    with services.db.connect() as conn:
        entry = conn.execute(
            """SELECT e.entry_media_path,s.slug FROM source_entries e JOIN sources s ON s.id=e.source_id
               WHERE s.feed_token=? AND e.video_id=? AND e.state IN ('published','retiring')""", (token, vid),
        ).fetchone()
    root = services.config.paths.output_dir
    if not entry or not entry["entry_media_path"]:
        return not_found()
    try:
        folder = source_dir(root, entry["slug"])
        path = safe_path(root, entry["entry_media_path"])
        if path.parent != folder or path.suffix != ".m4a" or not path.is_file():
            return not_found()
    except (ValueError, OSError, RuntimeError):
        return not_found()
    return serve(request, path, "audio/mp4")


@router.api_route("/covers/{token}/{filename}", methods=["GET", "HEAD"])
def cover(token: str, filename: str, request: Request) -> Response:
    if not COVER.fullmatch(filename):
        return not_found()
    services: Services = request.app.state.services
    with services.db.connect() as conn:
        source = conn.execute("SELECT slug FROM sources WHERE feed_token=? AND cover_filename=?", (token, filename)).fetchone()
    root = services.config.paths.output_dir
    return serve(request, existing_file(root, f"{source['slug']}/{filename}") if source else None,
                 "image/jpeg", "private, max-age=31536000, immutable")


@router.api_route("/artifacts/{token}/{filename}", methods=["GET", "HEAD"])
def artifact(token: str, filename: str, request: Request) -> Response:
    if filename in (".", "..") or filename.startswith(".") or "/" in filename or "\\" in filename:
        return not_found()
    services: Services = request.app.state.services
    with services.db.connect() as conn:
        entry = conn.execute(
            """SELECT a.rel_path,a.mime_type FROM video_artifacts a JOIN sources s ON s.id=a.source_id
               JOIN source_entries e ON e.source_id=a.source_id AND e.video_id=a.video_id
               WHERE s.feed_token=? AND a.is_public=1 AND e.state IN ('published','retiring')
               AND a.rel_path=s.slug || '/artifacts/' || ?""", (token, filename),
        ).fetchone()
    if entry is None:
        return not_found()
    return serve(request, existing_file(services.config.paths.output_dir, entry["rel_path"]), entry["mime_type"])
