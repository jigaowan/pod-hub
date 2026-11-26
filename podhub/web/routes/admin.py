from __future__ import annotations

import base64
import hmac
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.datastructures import FormData
from starlette.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from podhub.core.services import NotFound, Services
from podhub.db.connection import get_meta
from podhub.db.repositories import policy_for
from podhub.domain.enums import Category, SourceKind
from podhub.domain.models import SourceIdentity, utc_now
from podhub.domain.policy import SourcePolicy
from podhub.outputs.base import feed_url
from podhub.outputs.opml import render_opml
from podhub.storage.paths import youtube_cover

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=Path(__file__).parents[1] / "templates")
HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex", "Referrer-Policy": "no-referrer"}
TABS = {
    "active": ("Active", "v.state IN ('running','pending')"),
    "deferred": ("Deferred", "v.state='deferred'"),
    "failed": ("Failed", "v.state='failed'"),
    "unclassified": ("Unclassified", "v.unclassified=1"),
    "unavailable": ("Unavailable", "v.state='unavailable'"),
    "ready": ("Ready", "v.state='ready'"),
}
POLICY_BOOLEAN = ("include_shorts", "include_live_archives")
PREVIEW_FIELDS = (
    "initial_backfill_count", "min_duration_s", "max_duration_s", "title_include_regex", "title_exclude_regex",
    *POLICY_BOOLEAN,
)
POLICY_EDIT = (
    *PREVIEW_FIELDS, "keep_count", "keep_days", "unpublish_grace_hours", "max_feed_items", "feed_order",
    "playlist_scan_mode", "max_scan_items", "early_stop_known", "audio_format",
)


def text(form: FormData, name: str, default: str = "") -> str:
    value = form.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"{name} must contain text")
    return value


def number(form: FormData, name: str) -> int | None:
    value = text(form, name)
    return int(value) if value else None


def form_policy(form: FormData, default: SourcePolicy, fields: tuple[str, ...]) -> SourcePolicy:
    values = default.model_dump()
    for field in fields:
        if field in POLICY_BOOLEAN:
            values[field] = text(form, field) == "on"
        elif field in form:
            values[field] = text(form, field)
    return SourcePolicy.model_validate(values)


def render(request: Request, filename: str, status_code: int = 200, **context: Any) -> Response:
    services: Services = request.app.state.services
    return templates.TemplateResponse(
        request, filename, {"base_url": services.config.web.base_url, **context},
        headers=HEADERS, status_code=status_code,
    )


def redirect(location: str) -> Response:
    return RedirectResponse(location, status_code=303, headers=HEADERS)


def cookie_id(form: FormData) -> int | None:
    return number(form, "cookie_profile_id")


@asynccontextmanager
async def checked_form(request: Request) -> AsyncIterator[FormData]:
    services: Services = request.app.state.services
    origin = request.headers.get("origin")
    allowed = {urlsplit(str(request.base_url)).netloc, urlsplit(services.config.web.base_url).netloc}
    if request.headers.get("sec-fetch-site") == "cross-site" or (
        origin and (urlsplit(origin).netloc not in allowed or urlsplit(origin).scheme not in ("http", "https"))
    ):
        raise HTTPException(403, "cross-origin form submission")
    async with request.form(max_fields=50, max_files=1, max_part_size=2_000_000) as form:
        yield form


def age(value: str | None) -> str:
    if not value:
        return "Never"
    seconds = max(0, int((utc_now() - datetime.fromisoformat(value)).total_seconds()))
    if seconds < 60:
        return "Less than a minute ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def overview(request: Request, *, partial: bool = False) -> Response:
    s: Services = request.app.state.services
    with s.db.connect() as conn:
        paused = get_meta(conn, "global_paused") == "1"
        reason = get_meta(conn, "global_pause_reason")
        report = json.loads(get_meta(conn, "last_doctor", "{}"))
        attempts = conn.execute("SELECT * FROM attempts ORDER BY id DESC LIMIT 20").fetchall()
        last = conn.execute("SELECT max(ended_at) FROM attempts WHERE kind='download' AND outcome='ok'").fetchone()[0]
        counts = dict(conn.execute(
            """SELECT state,count(*) FROM videos v WHERE state='ready' OR EXISTS
               (SELECT 1 FROM source_entries e WHERE e.video_id=v.video_id AND e.state='wanted') GROUP BY state""",
        ).fetchall())
        orphans = json.loads(get_meta(conn, "orphan_audio", "[]"))
    return render(request, "overview_panel.html" if partial else "overview.html",
                  nav="overview", title="Overview", paused=paused, reason=reason, breakers=s.list_breakers(),
                  report=report, attempts=attempts, last_download=age(last), counts=counts, disk=s.disk_space(), orphans=orphans)


