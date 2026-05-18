from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time

from app.common.errors import DownloaderError
from app.config import Settings

MAX_ERROR_LENGTH = 4096


class YtDlpRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("podhub.downloader.runner")

    def run_json(self, command_type: str, extra_args: list[str], cookie_text: str | None = None) -> dict:
        completed = self.run_command(command_type, [*extra_args], timeout=1800, cookie_text=cookie_text)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            error = DownloaderError(f"yt-dlp returned invalid JSON for {command_type}")
            error.stdout = completed.stdout
            error.stderr = completed.stderr
            error.returncode = completed.returncode
            error.command_type = command_type
            raise error from exc

    def run_command(
        self,
        command_type: str,
        extra_args: list[str],
        *,
        timeout: int,
        cookie_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [self.settings.ytdlp_bin]
        if self.settings.ytdlp_proxy:
            command.extend(["--proxy", self.settings.ytdlp_proxy])
        if self.settings.ytdlp_js_runtimes:
            command.extend(["--js-runtimes", self.settings.ytdlp_js_runtimes])
        if self.settings.ytdlp_socket_timeout:
            command.extend(["--socket-timeout", str(self.settings.ytdlp_socket_timeout)])
        if self.settings.ytdlp_retries:
            command.extend(["--retries", str(self.settings.ytdlp_retries)])
        if self.settings.ytdlp_fragment_retries:
            command.extend(["--fragment-retries", str(self.settings.ytdlp_fragment_retries)])
        with self._temporary_cookie_file(cookie_text) as cookie_path:
            if cookie_path is not None:
                command.extend(["--cookies", str(cookie_path)])
            command.extend(extra_args)
            started = time.monotonic()
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        duration = time.monotonic() - started
        self.logger.info("yt-dlp command=%s duration=%.3fs returncode=%s", command_type, duration, completed.returncode)

        if completed.returncode != 0:
            stderr = self._summarize_error(completed.stderr or completed.stdout)
            error = DownloaderError(f"{command_type} failed: {stderr}")
            error.stdout = completed.stdout
            error.stderr = completed.stderr
            error.returncode = completed.returncode
            error.command_type = command_type
            raise error

        return completed

    @contextmanager
    def _temporary_cookie_file(self, cookie_text: str | None) -> Iterator[Path | None]:
        if cookie_text is None:
            yield None
            return

        self.settings.tmp_media_dir.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.settings.tmp_media_dir,
                prefix="yt-dlp-cookies-",
                suffix=".txt",
                delete=False,
            ) as temp_file:
                temp_file.write(cookie_text)
                temp_path = Path(temp_file.name)
            os.chmod(temp_path, 0o600)
            yield temp_path
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _summarize_error(self, value: str) -> str:
        cleaned = " ".join(value.split())
        return cleaned[:MAX_ERROR_LENGTH]
