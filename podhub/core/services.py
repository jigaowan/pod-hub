from __future__ import annotations

import asyncio
import errno
import json
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator
from uuid import uuid4

import httpx

from podhub.config import Config
from podhub.core.attempts import finish_attempt, interrupt_attempt
from podhub.core.discovery import Discovery, NETWORK_GATES, Preview, SourceError, cookie_for
from podhub.core.downloader import Downloader
from podhub.core.notifications import Notifier
from podhub.core.retention import purge_unlinked
from podhub.db.connection import Database, get_meta, set_meta
from podhub.db.repositories import activate_video, policy_for, start_attempt, upsert_video
from podhub.domain.enums import Category, SourceKind
from podhub.domain.models import SourceIdentity, stamp
from podhub.domain.policy import SourcePolicy
from podhub.errors.breaker import Breakers
from podhub.errors.taxonomy import classify
from podhub.postprocess.registry import PostProcessorRegistry
from podhub.postprocess.worker import PostprocessWorker
from podhub.storage.cookies import copy_cookie, expiry, has_youtube_auth_cookie, save_cookie
from podhub.storage.media import save_cover
from podhub.storage.paths import (
    FOLDER_NAME_MAX_BYTES,
    canonical_video,
    default_source_slug,
    folder_name_key,
    parse_video,
    safe_path,
    source_dir,
    validate_folder_name,
    youtube_cover,
)
from podhub.ytdlp.runner import RunResult, Runner, redact, stop_group
from podhub.ytdlp.shim import version_parts

DEFAULT_COOKIE_TEST_VIDEO = "dQw4w9WgXcQ"


class NotFound(LookupError):
    pass


def _podcast_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ValueError("podcast title cannot be empty")
    return title


