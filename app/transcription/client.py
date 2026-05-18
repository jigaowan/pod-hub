from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib import error, request

from app.common.errors import PodHubError
from app.config import Settings


class TranscriptionError(PodHubError):
    """Raised when Cloudflare transcription fails."""


class CloudflareTranscriptionClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def transcribe_to_vtt(self, audio_path: Path) -> str:
        if not self.settings.transcription_configured:
            raise TranscriptionError("Cloudflare transcription is not configured")
        if self.settings.cloudflare_whisper_url is None or self.settings.cloudflare_api_token is None:
            raise TranscriptionError("Cloudflare transcription is not configured")

        audio_base64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
        payload = json.dumps({"audio": audio_base64}).encode("utf-8")
        transcription_request = request.Request(
            self.settings.cloudflare_whisper_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.settings.cloudflare_api_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(transcription_request, timeout=600) as response:
                response_body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TranscriptionError(f"Cloudflare transcription failed with HTTP {exc.code}: {body}") from exc
        except OSError as exc:
            raise TranscriptionError(f"Cloudflare transcription request failed: {exc}") from exc

        try:
            data = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise TranscriptionError("Cloudflare transcription returned invalid JSON") from exc

        vtt = data.get("vtt")
        if not isinstance(vtt, str) or not vtt.strip():
            raise TranscriptionError("Cloudflare transcription response did not include VTT")
        return vtt
