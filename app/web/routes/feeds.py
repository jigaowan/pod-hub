from __future__ import annotations

from fastapi import APIRouter, Request, status
from fastapi.responses import Response

router = APIRouter()


@router.get("/feeds/{channel_slug}.xml")
def channel_feed(request: Request, channel_slug: str) -> Response:
    xml = request.app.state.feed_service.render_channel_feed(channel_slug)
    if xml is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=xml, media_type="application/rss+xml; charset=utf-8")

