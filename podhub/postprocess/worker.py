from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from podhub.config import Paths
from podhub.db.connection import Database
from podhub.domain.models import stamp
from podhub.postprocess.base import PostProcessContext
from podhub.postprocess.registry import PostProcessorRegistry
from podhub.storage.media import atomic_publish
from podhub.storage.paths import safe_path, source_dir

log = logging.getLogger(__name__)


class PostprocessWorker:
    def __init__(self, paths: Paths, db: Database, registry: PostProcessorRegistry):
        self.paths = paths
        self.db = db
        self.registry = registry

    def enqueue_for_video(self, conn: sqlite3.Connection, video_id: str, source: sqlite3.Row) -> None:
        if not self.registry:
            return
        policy = json.loads(source["policy_json"])
        for processor in self.registry:
            try:
                enabled = processor.is_enabled_for_source(policy)
            except Exception:
                log.exception("Could not check processor %s", processor.name)
                continue
            if enabled:
                conn.execute(
                    """INSERT INTO postprocess_tasks(video_id,source_id,processor_name,created_at,updated_at)
                       VALUES (?,?,?,?,?) ON CONFLICT(video_id,source_id,processor_name) DO NOTHING""",
                    (video_id, source["id"], processor.name, stamp(), stamp()),
                )

    async def run_next(self) -> None:
        if not self.registry:
            return
        with self.db.transaction() as conn:
            task = conn.execute(
                """SELECT t.*, s.slug, e.entry_media_path, v.meta_json FROM postprocess_tasks t
                   JOIN sources s ON s.id=t.source_id
                   JOIN videos v ON v.video_id=t.video_id
                   JOIN source_entries e ON e.video_id=t.video_id AND e.source_id=t.source_id
                   WHERE t.state='pending' AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=?)
                   AND e.state='published' AND v.state='ready' ORDER BY t.id LIMIT 1""", (stamp(),),
            ).fetchone()
            if task is None:
                return
            conn.execute(
                "UPDATE postprocess_tasks SET state='running',attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                (stamp(), task["id"]),
            )
        try:
            processor = self.registry.get(task["processor_name"])
            if processor is None:
                raise ValueError("processor is no longer registered")
            # Plugins write into a private stage. Only validated files are moved into output.
            with TemporaryDirectory(dir=self.paths.state_dir / "tmp") as work:
                staged = Path(work)
                output = safe_path(source_dir(self.paths.output_dir, task["slug"]), "artifacts")
                results = await processor.process(PostProcessContext(
                    task["video_id"], safe_path(self.paths.output_dir, task["entry_media_path"]),
                    staged, json.loads(task["meta_json"] or "{}"),
                ))
                files = []
                for result in results:
                    destination = safe_path(self.paths.output_dir, result.rel_path)
                    if (
                        destination.parent != output
                        or not destination.name.startswith(f"{task['video_id']}.")
                        or not result.kind
                        or not result.mime_type
                    ):
                        raise ValueError("artifact must belong to the source's artifacts folder and be prefixed with video_id.")
                    origin = safe_path(staged, destination.name)
                    if not origin.is_file():
                        raise ValueError("processor did not create the declared artifact")
                    files.append((result, origin, destination))
                with self.db.transaction() as conn:
                    current = conn.execute(
                        "SELECT state FROM source_entries WHERE source_id=? AND video_id=?",
                        (task["source_id"], task["video_id"]),
                    ).fetchone()
                    if not current or current["state"] not in ("published", "retiring"):
                        conn.execute("UPDATE postprocess_tasks SET state='pending',updated_at=? WHERE id=?", (stamp(), task["id"]))
                        return
                    for result, _, _ in files:
                        owner = conn.execute(
                            "SELECT video_id,source_id,kind FROM video_artifacts WHERE rel_path=?",
                            (result.rel_path,),
                        ).fetchone()
                        if owner and (
                            owner["video_id"] != task["video_id"]
                            or owner["source_id"] != task["source_id"]
                            or owner["kind"] != result.kind
                        ):
                            raise ValueError("artifact path is already owned by another episode or artifact kind")
                    for result, origin, destination in files:
                        atomic_publish(origin, destination)
                        conn.execute(
                            """INSERT INTO video_artifacts(video_id,source_id,kind,rel_path,mime_type,is_public,created_at)
                               VALUES (?,?,?,?,?,?,?) ON CONFLICT(video_id,source_id,kind) DO UPDATE SET
                               rel_path=excluded.rel_path,mime_type=excluded.mime_type,is_public=excluded.is_public,
                               created_at=excluded.created_at""",
                            (task["video_id"], task["source_id"], result.kind, result.rel_path,
                             result.mime_type, int(result.public), stamp()),
                        )
                    conn.execute("UPDATE postprocess_tasks SET state='done',updated_at=? WHERE id=?", (stamp(), task["id"]))
        except asyncio.CancelledError:
            with self.db.transaction() as conn:
                conn.execute("UPDATE postprocess_tasks SET state='pending' WHERE id=?", (task["id"],))
            raise
        except Exception as exc:
            log.warning("Processor %s failed for %s", task["processor_name"], task["video_id"])
            with self.db.transaction() as conn:
                conn.execute(
                    "UPDATE postprocess_tasks SET state='failed',last_error=?,updated_at=? WHERE id=?",
                    (str(exc)[:1000], stamp(), task["id"]),
                )
