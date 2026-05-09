from __future__ import annotations

import json
import logging
from urllib import request

from app.config import Settings


class AlarmNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.logger = logging.getLogger("podhub.notifications")

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.alarm_url
            and self.settings.alarm_custom_psk
            and self.settings.alarm_tg_user_id
        )

    def notify_sync_errors(self, *, channel_name: str, errors: list[str]) -> None:
        if not self.enabled or not errors:
            return

        title = f"Pod Hub 抓取异常: {channel_name}"
        message = self._build_sync_error_message(channel_name, errors)
        self._post(title=title, message=message)

    def _build_sync_error_message(self, channel_name: str, errors: list[str]) -> str:
        lines = [f"频道: {channel_name}", "错误:"]
        lines.extend(f"- {error}" for error in errors)
        return "\n".join(lines)

    def _post(self, *, title: str, message: str) -> None:
        if not self.enabled:
            return

        payload = json.dumps(
            {"title": title, "message": message},
            ensure_ascii=False,
        ).encode("utf-8")
        alarm_request = request.Request(
            self.settings.alarm_url,
            data=payload,
            method="POST",
            headers={
                "X-Custom-PSK": self.settings.alarm_custom_psk,
                "X-TGUser-ID": self.settings.alarm_tg_user_id,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "curl/8.7.1",
            },
        )

        try:
            with request.urlopen(alarm_request, timeout=self.settings.alarm_timeout_seconds):
                return
        except Exception:
            self.logger.exception("alarm notification failed")