@router.get("")
def overview_page(request: Request) -> Response:
    return overview(request)


@router.get("/status")
def overview_partial(request: Request) -> Response:
    return overview(request, partial=True)


@router.post("/system")
async def system_action(request: Request) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        action = text(form, "action")
        if action in ("pause", "resume"):
            s.pause(action == "pause")
        elif action == "reset":
            await s.reset_breaker(text(form, "key"))
        elif action == "doctor":
            await s.doctor()
        else:
            raise ValueError("invalid system action")
    return redirect("/admin")


def sources_page(request: Request, **extra: Any) -> Response:
    s: Services = request.app.state.services
    return render(request, "sources.html", nav="sources", title="Sources", sources=s.sources(),
                  cookies=s.cookies(), **{"policy": s.config.defaults, **extra})


@router.get("/sources")
def list_sources(request: Request) -> Response:
    return sources_page(request)


def preview_key(s: Services) -> bytes:
    with s.db.connect() as conn:
        return bytes.fromhex(get_meta(conn, "preview_key"))


def issue_preview(s: Services, payload: dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps({**payload, "expires": time() + 900}).encode()).decode()
    return f"{encoded}.{hmac.digest(preview_key(s), encoded.encode(), 'sha256').hex()}"


def read_preview(s: Services, token: str) -> dict[str, Any]:
    encoded, _, signature = token.partition(".")
    expected = hmac.digest(preview_key(s), encoded.encode(), "sha256").hex()
    if not signature.isascii() or not hmac.compare_digest(signature, expected):
        raise ValueError("invalid source preview")
    payload = json.loads(base64.urlsafe_b64decode(encoded))
    if payload["expires"] < time():
        raise ValueError("source preview expired; preview the source again")
    return payload


@router.post("/sources/preview")
async def preview_source(request: Request) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        value = text(form, "source_input").strip()
        selected = text(form, "kind")
        kind = SourceKind(selected) if selected else None
        profile = cookie_id(form)
        policy = form_policy(form, s.config.defaults, PREVIEW_FIELDS)
        preview = await s.preview(value, kind, profile)
    ticket = issue_preview(s, {"identity": asdict(preview.identity), "original": value,
                               "policy": policy.model_dump(), "cookie": profile})
    entries = [(item, policy.rejection(item)) for item in preview.entries]
    context = {"preview": preview, "ticket": ticket, "entries": entries, "policy": policy,
               "preview_cover": youtube_cover(preview.identity.cover_url),
               "suggested_slug": s.suggest_slug(preview.identity, value),
               "value": value, "selected": selected, "profile": profile}
    if request.headers.get("hx-request") == "true":
        return render(request, "source_preview.html", **context)
    return sources_page(request, **context)


@router.post("/sources")
async def confirm_source(request: Request) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        payload = read_preview(s, text(form, "ticket"))
        identity = SourceIdentity(**payload["identity"])
        changes = payload["policy"]
        changes["initial_backfill_count"] = number(form, "initial_backfill_count")
        sid = await s.add_source(
            identity, payload["original"], SourcePolicy.model_validate(changes), payload["cookie"],
            title=text(form, "title", identity.title), slug=text(form, "slug") or None,
        )
    return redirect(f"/admin/sources/{sid}")


@router.get("/sources/{sid}")
def source_detail(request: Request, sid: int, page: int = Query(1, ge=1, le=100000)) -> Response:
    s: Services = request.app.state.services
    source = s.source(sid)
    rows, more = s.source_episodes(sid, page=page)
    return render(
        request, "source_detail.html", title=source["title"], nav="sources", source=source,
        policy=policy_for(source), rows=rows, page=page, more=more, cookies=s.cookies(),
        rss=feed_url(s.config.web.base_url, source["feed_token"]),
    )


@router.post("/sources/{sid}")
async def source_action(request: Request, sid: int) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        action = text(form, "action")
        if action == "policy":
            source = s.source(sid)
            interval = number(form, "check_interval")
            if interval is None:
                raise ValueError("enter a check interval")
            s.update_source(sid, form_policy(form, policy_for(source), POLICY_EDIT), cookie_id(form), interval)
        elif action == "rename":
            s.rename_source(sid, text(form, "title"))
        elif action == "add-video":
            s.add_video(sid, text(form, "video"))
        elif action == "delete":
            selection = text(form, "files")
            if selection not in ("keep", "delete"):
                raise ValueError("choose how to handle the source folder")
            s.delete_source(sid, delete_files=selection == "delete")
            return redirect("/admin/sources")
        else:
            s.source_command(sid, action, count=number(form, "count") or 0)
        if text(form, "from_list") == "yes":
            return redirect("/admin/sources")
    return redirect(f"/admin/sources/{sid}")


