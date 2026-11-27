from __future__ import annotations

import asyncio
import logging
import random
import shutil
import sqlite3
from collections.abc import Coroutine
from time import monotonic
from typing import Any

from podhub.core.attempts import interrupt_attempt
from podhub.core.discovery import NETWORK_GATES, due_source
from podhub.core.retention import reconcile, sweep
from podhub.core.services import Services
from podhub.db.connection import get_meta, set_meta
from podhub.db.repositories import claim_video, expire_deadlines, prune_attempts, recover, start_attempt, utc_day
from podhub.domain.enums import Category, Confidence, Scope
from podhub.domain.models import stamp, utc_now
from podhub.errors.taxonomy import Classification
from podhub.storage.media import STAGING_PREFIX
from podhub.storage.paths import validate_folder_name

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, services: Services):
        self.services = services
        self.tasks: dict[str, set[asyncio.Task[None]]] = {}
        self.active_downloads: dict[asyncio.Task[None], tuple[str, int]] = {}
        self.launch_after = 0.0
        self.backfill_after = 0.0
        self.pacing_rev = ""

    def recover(self) -> None:
        services = self.services
        paths = services.config.paths
        recover(services.db)
        with services.db.transaction() as conn:
            services.breakers.clear_reservations(conn)
        for item in (paths.state_dir / "tmp").iterdir():
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        for folder in paths.output_dir.iterdir():
            if folder.is_symlink() or not folder.is_dir():
                continue
            try:
                validate_folder_name(folder.name)
            except ValueError:
                continue
            for stage in (folder, folder / "artifacts"):
                if stage.is_dir() and not stage.is_symlink():
                    for item in stage.glob(f"{STAGING_PREFIX}*"):
                        if item.is_file() or item.is_symlink():
                            item.unlink()

    def spawn(self, group: str, coroutine: Coroutine[Any, Any, None], *, download_claim: tuple[str, int] | None = None) -> None:
        tasks = self.tasks.setdefault(group, set())
        task = asyncio.create_task(coroutine, name=f"podhub:{group}")
        tasks.add(task)
        if download_claim is not None:
            self.active_downloads[task] = download_claim

        def done(finished: asyncio.Task[None]) -> None:
            tasks.discard(finished)
            claim = self.active_downloads.pop(finished, None)
            if not finished.cancelled() and (error := finished.exception()):
                log.error("%s task failed", group, exc_info=error)
                if claim is not None:
                    vid, attempt = claim
                    try:
                        self.services.downloader.return_claim(vid, attempt, f"Download task failed: {error}")
                    except Exception:
                        log.exception("Failed to release orphaned claim for %s", vid)

        task.add_done_callback(done)

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("Scheduler tick failed")
                await asyncio.sleep(5)
        finally:
            tasks = [task for group in self.tasks.values() for task in group]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self) -> None:
        services = self.services
        config = services.config
        now = utc_now()
        with services.db.transaction() as conn:
            active_vids = {vid for vid, _ in self.active_downloads.values()}
            for row in conn.execute("SELECT video_id FROM videos WHERE state='running'").fetchall():
                if row["video_id"] not in active_vids:
                    conn.execute("UPDATE videos SET state='pending',running_since=NULL WHERE video_id=? AND state='running'", (row["video_id"],))
                    for att in conn.execute("SELECT id FROM attempts WHERE kind='download' AND video_id=? AND outcome='running'", (row["video_id"],)).fetchall():
                        interrupt_attempt(conn, services.breakers, att["id"], "Recovered orphaned download claim")
            expire_deadlines(conn, stamp(now))
            services.breakers.refresh(conn)
            paused = get_meta(conn, "global_paused") == "1"
            local_blocked = not services.breakers.allowed(conn, ("local",))
            maintenance = (
                get_meta(conn, "last_retention_at") <= stamp(now, seconds=-900),
                get_meta(conn, "last_reconcile_at") <= stamp(now, seconds=-86400),
                get_meta(conn, "last_backup_at") <= stamp(now, seconds=-86400),
            )
            doctor_due = get_meta(conn, "last_doctor_tick") <= stamp(now, seconds=-3600)
        if doctor_due and not self.tasks.get("doctor"):
            self.spawn("doctor", self.doctor())
        if any(maintenance) and not self.tasks.get("maintenance"):
            self.spawn("maintenance", self.maintain(*maintenance))
        if services.registry and not self.tasks.get("postprocess"):
            self.spawn("postprocess", services.worker.run_next())
        if local_blocked:
            self.cancel_downloads()
        if paused or local_blocked:
            return
        free = services.disk_space()
        if min(free.values()) < config.disk.min_free_gb * 1024**3:
            failure = Classification(Category.LOCAL_RESOURCE, Scope.LOCAL, Confidence.HIGH, True,
                                     "Disk space below configured minimum")
            with services.db.transaction() as conn:
                event = services.breakers.trip(conn, "local", failure)
            self.cancel_downloads()
            if event:
                await services.notifier.send([event])
            return
        services.downloader.link_ready()
        pacing = services.effective_pacing()
        with services.db.transaction() as conn:
            if get_meta(conn, "global_paused") == "1" or not services.breakers.allowed(conn, NETWORK_GATES):
                return
            pacing_rev = get_meta(conn, "pacing_rev")
            if pacing_rev != self.pacing_rev:
                self.pacing_rev = pacing_rev
                self.launch_after = 0.0
                self.backfill_after = 0.0
            # Give the half-open attempt to the download queue first.
            if (len(self.tasks.get("download", ())) < pacing.max_concurrent_downloads
                    and monotonic() >= self.launch_after and self.below_cap(conn, pacing.daily_download_cap)):
                allow_history = (
                    monotonic() >= self.backfill_after
                    and self.below_backfill_cap(conn, pacing.backfill_daily_cap)
                )
                claimed = claim_video(conn, stamp(now), min_priority=0 if allow_history else 20)
                if claimed:
                    video, sid = claimed
                    is_backfill = video["priority"] <= 10
                    cookie = conn.execute("SELECT cookie_profile_id FROM sources WHERE id=?", (sid,)).fetchone()[0]
                    attempt = start_attempt(conn, "download", sid, video["video_id"], cookie, is_backfill=is_backfill)
                    services.breakers.reserve(conn, NETWORK_GATES, attempt)
                    now_mono = monotonic()
                    self.launch_after = now_mono + pacing.min_launch_gap_s + random.uniform(0, pacing.launch_jitter_s)
                    if is_backfill:
                        self.backfill_after = now_mono + pacing.backfill_min_gap_s + random.uniform(0, pacing.backfill_jitter_s)
                else:
                    attempt = None
            else:
                attempt = None
        if attempt:
            self.spawn("download", services.downloader.execute(video, sid, attempt), download_claim=(video["video_id"], attempt))
        if self.tasks.get("discovery"):
            return
        with services.db.transaction() as conn:
            if get_meta(conn, "global_paused") == "1":
                return
            for source in due_source(conn, now):
                cookie = source["cookie_profile_id"]
                gates = (*NETWORK_GATES, f"cookie:{cookie}") if cookie is not None else NETWORK_GATES
                if services.breakers.allowed(conn, gates):
                    discovery_attempt = start_attempt(conn, "discovery", source["id"], cookie_id=cookie)
                    services.breakers.reserve(conn, gates, discovery_attempt)
                    break
            else:
                return
        self.spawn("discovery", self.discover(source, discovery_attempt))

    def cancel_downloads(self) -> None:
        for task in self.tasks.get("download", ()):
            if not task.cancelling():
                task.cancel()

    def below_cap(self, conn: sqlite3.Connection, limit: int) -> bool:
        if not limit:
            return True
        count = conn.execute(
            "SELECT count(*) FROM attempts WHERE kind='download' AND outcome IN ('ok','running','error','deferred') AND started_at>=?",
            (utc_day(),),
        ).fetchone()[0]
        return count < limit

    def below_backfill_cap(self, conn: sqlite3.Connection, limit: int) -> bool:
        if not limit:
            return True
        count = conn.execute(
            """SELECT count(*) FROM attempts
               WHERE kind='download' AND is_backfill=1
                 AND outcome IN ('ok','running','error','deferred') AND started_at>=?""",
            (utc_day(),),
        ).fetchone()[0]
        return count < limit

    async def discover(self, source: sqlite3.Row, attempt: int) -> None:
        try:
            await self.services.discovery.sync(source, attempt)
        finally:
            with self.services.db.transaction() as conn:
                interrupt_attempt(conn, self.services.breakers, attempt, "Discovery interrupted")

    async def maintain(self, retention: bool, reconciliation: bool, backup: bool) -> None:
        services = self.services
        if retention:
            await asyncio.to_thread(sweep, services.config, services.db)
        if reconciliation:
            await asyncio.to_thread(reconcile, services.config, services.db)
        if backup:
            await asyncio.to_thread(self.backup)

    def backup(self) -> None:
        services = self.services
        services.db.backup(services.config.paths.state_dir / "backups")
        with services.db.transaction() as conn:
            prune_attempts(conn, utc_now())
            set_meta(conn, "last_backup_at", stamp())

    async def doctor(self) -> None:
        with self.services.db.transaction() as conn:
            set_meta(conn, "last_doctor_tick", stamp())
        await self.services.doctor()