class Services:
    def __init__(self, config: Config, *, runner: Runner | None = None, registry: PostProcessorRegistry | None = None):
        self.config = config
        self.db = Database(config.paths.state_dir)
        self.runner = runner if runner is not None else Runner(config)
        self.breakers = Breakers(config.breaker)
        self.notifier = Notifier(config.notify)
        self.registry = registry if registry is not None else PostProcessorRegistry()
        self.worker = PostprocessWorker(config.paths, self.db, self.registry)
        self.discovery = Discovery(config, self.db, self.runner, self.breakers, self.notifier)
        self.downloader = Downloader(config, self.db, self.runner, self.breakers, self.notifier, self.worker)

    def initialize(self) -> None:
        paths = self.config.paths
        paths.state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
        paths.output_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        for name in ("tmp", "cookies", "backups"):
            path = safe_path(paths.state_dir, name)
            path.mkdir(mode=0o700, exist_ok=True)
            path.chmod(0o700)
        self.db.migrate()
        with self.db.transaction() as conn:
            if not get_meta(conn, "preview_key"):
                set_meta(conn, "preview_key", secrets.token_hex(32))

    def source(self, source_id: int) -> sqlite3.Row:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise NotFound("source does not exist")
        return row

    def sources(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute(
                """SELECT s.*,
                     sum(e.state='published') AS published_count,
                     sum(e.state='wanted') AS wanted_count,
                     sum(e.state='wanted' AND v.state='failed') AS failed_count
                   FROM sources s LEFT JOIN source_entries e ON e.source_id=s.id
                   LEFT JOIN videos v ON v.video_id=e.video_id GROUP BY s.id
                   ORDER BY s.title COLLATE NOCASE,s.id""",
            ).fetchall()

    def source_episodes(self, source_id: int, *, page: int = 1) -> tuple[list[sqlite3.Row], bool]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT e.*,v.title,v.state AS video_state,v.published_at AS episode_date
                   FROM source_entries e JOIN videos v USING(video_id) WHERE e.source_id=?
                   ORDER BY coalesce(v.published_at,e.discovered_at) DESC, e.video_id LIMIT 101 OFFSET ?""",
                (source_id, (page - 1) * 100),
            ).fetchall()
        return rows[:100], len(rows) > 100

    async def preview(self, value: str, kind: SourceKind | None, cookie_id: int | None = None) -> Preview:
        if kind == SourceKind.MANUAL:
            if not value.strip():
                raise ValueError("enter a name for the manual collection")
            return Preview(SourceIdentity(kind, f"manual:{uuid4()}", value.strip(), ""), [])
        return await self.discovery.preview(value, kind, cookie_id)

    def _available_slug(self, conn: sqlite3.Connection, base: str, *, explicit: bool = False) -> str:
        base = validate_folder_name(base)
        occupied = {folder_name_key(row["slug"]) for row in conn.execute("SELECT slug FROM sources")}
        occupied.update(folder_name_key(path.name) for path in self.config.paths.output_dir.iterdir())
        slug = base
        index = 2
        while folder_name_key(slug) in occupied:
            if explicit:
                raise ValueError("folder name is already in use")
            suffix = f"-{index}"
            budget = FOLDER_NAME_MAX_BYTES - len(suffix)
            prefix = base.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(" .")
            slug = f"{prefix}{suffix}"
            index += 1
        return slug

    def suggest_slug(self, identity: SourceIdentity, original: str) -> str:
        with self.db.connect() as conn:
            return self._available_slug(conn, default_source_slug(identity.title, original, identity.external_id))

    async def add_source(
        self, identity: SourceIdentity, original: str, policy: SourcePolicy,
        cookie_id: int | None = None, check_interval_s: int | None = None,
        *, title: str | None = None, slug: str | None = None,
    ) -> int:
        display_title = _podcast_title(identity.title if title is None else title)
        interval = check_interval_s or (
            self.config.pacing.playlist_check_interval_s if identity.kind == "playlist"
            else self.config.pacing.default_check_interval_s
        )
        if interval < 1:
            raise ValueError("check interval must be positive")
        with self.db.transaction() as conn:
            if cookie_id is not None and not cookie_for(conn, cookie_id):
                raise ValueError("cookie profile does not exist")
            if conn.execute("SELECT 1 FROM sources WHERE external_id=?", (identity.external_id,)).fetchone():
                raise ValueError("source already exists")
            base = default_source_slug(display_title, original, identity.external_id) if slug is None else slug
            slug = self._available_slug(conn, base, explicit=slug is not None)
            now = stamp()
            cursor = conn.execute(
                """INSERT INTO sources(kind,source_input,external_id,url,title,slug,feed_token,policy_json,
                   cookie_profile_id,check_interval_s,next_check_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (identity.kind, original, identity.external_id, identity.normalized_url, display_title, slug,
                 secrets.token_urlsafe(16), policy.model_dump_json(), cookie_id, interval, now, now, now),
            )
            source_id = cursor.lastrowid
            assert source_id is not None
        if identity.cover_url:
            await self.fetch_cover(source_id, slug, identity.cover_url)
        return source_id

    def rename_source(self, source_id: int, title: str) -> None:
        title = _podcast_title(title)
        with self.db.transaction() as conn:
            updated = conn.execute(
                "UPDATE sources SET title=?,updated_at=? WHERE id=?", (title, stamp(), source_id),
            )
            if not updated.rowcount:
                raise NotFound("source does not exist")

    async def fetch_cover(self, source_id: int, slug: str, url: str) -> None:
        if not youtube_cover(url):
            return
        try:
            with TemporaryDirectory(dir=self.config.paths.state_dir / "tmp") as work:
                image, jpg = Path(work) / "cover", Path(work) / "cover.jpg"
                async with httpx.AsyncClient(timeout=15, proxy=self.config.toolchain.ytdlp_proxy or None) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        if not response.headers.get("content-type", "").startswith("image/"):
                            return
                        with image.open("wb") as output:
                            async for block in response.aiter_bytes():
                                output.write(block)
                                if output.tell() > 5_000_000:
                                    return
                process = await asyncio.create_subprocess_exec(
                    self.config.toolchain.ffmpeg_path, "-nostdin", "-v", "error", "-i", str(image),
                    "-frames:v", "1", "--", str(jpg), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
                )
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except BaseException:
                    await stop_group(process)
                    raise
                if process.returncode == 0 and jpg.is_file():
                    with self.db.transaction() as conn:
                        if conn.execute("SELECT 1 FROM sources WHERE id=? AND slug=?", (source_id, slug)).fetchone():
                            filename = save_cover(jpg, self.config.paths.output_dir, slug)
                            conn.execute("UPDATE sources SET cover_filename=?,updated_at=? WHERE id=?", (filename, stamp(), source_id))
        except (httpx.HTTPError, ImportError, OSError, ValueError, TimeoutError):
            return

    def update_source(
        self, source_id: int, policy: SourcePolicy, cookie_id: int | None, check_interval: int,
    ) -> None:
        if check_interval < 1:
            raise ValueError("check interval must be positive")
        with self.db.transaction() as conn:
            cookie = cookie_for(conn, cookie_id)
            if cookie_id is not None and cookie is None:
                raise ValueError("cookie profile does not exist")
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise NotFound("source does not exist")
            revised = policy != policy_for(source)
            rev = source["policy_rev"] + int(revised)
            conn.execute(
                """UPDATE sources SET policy_json=?,policy_rev=?,cookie_profile_id=?,
                   check_interval_s=?,updated_at=? WHERE id=?""",
                (policy.model_dump_json(), rev, cookie_id, check_interval, stamp(), source_id),
            )
            if cookie and cookie["id"] != source["cookie_profile_id"] and cookie["status"] == "active" and (
                self.breakers.allowed(conn, (f"cookie:{cookie_id}",))
            ):
                self._reactivate_auth_videos(conn, cookie["id"], source_id=source_id)
            if not revised:
                return
            for row in conn.execute(
                """SELECT v.meta_json,e.video_id FROM source_entries e JOIN videos v USING(video_id)
                   WHERE source_id=? AND e.state IN ('filtered','candidate')""", (source_id,),
            ).fetchall():
                reason = policy.rejection(json.loads(row["meta_json"] or "{}"))
                conn.execute(
                    """UPDATE source_entries SET state=?,filter_reason=?,evaluated_rev=? WHERE source_id=? AND video_id=?""",
                    ("filtered" if reason else "candidate", reason, rev, source_id, row["video_id"]),
                )

    def source_command(self, source_id: int, action: str, *, count: int = 0) -> None:
        with self.db.transaction() as conn:
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if not source:
                raise NotFound("source does not exist")
            if action == "sync":
                conn.execute("UPDATE sources SET next_check_at=?,updated_at=? WHERE id=?", (stamp(), stamp(), source_id))
            elif action in ("pause", "resume"):
                conn.execute("UPDATE sources SET enabled=?,updated_at=? WHERE id=?", (int(action == "resume"), stamp(), source_id))
            elif action == "rotate":
                conn.execute("UPDATE sources SET feed_token=?,updated_at=? WHERE id=?", (secrets.token_urlsafe(16), stamp(), source_id))
            elif action == "backfill":
                if count < 1:
                    raise ValueError("backfill count must be positive")
                rows = conn.execute(
                    """SELECT e.video_id FROM source_entries e JOIN videos v USING(video_id)
                       WHERE source_id=? AND e.state='candidate'
                       ORDER BY v.published_at IS NULL, v.published_at,
                                CASE WHEN ?!='manual' THEN e.sort_key END DESC, e.discovered_at,e.video_id LIMIT ?""",
                    (source_id, source["kind"], count),
                ).fetchall()
                for row in rows:
                    self.want(conn, source_id, row["video_id"], 10)
                remaining = count - len(rows)
                if remaining > 0 and source["kind"] != "manual":
                    set_meta(conn, f"force_deep:{source_id}", str(remaining))
                    conn.execute("UPDATE sources SET next_check_at=? WHERE id=?", (stamp(), source_id))
            else:
                raise ValueError("invalid source action")

    def delete_source(self, source_id: int, *, delete_files: bool) -> None:
        root = self.config.paths.output_dir
        with self.db.transaction() as conn:
            row = conn.execute("SELECT slug FROM sources WHERE id=?", (source_id,)).fetchone()
            if row is None:
                raise NotFound("source does not exist")
            if delete_files:
                folder = source_dir(root, row["slug"])
                if folder.is_dir():
                    shutil.rmtree(folder)
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            conn.execute("DELETE FROM meta WHERE key IN (?,?,?,?)",
                         (f"last_deep:{source_id}", f"force_deep:{source_id}",
                          f"backfill_cursor:{source_id}", f"atom_variant:{source_id}"))
            purge_unlinked(conn, self.config)

    def want(self, conn: sqlite3.Connection, sid: int, vid: str, priority: int, *, manual: bool = False) -> None:
        from_state = "('candidate','filtered','wanted')" if manual else "('candidate','wanted')"
        changed = conn.execute(
            f"""UPDATE source_entries SET state='wanted',filter_reason=NULL
                WHERE source_id=? AND video_id=? AND state IN {from_state}""", (sid, vid),
        ).rowcount
        if changed:
            activate_video(conn, self.config, vid, priority, retry=manual)

    def add_video(self, source_id: int, value: str) -> str:
        vid = parse_video(value)
        with self.db.transaction() as conn:
            source = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
            if source is None:
                raise NotFound("source does not exist")
            upsert_video(conn, {"id": vid})
            conn.execute(
                """INSERT INTO source_entries(source_id,video_id,state,evaluated_rev,discovered_at,sort_key)
                   VALUES (?,?,'wanted',?,?,(SELECT coalesce(max(sort_key),0)+1 FROM source_entries WHERE source_id=?))
                   ON CONFLICT(source_id,video_id) DO NOTHING""",
                (source_id, vid, source["policy_rev"], stamp(), source_id),
            )
            self.want(conn, source_id, vid, 100, manual=True)
        return vid

    def episode_command(self, source_id: int, video_id: str, action: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM source_entries WHERE source_id=? AND video_id=?", (source_id, video_id)).fetchone()
            if row is None:
                raise NotFound("episode does not exist")
            if action in ("pin", "unpin") and row["state"] not in ("retiring", "evicted"):
                conn.execute("UPDATE source_entries SET pinned=? WHERE source_id=? AND video_id=?", (int(action == "pin"), source_id, video_id))
            elif action in ("promote", "retry"):
                self.want(conn, source_id, video_id, 100, manual=True)
            elif action == "skip":
                conn.execute(
                    """UPDATE source_entries SET state='filtered',filter_reason='Skipped by administrator'
                       WHERE source_id=? AND video_id=? AND state IN ('wanted','candidate','filtered')""", (source_id, video_id),
                )
            elif action not in ("pin", "unpin"):
                raise ValueError("invalid episode action")

    def pause(self, paused: bool) -> None:
        with self.db.transaction() as conn:
            set_meta(conn, "global_paused", str(int(paused)))
            set_meta(conn, "global_pause_reason", "Paused by administrator" if paused else "")

    async def reset_breaker(self, key: str) -> None:
        with self.db.transaction() as conn:
            event = self.breakers.reset(conn, key)
        if event:
            await self.notifier.send([event])

    def list_breakers(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = {row["key"]: dict(row) for row in conn.execute("SELECT * FROM circuit_breakers")}
            ids = [r[0] for r in conn.execute("SELECT id FROM cookie_profiles")]
        return [
            rows.get(key, {"key": key, "state": "closed"})
            for key in ("global", "toolchain", "local", *(f"cookie:{i}" for i in ids))
        ]

    def retry(self, *, video_id: str | None = None, category: str | None = None) -> int:
        conditions = ["v.state IN ('failed','pending','deferred','unavailable')"]
        params = []
        if video_id:
            conditions.append("v.video_id=?")
            params.append(video_id)
        if category:
            conditions.append("v.last_category=?")
            params.append(Category(category))
        if not video_id and not category:
            raise ValueError("specify a video ID or an error category")
        with self.db.transaction() as conn:
            rows = conn.execute(f"SELECT v.video_id FROM videos v WHERE {' AND '.join(conditions)}", params).fetchall()
            for row in rows:
                activate_video(conn, self.config, row["video_id"], 100 if video_id else 30, retry=True)
        return len(rows)

    def video_command(self, video_id: str, action: str) -> None:
        if action == "retry":
            self.retry(video_id=video_id)
            return
        with self.db.transaction() as conn:
            if action == "prioritize":
                conn.execute("UPDATE videos SET priority=100 WHERE video_id=?", (video_id,))
            elif action == "skip":
                conn.execute(
                    """UPDATE videos SET state='unavailable',next_attempt_at=NULL,running_since=NULL,
                       last_error='Skipped by administrator',updated_at=?
                       WHERE video_id=? AND state NOT IN ('ready','running')""", (stamp(), video_id),
                )
            else:
                raise ValueError("invalid video action")

    def cookies(self) -> list[sqlite3.Row]:
        with self.db.connect() as conn:
            return conn.execute("SELECT * FROM cookie_profiles ORDER BY name").fetchall()

    def save_cookie(self, name: str, contents: str, note: str = "", cookie_id: int | None = None) -> int:
        name = name.strip()
        if not name:
            raise ValueError("enter a name for the cookie profile")
        earliest = expiry(contents)
        now = stamp()
        # A cookie update must be validated again before clearing an expired profile's breaker.
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM cookie_profiles WHERE name=? AND id!=?", (name, cookie_id or -1)).fetchone():
                raise ValueError("cookie profile name already exists")
            if cookie_id is None:
                cursor = conn.execute(
                    """INSERT INTO cookie_profiles(name,file_path,status,earliest_expiry,note,created_at,updated_at)
                       VALUES (?,?,'active',?,?,?,?)""", (name, "pending", earliest, note, now, now),
                )
                cookie_id = cursor.lastrowid
                assert cookie_id is not None
            else:
                if conn.execute(
                    """UPDATE cookie_profiles SET name=?,status='suspect',earliest_expiry=?,note=?,updated_at=? WHERE id=?""",
                    (name, earliest, note, now, cookie_id),
                ).rowcount == 0:
                    raise NotFound("cookie profile does not exist")
            filename = f"{cookie_id}.txt"
            save_cookie(self.config.paths.state_dir / "cookies", filename, contents)
            conn.execute("UPDATE cookie_profiles SET file_path=? WHERE id=?", (filename, cookie_id))
            return cookie_id

    def delete_cookie(self, cookie_id: int) -> None:
        with self.db.transaction() as conn:
            row = cookie_for(conn, cookie_id)
            if row is None:
                raise NotFound("cookie profile does not exist")
            safe_path(self.config.paths.state_dir / "cookies", row["file_path"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM cookie_profiles WHERE id=?", (cookie_id,))
            conn.execute("DELETE FROM circuit_breakers WHERE key=?", (f"cookie:{cookie_id}",))

    def _reactivate_auth_videos(self, conn: sqlite3.Connection, cookie_id: int, *, source_id: int | None = None) -> None:
        rows = conn.execute(
            """SELECT DISTINCT v.video_id FROM videos v JOIN source_entries e USING(video_id)
               JOIN sources s ON s.id=e.source_id
               WHERE s.cookie_profile_id=? AND (? IS NULL OR s.id=?)
                 AND e.state='wanted' AND v.state='unavailable' AND v.last_category='auth_required'""",
            (cookie_id, source_id, source_id),
        ).fetchall()
        for row in rows:
            activate_video(conn, self.config, row["video_id"], 50, retry=True)

    async def validate_cookie(self, cookie_id: int, value: str = "") -> bool:
        vid = parse_video(value) if value.strip() else DEFAULT_COOKIE_TEST_VIDEO
        with self.db.transaction() as conn:
            cookie = cookie_for(conn, cookie_id)
            if cookie is None:
                raise NotFound("cookie profile does not exist")
            self.breakers.refresh(conn)
            if not self.breakers.allowed(conn, NETWORK_GATES):
                raise SourceError("validation is blocked by an open circuit breaker")
            attempt = start_attempt(conn, "cookie_check", cookie_id=cookie_id)
            self.breakers.reserve(conn, NETWORK_GATES, attempt)
        run = RunResult()
        try:
            with TemporaryDirectory(dir=self.config.paths.state_dir / "tmp") as work:
                isolated = copy_cookie(self.config.paths.state_dir / "cookies", cookie["file_path"], Path(work))
                if not has_youtube_auth_cookie(isolated.read_text(encoding="utf-8", errors="replace")):
                    run.error = {"msg": "Cookie file contains no YouTube login session cookies; cookies are invalid"}
                else:
                    run = await self.runner.run("probe", url=canonical_video(vid), cookiefile=isolated)
                    self.discovery.check_version(run)
                    if not run.error and run.probe.get("id") != vid:
                        run.error = {"msg": "cookie probe did not return the expected video"}
        except asyncio.CancelledError:
            with self.db.transaction() as conn:
                interrupt_attempt(conn, self.breakers, attempt, "Cookie validation interrupted")
            raise
        except Exception as exc:
            run.error = {"exc_class": type(exc).__name__, "msg": redact(str(exc))}
        classification = classify(run, used_cookie=True, minimum=self.config.toolchain.min_ytdlp_version)
        valid = not run.error and classification.category != Category.COOKIE_INVALID
        with self.db.transaction() as conn:
            current = cookie_for(conn, cookie_id)
            if not current or current["updated_at"] != cookie["updated_at"]:
                interrupt_attempt(conn, self.breakers, attempt, "Cookie profile changed")
                return False
            _, events = finish_attempt(conn, self.breakers, attempt, run, "ok" if valid else "error",
                                       None if valid else classification, cookie_id,
                                       cookie_updated_at=cookie["updated_at"])
            if valid:
                conn.execute("UPDATE cookie_profiles SET status='active',last_ok_at=?,updated_at=? WHERE id=?", (stamp(), stamp(), cookie_id))
                if event := self.breakers.reset(conn, f"cookie:{cookie_id}"):
                    events.append(event)
                conn.execute("UPDATE sources SET next_check_at=? WHERE cookie_profile_id=?", (stamp(), cookie_id))
                self._reactivate_auth_videos(conn, cookie_id)
            else:
                conn.execute(
                    """UPDATE cookie_profiles SET status=CASE WHEN status='expired' THEN status ELSE 'suspect' END,
                       last_fail_at=?,updated_at=? WHERE id=?""", (stamp(), stamp(), cookie_id),
                )
        await self.notifier.send(events)
        return valid

    def disk_space(self) -> dict[str, int]:
        p = self.config.paths
        return {"state": shutil.disk_usage(p.state_dir).free, "output": shutil.disk_usage(p.output_dir).free}

    async def doctor(self) -> dict[str, Any]:
        with self.db.transaction() as conn:
            attempt = start_attempt(conn, "doctor")
        try:
            run = await self.runner.run("doctor")
        except asyncio.CancelledError:
            with self.db.transaction() as conn:
                interrupt_attempt(conn, self.breakers, attempt, "Doctor interrupted")
            raise
        toolchain = self.config.toolchain
        minimum = toolchain.min_ytdlp_version
        if not run.error:
            missing = []
            if not run.version or version_parts(run.version) < version_parts(minimum):
                missing.append(f"yt-dlp >= {minimum}")
            if not run.hello.get("ffmpeg_version"):
                missing.append("ffmpeg")
            if not shutil.which(toolchain.ffprobe_path):
                missing.append("ffprobe")
            if toolchain.js_runtime and not run.hello.get("js_runtime_version"):
                missing.append("JavaScript runtime")
            if missing:
                run.error = {"exc_class": "ToolchainError", "msg": "Missing or outdated: " + ", ".join(missing)}
        free = self.disk_space()
        if min(free.values()) < self.config.disk.min_free_gb * 1024**3 and not run.error:
            run.error = {"exc_class": "LocalResource", "msg": "Disk space below configured minimum"}
        failure = classify(run, minimum=minimum) if run.error else None
        with self.db.transaction() as conn:
            self.breakers.refresh(conn, run.version if not run.error else None)
            _, events = finish_attempt(conn, self.breakers, attempt, run, "error" if failure else "ok", failure)
            report = {
                **{key: value for key, value in run.hello.items() if key != "type"},
                "ok": failure is None, "error": failure.reason if failure else None, "disk_free": free,
                "orphan_audio": json.loads(get_meta(conn, "orphan_audio", "[]")),
                "checked_at": stamp(),
            }
            set_meta(conn, "last_doctor", json.dumps(report))
        await self.notifier.send(events)
        return report

    @contextmanager
    def service_lock(self) -> Iterator[None]:
        import fcntl

        with (self.config.paths.state_dir / "service.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise RuntimeError("a Pod Hub scheduler already owns this state directory") from exc
            yield