@router.post("/sources/{sid}/episodes/{vid}")
async def episode_action(request: Request, sid: int, vid: str) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        s.episode_command(sid, vid, text(form, "action"))
    return redirect(f"/admin/sources/{sid}")


@router.get("/videos")
def videos_page(request: Request, status: str = "active", page: int = Query(1, ge=1, le=100000)) -> Response:
    if status not in TABS:
        raise ValueError("unknown video status")
    s: Services = request.app.state.services
    with s.db.connect() as conn:
        rows = conn.execute(
            f"""SELECT v.* FROM videos v WHERE {TABS[status][1]}
                AND (v.state!='pending' OR EXISTS (SELECT 1 FROM source_entries e
                WHERE e.video_id=v.video_id AND e.state='wanted'))
                ORDER BY v.priority DESC,v.updated_at DESC,v.video_id LIMIT 101 OFFSET ?""",
            ((page - 1) * 100,),
        ).fetchall()
    return render(request, "videos.html", title="Videos & Queue", nav="videos", rows=rows[:100],
                  tabs=TABS, status=status, page=page, more=len(rows) > 100, categories=list(Category))


@router.post("/videos")
async def retry_videos(request: Request) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        s.retry(category=text(form, "category"))
    return redirect("/admin/videos")


@router.get("/videos/{vid}")
def video_detail(request: Request, vid: str) -> Response:
    s: Services = request.app.state.services
    with s.db.connect() as conn:
        video = conn.execute("SELECT * FROM videos WHERE video_id=?", (vid,)).fetchone()
        attempts = conn.execute("SELECT * FROM attempts WHERE video_id=? ORDER BY id DESC", (vid,)).fetchall()
        folders = conn.execute(
            """SELECT s.id,s.slug,s.title,e.state,e.entry_media_path FROM source_entries e
               JOIN sources s ON s.id=e.source_id WHERE e.video_id=? ORDER BY s.id""", (vid,),
        ).fetchall()
    if video is None:
        raise NotFound("video does not exist")
    return render(request, "video_detail.html", title=video["title"], nav="videos", video=video,
                  attempts=attempts, folders=folders)


@router.post("/videos/{vid}")
async def video_action(request: Request, vid: str) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        s.video_command(vid, text(form, "action"))
    return redirect(f"/admin/videos/{vid}")


@router.get("/cookies")
def cookies_page(request: Request, validated: str = "") -> Response:
    s: Services = request.app.state.services
    return render(request, "cookies.html", title="Cookies & Config", nav="cookies", rows=s.cookies(),
                  config=json.dumps(s.config.redacted(), indent=2), validated=validated)


@router.post("/cookies")
async def cookies_action(request: Request) -> Response:
    s: Services = request.app.state.services
    async with checked_form(request) as form:
        action = text(form, "action")
        profile = number(form, "id")
        if action == "save":
            s.save_cookie(text(form, "name"), text(form, "contents"), text(form, "note"), profile)
        elif action == "delete" and profile:
            s.delete_cookie(profile)
        elif action == "validate" and profile:
            valid = await s.validate_cookie(profile, text(form, "video"))
            return redirect(f"/admin/cookies?validated={'ok' if valid else 'failed'}")
        else:
            raise ValueError("invalid cookie action")
    return redirect("/admin/cookies")


@router.get("/export/opml")
def export(request: Request) -> Response:
    s: Services = request.app.state.services
    opml = render_opml(s.config.web.base_url, (
        (row["title"], row["feed_token"], row["url"]) for row in s.sources() if row["enabled"]
    ))
    return Response(opml.content, media_type=opml.content_type, headers={
        **HEADERS, "Content-Disposition": 'attachment; filename="pod-hub.opml"',
    })


async def user_error(request: Request, error: Exception) -> Response:
    status = 404 if isinstance(error, NotFound) else 400
    if isinstance(error, sqlite3.IntegrityError):
        message = "The change conflicts with an existing record."
    elif hasattr(error, "errors"):
        # Pydantic's normal display can include raw secrets or form contents.
        message = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors())
    else:
        message = str(error)
    if request.headers.get("hx-request") == "true":
        return render(request, "error_fragment.html", status, message=message)
    return render(request, "error.html", status, title="Request failed", message=message)
