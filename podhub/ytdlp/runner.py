from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from podhub.config import Config


@dataclass
class RunResult:
    hello: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    gate: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    phase: str = "extract"
    exit_code: int | None = None
    bytes_downloaded: int = 0
    avg_bps: int | None = None

    @property
    def version(self) -> str | None:
        return self.hello.get("ytdlp_version")


def redact(message: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "<redacted>")
    message = re.sub(r"(https?://)[^/\s@]+@", r"\1<redacted>@", message)
    return re.sub(r"(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?<redacted>", message)


async def stop_group(process: asyncio.subprocess.Process, *, grace: float = 5) -> None:
    def send(sig: int) -> bool:
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Some sandboxes deny a group liveness check while its members exit.
            if sig == 0:
                return process.returncode is None
            if process.returncode is not None:
                return False
            try:
                process.send_signal(sig)
            except ProcessLookupError:
                return False
        return True

    cancelled = False
    if send(signal.SIGTERM):
        deadline = monotonic() + grace
        while monotonic() < deadline and send(0):
            try:
                await asyncio.sleep(min(0.05, max(0, deadline - monotonic())))
            except asyncio.CancelledError:
                cancelled = True
                break
        send(signal.SIGKILL)
    while True:
        try:
            await process.wait()
            break
        except asyncio.CancelledError:
            cancelled = True
            send(signal.SIGKILL)
    if cancelled:
        raise asyncio.CancelledError


class Runner:
    def __init__(self, config: Config, shim: Path | None = None) -> None:
        self.config = config
        self.shim = shim or Path(__file__).with_name("shim.py")

    async def run(
        self, mode: str, *, on_entry: Callable[[dict[str, Any]], bool] | None = None, **payload: Any,
    ) -> RunResult:
        tools = self.config.toolchain
        request = {
            "mode": mode, "ffmpeg_path": tools.ffmpeg_path, "js_runtime": tools.js_runtime,
            "proxy": tools.ytdlp_proxy, "extractor_args": tools.ytdlp_extractor_args,
            "min_ytdlp_version": tools.min_ytdlp_version, **payload,
        }
        timeout = self.config.watchdog
        result = RunResult()
        try:
            process = await asyncio.create_subprocess_exec(
                tools.ytdlp_python, str(self.shim), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=True, limit=2**21,
            )
        except OSError as exc:
            result.error = {"exc_class": type(exc).__name__, "msg": str(exc)}
            return result

        assert process.stdin and process.stdout
        started = phase_started = last_progress = monotonic()
        total = timeout.total_timeout_base_s
        stopped_early = False
        try:
            process.stdin.write(json.dumps(request, default=str).encode())
            try:
                await asyncio.wait_for(process.stdin.drain(), timeout.probe_timeout_s)
            except (BrokenPipeError, ConnectionResetError):
                pass
            process.stdin.close()
            while True:
                deadline = started + total
                if result.phase == "download":
                    deadline = min(deadline, last_progress + timeout.stall_timeout_s)
                elif not result.probe and mode != "list":
                    deadline = min(deadline, started + timeout.probe_timeout_s)
                elif mode == "list":
                    deadline = min(deadline, last_progress + timeout.probe_timeout_s)
                line = await asyncio.wait_for(process.stdout.readline(), max(0.001, deadline - monotonic()))
                if not line:
                    break
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event must be a JSON object")
                event_type = event.get("type")
                now = monotonic()
                if event_type == "hello":
                    result.hello = event
                elif event_type == "phase":
                    result.phase = event["phase"]
                    phase_started = last_progress = now
                elif event_type == "entry":
                    last_progress = now
                    if on_entry and not on_entry(event):
                        stopped_early = True
                        await stop_group(process)
                        break
                elif event_type == "probe":
                    result.probe = event["info"]
                    duration = result.probe.get("duration") or 0
                    total = max(total, int(duration * timeout.total_timeout_duration_factor) + 300)
                elif event_type == "progress":
                    count = event.get("downloaded_bytes") or 0
                    if count > result.bytes_downloaded:
                        last_progress = now
                        result.bytes_downloaded = count
                        result.avg_bps = int(count / max(0.001, now - phase_started))
                elif event_type == "warning":
                    if len(result.warnings) < 50:
                        result.warnings.append(redact(str(event.get("msg", "")), (tools.ytdlp_proxy,))[:1500])
                elif event_type in ("gate", "result", "error"):
                    setattr(result, event_type, event)
            result.exit_code = await asyncio.wait_for(process.wait(), max(0.001, started + total - monotonic()))
        except (ValueError, KeyError, TypeError, TimeoutError) as exc:
            result.error = {
                "exc_class": "WatchdogTimeout" if isinstance(exc, TimeoutError) else "ProtocolError",
                "msg": "shim watchdog timed out" if isinstance(exc, TimeoutError) else f"invalid shim event: {exc}",
                "phase": result.phase,
            }
            await stop_group(process)
            result.exit_code = process.returncode
        except BaseException:
            await stop_group(process)
            raise
        if stopped_early:
            result.exit_code = 0
        elif result.exit_code and not result.error:
            result.error = {"exc_class": "ChildProcessError", "msg": f"shim exited with code {result.exit_code}"}
        if result.error:
            result.error["msg"] = redact(str(result.error.get("msg", "")), (tools.ytdlp_proxy,))[:2000]
        return result
