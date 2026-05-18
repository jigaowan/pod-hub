from __future__ import annotations

import json
import logging
from urllib import request

from app.config import Settings
from app.runtime_settings.service import RuntimeSettings, RuntimeSettingsService


class AlarmNotifier:
    def __init__(
        self,
        settings: Settings,
        *,
        runtime_settings_service: RuntimeSettingsService | None = None,
    ) -> None:
        self.settings = settings
        self.runtime_settings_service = runtime_settings_service or RuntimeSettingsService(settings)
        self.logger = logging.getLogger("podhub.notifications")

    @property
    def enabled(self) -> bool:
        return self.runtime_settings_service.get_effective_settings().alarm_configured

    def notify_sync_errors(self, *, channel_name: str, errors: list[str]) -> None:
        runtime_settings = self.runtime_settings_service.get_effective_settings()
        if not runtime_settings.alarm_configured or not errors:
            return

        title = f"Pod Hub 抓取异常: {channel_name}"
        message = self._build_sync_error_message(channel_name, errors)
        self._post(title=title, message=message, runtime_settings=runtime_settings)

    def _build_sync_error_message(self, channel_name: str, errors: list[str]) -> str:
        lines = [f"频道: {channel_name}", "错误:"]
        lines.extend(f"- {error}" for error in errors)
        return "\n".join(lines)

    def _post(self, *, title: str, message: str, runtime_settings: RuntimeSettings) -> None:
        if not runtime_settings.alarm_configured:
            return

        payload = json.dumps(
            {"title": title, "message": message},
            ensure_ascii=False,
        ).encode("utf-8")
        alarm_request = request.Request(
            runtime_settings.alarm_url,
            data=payload,
            method="POST",
            headers={
                "X-Custom-PSK": runtime_settings.alarm_custom_psk,
                "X-TGUser-ID": runtime_settings.alarm_tg_user_id,
                "Content-Type": "application/json; charset=utf-8",
                "User-Agent": "curl/8.7.1",
            },
        )

        try:
            with request.urlopen(alarm_request, timeout=runtime_settings.alarm_timeout_seconds):
                return
        except Exception:
            self.logger.exception("alarm notification failed")
