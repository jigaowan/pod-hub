from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request, status
from fastapi.responses import FileResponse, Response

from app.db.repositories import episodes as episode_repo
from app.db.session import get_session_factory
from app.domain.status import TranscriptStatus

router = APIRouter()


@router.get("/transcripts/{episode_id}.vtt")
def transcript_file(request: Request, episode_id: int) -> Response:
    session_factory = get_session_factory(request.app.state.settings)
    session = session_factory()
    try:
        episode = episode_repo.get_episode(session, episode_id)
        if episode is None or episode.transcript_status != TranscriptStatus.READY or not episode.transcript_path:
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        file_path = Path(episode.transcript_path)
        if not file_path.exists() or not file_path.is_file():
            return Response(status_code=status.HTTP_404_NOT_FOUND)

        return FileResponse(
            file_path,
            media_type=episode.transcript_mime_type or "text/vtt",
            filename=f"{episode.id}.vtt",
        )
    finally:
        session.close()
