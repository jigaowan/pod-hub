from __future__ import annotations

import logging
import threading

from app.sync.service import SyncService


class SyncCoordinator:
    def __init__(self, sync_service: SyncService) -> None:
        self.sync_service = sync_service
        self._lock = threading.Lock()
        self.logger = logging.getLogger("podhub.sync.coordinator")

    def run_scheduled_sync(self) -> None:
        if not self._lock.acquire(blocking=False):
            reason = "Skipped because another sync run is already active"
            self.logger.info(reason)
            self.sync_service.record_global_skip(reason)
            return

        try:
            self.sync_service.sync_all_channels(notify=True)
        finally:
            self._lock.release()
