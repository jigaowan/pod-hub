from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse, Response

from app.common.media import AUDIO_FILE_EXTENSION, AUDIO_MIME_TYPE, episode_media_filename
from app.db.repositories import episodes as episode_repo
from app.db.session import get_session_factory
from app.domain.status import EpisodeStatus

router = APIRouter()


@router.get(f"/media/{{episode_id}}.{AUDIO_FILE_EXTENSION}")
def media_file(request: Request, episode_id: int) -> Response:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        episode = episode_repo.get_episode(session, episode_id)
        if episode is None or episode.status != EpisodeStatus.AUDIO_READY or not episode.audio_path:
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        file_path = Path(episode.audio_path)
        if not file_path.exists():
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        return FileResponse(
            file_path,
            media_type=episode.audio_mime_type or AUDIO_MIME_TYPE,
            filename=episode_media_filename(episode.id),
        )
    finally:
        session.close()
