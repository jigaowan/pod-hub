from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from podhub.config import Notify

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transition:
    key: str
    state: str
    category: str | None
    reason: str | None


class Notifier:
    def __init__(self, settings: Notify):
        self.settings = settings

    async def send(self, events: list[Transition]) -> None:
        if not self.settings.webhook_url or not events:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            for event in events:
                try:
                    result = await client.post(
                        self.settings.webhook_url, headers=json.loads(self.settings.webhook_headers_json or "{}"),
                        json={"key": event.key, "state": event.state, "category": event.category,
                              "reason": event.reason, "text": f"Pod Hub: {event.key} {event.state}"},
                    )
                    result.raise_for_status()
                except (httpx.HTTPError, ValueError):
                    log.warning("Webhook delivery failed for breaker %s", event.key)
