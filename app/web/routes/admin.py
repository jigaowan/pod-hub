from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.common.errors import PodHubError
from app.db.repositories import channels as channel_repo
from app.db.repositories import episodes as episode_repo
from app.db.repositories import sync_runs as sync_run_repo
from app.db.session import get_session_factory
from app.web.viewmodels.channels import to_channel_list_items, to_episode_items, to_sync_run_summary

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("/admin/channels", response_class=HTMLResponse)
def list_channels(request: Request) -> HTMLResponse:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        channels = channel_repo.list_channels(session)
        return templates.TemplateResponse(
            name="channels_list.html",
            request=request,
            context={"channels": to_channel_list_items(channels), "base_url": request.app.state.settings.base_url},
        )
    finally:
        session.close()


@router.get("/admin/channels/new", response_class=HTMLResponse)
def new_channel_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        name="channels_new.html",
        request=request,
        context={"error": None, "source_input": ""},
    )


@router.post("/admin/channels", response_class=HTMLResponse)
def create_channel(request: Request, source_input: str = Form(...)) -> HTMLResponse:
    sync_service = request.app.state.sync_service
    try:
        channel = sync_service.register_channel(source_input)
        return RedirectResponse(
            url=f"/admin/channels/{channel.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    except PodHubError as exc:
        return templates.TemplateResponse(
            name="channels_new.html",
            request=request,
            context={"error": str(exc), "source_input": source_input},
            status_code=status.HTTP_400_BAD_REQUEST,
        )


@router.get("/admin/channels/{channel_id}", response_class=HTMLResponse)
def show_channel(request: Request, channel_id: int) -> HTMLResponse:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        channel = channel_repo.get_channel(session, channel_id)
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
            },
        )
    finally:
        session.close()
