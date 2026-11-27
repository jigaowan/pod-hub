from __future__ import annotations

import asyncio
import errno
import json
import logging
import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

log = logging.getLogger(__name__)

from podhub.config import Config
from podhub.core.attempts import finish_attempt, interrupt_attempt
from podhub.core.discovery import cookie_for
from podhub.core.notifications import Notifier, Transition
from podhub.db.connection import Database
from podhub.db.repositories import activate_video, policy_for, public_media, upsert_video
from podhub.domain.enums import Category
from podhub.domain.models import stamp, utc_now
from podhub.errors.breaker import Breakers
from podhub.errors.retry import plan_retry
from podhub.errors.taxonomy import Classification, classify
from podhub.postprocess.worker import PostprocessWorker
from podhub.storage.cookies import copy_cookie
from podhub.storage.media import atomic_publish
from podhub.storage.paths import audio_path, canonical_video, safe_path, source_dir
from podhub.ytdlp.ffprobe import CorruptedArtifact, MediaInfo, validate_media
from podhub.ytdlp.runner import RunResult, Runner, redact


class Downloader:
    def __init__(
        self, config: Config, db: Database, runner: Runner, breakers: Breakers,
        notifier: Notifier, worker: PostprocessWorker,
    ):
        self.config = config
        self.db = db
        self.runner = runner
        self.breakers = breakers
        self.notifier = notifier
        self.worker = worker

    def sources_for(self, conn: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            """SELECT s.* FROM sources s JOIN source_entries e ON e.source_id=s.id
               WHERE e.video_id=? AND e.state='wanted' AND s.enabled=1 ORDER BY s.id""",
            (video_id,),
        ).fetchall()

    def publish(
        self, conn: sqlite3.Connection, source: sqlite3.Row, video_id: str,
        origin: Path, *, hardlink: bool,
    ) -> Path:
        root = self.config.paths.output_dir
        if hardlink:
            dest = safe_path(source_dir(root, source["slug"]), origin.name)
        else:
            video = conn.execute(
                "SELECT title,published_at,upload_date,meta_json FROM videos WHERE video_id=?", (video_id,),
            ).fetchone()
            title = video["title"]
            if title == video_id:
                title = json.loads(video["meta_json"] or "{}").get("title")
            dest = audio_path(root, source["slug"], video_id, title, video["published_at"], video["upload_date"])
        atomic_publish(origin, dest, hardlink=hardlink)
        conn.execute(
            """UPDATE source_entries SET state='published',entry_media_path=?,published_at=?,filter_reason=NULL
               WHERE source_id=? AND video_id=? AND state='wanted'""",
            (str(dest.relative_to(root)), stamp(), source["id"], video_id),
        )
        self.worker.enqueue_for_video(conn, video_id, source)
        return dest

    def apply_probe_filters(
        self, conn: sqlite3.Connection, video_id: str, info: dict[str, Any], sources: list[sqlite3.Row],
    ) -> list[sqlite3.Row]:
        eligible = []
        for source in sources:
            reason = policy_for(source).rejection(info)
            if reason:
                conn.execute(
                    """UPDATE source_entries SET state='filtered',filter_reason=?,evaluated_rev=?
                       WHERE source_id=? AND video_id=? AND state='wanted'""",
                    (reason, source["policy_rev"], source["id"], video_id),
                )
            else:
                eligible.append(source)
        return eligible

    def link_ready(self, limit: int = 20) -> None:
        root = self.config.paths.output_dir
        with self.db.connect() as conn:
            video_ids = [
                row[0] for row in conn.execute(
                    """SELECT DISTINCT v.video_id FROM videos v JOIN source_entries e ON e.video_id=v.video_id
                       JOIN sources s ON s.id=e.source_id WHERE v.state='ready' AND e.state='wanted'
                       AND s.enabled=1 LIMIT ?""", (limit,),
                ).fetchall()
            ]
        for vid in video_ids:
            try:
                with self.db.transaction() as conn:
                    video = conn.execute("SELECT * FROM videos WHERE video_id=? AND state='ready'", (vid,)).fetchone()
                    if video is None:
                        continue
                    eligible = self.apply_probe_filters(conn, vid, json.loads(video["meta_json"] or "{}"), self.sources_for(conn, vid))
                    if not eligible:
                        continue
                    files = (
                        safe_path(root, row["entry_media_path"]) for row in public_media(conn, vid)
                    )
                    origin = next((path for path in files if path.is_file()), None)
                    if not origin:
                        conn.execute(
                            """UPDATE source_entries SET state='wanted',entry_media_path=NULL
                               WHERE video_id=? AND state='published'""", (vid,),
                        )
                        conn.execute("UPDATE videos SET state='purged',give_up_at=NULL WHERE video_id=?", (vid,))
                        activate_video(conn, self.config, vid, 50)
                        continue
                    for source in eligible:
                        try:
                            self.publish(conn, source, vid, origin, hardlink=True)
                        except (OSError, ValueError) as exc:
                            log.warning("Failed to publish shared audio for source %s video %s: %s", source["id"], vid, exc)
                            conn.execute(
                                """UPDATE sources SET last_error_category='local_resource',last_error_message=?,
                                   updated_at=? WHERE id=?""",
                                (redact(str(exc))[:500], stamp(), source["id"]),
                            )
            except (OSError, sqlite3.Error, ValueError) as exc:
                log.warning("link_ready skipped video %s due to error: %s", vid, exc)

    async def execute(self, video: sqlite3.Row, source_id: int, attempt: int) -> None:
        vid = video["video_id"]
        run = RunResult()
        cookie_id = None
        cookie_updated_at: str | None = None
        failure: Classification | None = None
        work_root = self.config.paths.state_dir / "tmp"
        parts = safe_path(work_root, f"retry-{vid}")
        try:
            with TemporaryDirectory(prefix=f"{attempt}-", dir=work_root) as work:
                workspace = Path(work)
                try:
                    with self.db.connect() as conn:
                        source = conn.execute("SELECT * FROM sources WHERE id=? AND enabled=1", (source_id,)).fetchone()
                        sources = self.sources_for(conn, vid)
                        if source is None or not any(item["id"] == source_id for item in sources):
                            self.return_claim(vid, attempt, "Source changed before execution")
                            return
                        cookie_id = source["cookie_profile_id"]
                        cookie = cookie_for(conn, cookie_id)
                        cookie_updated_at = cookie["updated_at"] if cookie else None
                    for old in parts.glob("input.*"):
                        old.replace(workspace / old.name)
                    if parts.is_dir():
                        parts.rmdir()
                    extras = {"cookiefile": copy_cookie(self.config.paths.state_dir / "cookies", cookie["file_path"], workspace)} if cookie else {}
                    run = await self.runner.run(
                        "fetch", url=canonical_video(vid), workspace=workspace, policy=policy_for(source).model_dump(),
                        entry_policies={str(row["id"]): policy_for(row).model_dump() for row in sources},
                        **extras,
                    )
                    if run.error or run.warnings or run.gate.get("action") == "defer":
                        failure = classify(run, used_cookie=cookie is not None, minimum=self.config.toolchain.min_ytdlp_version)
                    blocking = failure and (
                        (run.error and failure.category != Category.DEGRADABLE_POSTPROCESS)
                        or failure.category in (Category.COOKIE_INVALID, Category.NOT_READY)
                    )
                    if blocking:
                        if failure.category == Category.TRANSIENT_NETWORK:
                            partials = [path for path in workspace.glob("input.*") if ".part" in path.name or path.suffix == ".ytdl"]
                            if partials:
                                parts.mkdir(mode=0o700, exist_ok=True)
                                for partial in partials:
                                    partial.replace(parts / partial.name)
                        await self.record_failure(video, source_id, cookie_id, cookie_updated_at, attempt, run, failure)
                        return
                    if run.probe.get("id") != vid:
                        raise CorruptedArtifact("yt-dlp returned a different video ID")
                    if run.gate.get("action") in ("filter", "skip"):
                        events = self.finish_filtered(video, attempt, run, cookie_id, cookie_updated_at, sources)
                    else:
                        audio = Path(run.result.get("filepath") or workspace / "audio.m4a")
                        if audio.resolve().parent != workspace or audio.is_symlink():
                            raise CorruptedArtifact("shim returned an audio path outside its workspace")
                        media = await validate_media(self.config.toolchain.ffprobe_path, audio, run.probe.get("duration"))
                        if failure and failure.category == Category.DEGRADABLE_POSTPROCESS and failure.reason not in run.warnings:
                            run.warnings.append(failure.reason)
                        events = self.finish_ready(video, attempt, run, audio, media, cookie_id, cookie_updated_at, failure)
                    await self.notifier.send(events)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    category = "LocalResource" if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EACCES, errno.EROFS, errno.EDQUOT) else type(exc).__name__
                    run.error = {"exc_class": category, "msg": redact(str(exc), (self.config.toolchain.ytdlp_proxy,))}
                    failure = classify(run, used_cookie=cookie_id is not None, minimum=self.config.toolchain.min_ytdlp_version)
                    await self.record_failure(video, source_id, cookie_id, cookie_updated_at, attempt, run, failure)
        except asyncio.CancelledError:
            self.return_claim(vid, attempt, "Interrupted by shutdown")
            raise
        except Exception as exc:
            run.error = {"exc_class": type(exc).__name__, "msg": redact(str(exc))}
            failure = classify(run, used_cookie=cookie_id is not None)
            await self.record_failure(video, source_id, cookie_id, cookie_updated_at, attempt, run, failure)

    def return_claim(self, vid: str, attempt: int, reason: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("UPDATE videos SET state='pending',running_since=NULL WHERE video_id=? AND state='running'", (vid,))
            interrupt_attempt(conn, self.breakers, attempt, reason)

    def finish_filtered(
        self, video: sqlite3.Row, attempt: int, run: RunResult, cookie_id: int | None,
        cookie_updated_at: str | None, sources: list[sqlite3.Row],
    ) -> list[Transition]:
        vid = video["video_id"]
        with self.db.transaction() as conn:
            if run.probe.get("id") == vid:
                upsert_video(conn, run.probe)
                current = self.sources_for(conn, vid)
                self.apply_probe_filters(conn, vid, run.probe, current)
                # A source or its policy could have changed while extraction was in flight.
                for source in sources:
                    if run.gate["action"] == "skip":
                        conn.execute(
                            """UPDATE source_entries SET state='filtered',filter_reason=?
                               WHERE source_id=? AND video_id=? AND state='wanted' AND EXISTS (
                                 SELECT 1 FROM sources WHERE id=? AND policy_rev=?)""",
                            (run.gate.get("reason"), source["id"], vid, source["id"], source["policy_rev"]),
                        )
            conn.execute(
                """UPDATE videos SET state='pending',running_since=NULL,
                   give_up_at=CASE WHEN EXISTS (SELECT 1 FROM source_entries WHERE video_id=? AND state='wanted')
                   THEN give_up_at ELSE NULL END WHERE video_id=? AND state='running'""", (vid, vid),
            )
            _, events = finish_attempt(conn, self.breakers, attempt, run, "skipped", cookie_id=cookie_id, cookie_updated_at=cookie_updated_at)
            return events

    def finish_ready(
        self, video: sqlite3.Row, attempt: int, run: RunResult, audio: Path, media: MediaInfo,
        cookie_id: int | None, cookie_updated_at: str | None, failure: Classification | None,
    ) -> list[Transition]:
        vid = video["video_id"]
        with self.db.transaction() as conn:
            upsert_video(conn, run.probe)
            eligible = self.apply_probe_filters(conn, vid, run.probe, self.sources_for(conn, vid))
            if eligible:
                first, *rest = eligible
                origin = self.publish(conn, first, vid, audio, hardlink=False)
                for source in rest:
                    self.publish(conn, source, vid, origin, hardlink=True)
                conn.execute(
                    """UPDATE videos SET state='ready',media_bytes=?,media_duration_s=?,media_mime=?,
                       running_since=NULL,ready_at=?,updated_at=?,next_attempt_at=NULL,give_up_at=NULL,
                       last_category=NULL,last_scope=NULL,last_error=NULL,unclassified=0,
                       category_attempts_json='{}' WHERE video_id=?""",
                    (media.size, media.duration, media.mime, stamp(), stamp(), vid),
                )
            else:
                conn.execute("UPDATE videos SET state='pending',running_since=NULL WHERE video_id=?", (vid,))
            degradable = failure if failure and failure.category == Category.DEGRADABLE_POSTPROCESS else None
            _, events = finish_attempt(conn, self.breakers, attempt, run, "ok" if eligible else "skipped",
                                       degradable, cookie_id, cookie_updated_at=cookie_updated_at)
            return events

    async def record_failure(
        self, video: sqlite3.Row, source_id: int, cookie_id: int | None, cookie_updated_at: str | None,
        attempt: int, run: RunResult, failure: Classification,
    ) -> None:
        vid = video["video_id"]
        with self.db.transaction() as conn:
            if run.probe.get("id") == vid:
                upsert_video(conn, run.probe)
            outcome = "deferred" if failure.category == Category.NOT_READY else "error"
            classified, events = finish_attempt(conn, self.breakers, attempt, run, outcome, failure, cookie_id,
                                                cookie_updated_at=cookie_updated_at)
            assert classified is not None
            alternate = classified.category == Category.AUTH_REQUIRED and any(
                row["id"] != source_id and row["cookie_profile_id"]
                and self.breakers.allowed(conn, (f"cookie:{row['cookie_profile_id']}",))
                and (cookie := cookie_for(conn, row["cookie_profile_id"]))
                and cookie["status"] in ("active", "suspect")
                for row in self.sources_for(conn, vid)
            )
            now = utc_now()
            first_failure = video["attempt_count"] == 0 and video["last_category"] is None
            fresh_deadline = stamp(now, seconds=self.config.retry.give_up_after_days * 86400) if first_failure else video["give_up_at"]
            video_for_retry = {**dict(video), "give_up_at": fresh_deadline}
            decision = plan_retry(
                self.config.retry, video_for_retry, classified, alternate_source=alternate,
                release=run.gate.get("release_timestamp") or run.probe.get("release_timestamp"),
                live_status=run.probe.get("live_status") or run.gate.get("reason"),
            )
            conn.execute(
                """UPDATE videos SET state=?,next_attempt_at=?,give_up_at=?,category_attempts_json=?,unclassified=?,
                   attempt_count=attempt_count+?,priority=CASE WHEN ? THEN min(priority,30) ELSE priority END,
                   running_since=NULL,last_category=?,last_scope=?,last_error=?,updated_at=?
                   WHERE video_id=? AND state='running'""",
                (decision.state, decision.next_attempt_at, fresh_deadline, json.dumps(decision.category_attempts),
                 int(decision.unclassified), int(decision.charge_attempt), int(decision.charge_attempt),
                 classified.category, classified.scope, classified.reason, stamp(now), vid),
            )
        if decision.state != "pending":
            shutil.rmtree(safe_path(self.config.paths.state_dir / "tmp", f"retry-{vid}"), ignore_errors=True)
        if events:
            await self.notifier.send(events)
