from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.common.errors import PodHubError
from app.common.time import browser_datetime_fallback, browser_datetime_iso
from app.db.repositories import channels as channel_repo
from app.db.repositories import episodes as episode_repo
from app.db.repositories import sync_runs as sync_run_repo
from app.db.session import get_session_factory
from app.web.viewmodels.channels import to_channel_list_items, to_episode_items, to_sync_run_summary

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["browser_datetime_iso"] = browser_datetime_iso
templates.env.globals["browser_datetime_fallback"] = browser_datetime_fallback


def get_next_sync_at(request: Request):
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return None

    job = scheduler.get_job("channel-sync")
    if job is None:
        return None

    return job.next_run_time


def reschedule_channel_sync(request: Request, sync_interval: int) -> None:
    scheduler = getattr(request.app.state, "scheduler", None)
    if scheduler is None:
        return
    scheduler.reschedule_job("channel-sync", trigger="interval", minutes=sync_interval)


@router.get("/admin/channels", response_class=HTMLResponse)
def list_channels(request: Request) -> HTMLResponse:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        channels = channel_repo.list_channels(session)
        return templates.TemplateResponse(
            name="channels_list.html",
            request=request,
            context={
                "channels": to_channel_list_items(channels),
                "base_url": request.app.state.settings.base_url,
                "next_sync_at": get_next_sync_at(request),
            },
        )
    finally:
        session.close()


@router.get("/admin/channels/new", response_class=HTMLResponse)
def new_channel_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="channels_new.html",
        request=request,
        context={
            "error": None,
            "source_input": "",
            "transcription_enabled": False,
            "next_sync_at": get_next_sync_at(request),
        },
    )


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="settings.html",
        request=request,
        context={
            "settings_view": request.app.state.runtime_settings_service.get_admin_view(),
            "form_values": {},
            "error": None,
            "saved": request.query_params.get("saved") == "1",
        },
    )


@router.post("/admin/settings", response_class=HTMLResponse)
async def update_settings(request: Request) -> HTMLResponse:
    form = await request.form()
    form_values = {
        key: str(value)
        for key, value in form.items()
        if key not in {"alarm_custom_psk", "alarm_tg_user_id", "cloudflare_api_token"}
    }
    try:
        effective_settings = request.app.state.runtime_settings_service.save_form(form)
        reschedule_channel_sync(request, effective_settings.sync_interval)
    except PodHubError as exc:
        return templates.TemplateResponse(
            name="settings.html",
            request=request,
            context={
                "settings_view": request.app.state.runtime_settings_service.get_admin_view(),
                "form_values": form_values,
                "error": str(exc),
                "saved": False,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return RedirectResponse(url="/admin/settings?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/channels", response_class=HTMLResponse)
def create_channel(
    request: Request,
    source_input: str = Form(...),
    cookie_text: str | None = Form(default=None),
    transcription_enabled: bool = Form(default=False),
) -> HTMLResponse:
    channel_service = request.app.state.channel_service
    try:
        channel = channel_service.register_channel(
            source_input,
            cookie_text=cookie_text,
            transcription_enabled=transcription_enabled,
        )
        return RedirectResponse(
            url=f"/admin/channels/{channel.slug}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except PodHubError as exc:
        return templates.TemplateResponse(
            name="channels_new.html",
            request=request,
            context={
                "error": str(exc),
                "source_input": source_input,
                "transcription_enabled": transcription_enabled,
                "next_sync_at": get_next_sync_at(request),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )


def render_channel_detail(
    request: Request,
    channel_slug: str,
    *,
    status_code: int = status.HTTP_200_OK,
    cookie_error: str | None = None,
) -> HTMLResponse:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        channel = channel_repo.get_channel_by_slug(session, channel_slug)
        if channel is None:
            return HTMLResponse("Channel not found", status_code=status.HTTP_404_NOT_FOUND)

        episodes = episode_repo.list_episodes_for_channel(session, channel.id)
        latest_sync_run = sync_run_repo.get_latest_sync_run(session, channel.id)
        return templates.TemplateResponse(
            name="channels_detail.html",
            request=request,
            context={
                "channel": channel,
                "episodes": to_episode_items(episodes),
                "sync_run": to_sync_run_summary(latest_sync_run),
                "base_url": request.app.state.settings.base_url,
                "next_sync_at": get_next_sync_at(request),
                "cookie_error": cookie_error,
            },
            status_code=status_code,
        )
    finally:
        session.close()


@router.get("/admin/channels/{channel_slug}", response_class=HTMLResponse)
def show_channel(request: Request, channel_slug: str) -> HTMLResponse:
    return render_channel_detail(request, channel_slug)


@router.post("/admin/channels/{channel_slug}/cookie", response_class=HTMLResponse)
def update_channel_cookie(
    request: Request,
    channel_slug: str,
    cookie_text: str | None = Form(default=None),
) -> HTMLResponse:
    try:
        updated = request.app.state.channel_service.update_channel_cookie(channel_slug, cookie_text)
    except PodHubError as exc:
        return render_channel_detail(
            request,
            channel_slug,
            status_code=status.HTTP_400_BAD_REQUEST,
            cookie_error=str(exc),
        )

    if not updated:
        return HTMLResponse("Channel not found", status_code=status.HTTP_404_NOT_FOUND)

    return RedirectResponse(url=f"/admin/channels/{channel_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/channels/{channel_slug}/cookie/delete")
def delete_channel_cookie(request: Request, channel_slug: str) -> HTMLResponse:
    deleted = request.app.state.channel_service.delete_channel_cookie(channel_slug)
    if not deleted:
        return HTMLResponse("Channel not found", status_code=status.HTTP_404_NOT_FOUND)

    return RedirectResponse(url=f"/admin/channels/{channel_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/channels/{channel_slug}/transcription")
def update_channel_transcription(
    request: Request,
    channel_slug: str,
    transcription_enabled: bool = Form(default=False),
) -> HTMLResponse:
    updated = request.app.state.channel_service.update_channel_transcription(channel_slug, transcription_enabled)
    if not updated:
        return HTMLResponse("Channel not found", status_code=status.HTTP_404_NOT_FOUND)

    return RedirectResponse(url=f"/admin/channels/{channel_slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/admin/channels/{channel_slug}/delete")
def delete_channel(request: Request, channel_slug: str) -> HTMLResponse:
    deleted = request.app.state.channel_service.delete_channel(channel_slug)
    if not deleted:
        return HTMLResponse("Channel not found", status_code=status.HTTP_404_NOT_FOUND)

    return RedirectResponse(url="/admin/channels", status_code=status.HTTP_303_SEE_OTHER)
