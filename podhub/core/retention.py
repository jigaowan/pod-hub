from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from podhub.config import Config
from podhub.db.connection import Database, set_meta
from podhub.db.repositories import activate_video, policy_for
from podhub.domain.models import stamp, utc_now
from podhub.storage.media import atomic_publish
from podhub.storage.paths import safe_path

log = logging.getLogger(__name__)


def purge_unlinked(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE videos SET state='purged',media_bytes=NULL,media_duration_s=NULL,media_mime=NULL,
           ready_at=NULL,give_up_at=NULL,next_attempt_at=NULL,updated_at=? WHERE state='ready'
           AND NOT EXISTS (SELECT 1 FROM source_entries e WHERE e.video_id=videos.video_id
             AND e.state IN ('published','retiring'))""", (stamp(),),
    )


def sweep(config: Config, db: Database, now: datetime | None = None) -> None:
    now = now or utc_now()
    root = config.paths.output_dir
    with db.transaction() as conn:
        for source in conn.execute("SELECT * FROM sources").fetchall():
            policy = policy_for(source)
            sid = source["id"]
            if policy.keep_count or policy.keep_days:
                entries = conn.execute(
                    """SELECT e.*,coalesce(v.published_at,e.published_at) AS episode_date
                       FROM source_entries e JOIN videos v ON v.video_id=e.video_id
                       WHERE e.source_id=? AND e.state='published' ORDER BY episode_date DESC, e.video_id""",
                    (sid,),
                ).fetchall()
                cutoff = stamp(now, seconds=-policy.keep_days * 86400)
                for index, row in enumerate(entries):
                    if not row["pinned"] and (
                        (policy.keep_count and index >= policy.keep_count) or
                        (policy.keep_days and row["episode_date"] < cutoff)
                    ):
                        conn.execute(
                            "UPDATE source_entries SET state='retiring',unpublished_at=? WHERE source_id=? AND video_id=?",
                            (stamp(now), sid, row["video_id"]),
                        )
            retiring = conn.execute(
                "SELECT * FROM source_entries WHERE source_id=? AND state='retiring' AND unpublished_at<=?",
                (sid, stamp(now, seconds=-policy.unpublish_grace_hours * 3600)),
            ).fetchall()
            for row in retiring:
                vid = row["video_id"]
                artifacts = conn.execute(
                    "SELECT rel_path FROM video_artifacts WHERE source_id=? AND video_id=?", (sid, vid),
                )
                for artifact in artifacts:
                    safe_path(root, artifact["rel_path"]).unlink(missing_ok=True)
                if row["entry_media_path"]:
                    safe_path(root, row["entry_media_path"]).unlink(missing_ok=True)
                conn.execute("DELETE FROM video_artifacts WHERE source_id=? AND video_id=?", (sid, vid))
                conn.execute("DELETE FROM postprocess_tasks WHERE source_id=? AND video_id=?", (sid, vid))
                conn.execute(
                    """UPDATE source_entries SET state='evicted',evicted_at=?,entry_media_path=NULL
                       WHERE source_id=? AND video_id=?""", (stamp(now), sid, vid),
                )
        purge_unlinked(conn)
        set_meta(conn, "last_retention_at", stamp(now))


def reconcile(config: Config, db: Database) -> list[str]:
    root = config.paths.output_dir
    with db.transaction() as conn:
        rows = conn.execute(
            """SELECT e.* FROM source_entries e JOIN videos v ON e.video_id=v.video_id
               WHERE v.state='ready' AND e.state IN ('published','retiring') AND e.entry_media_path IS NOT NULL""",
        ).fetchall()
        existing = {}
        missing = []
        for row in rows:
            path = safe_path(root, row["entry_media_path"])
            if path.is_file():
                existing[row["video_id"]] = path
            else:
                missing.append((row, path))
        for row, path in missing:
            vid = row["video_id"]
            if origin := existing.get(vid):
                atomic_publish(origin, path, hardlink=True)
            elif row["state"] == "published":
                conn.execute(
                    """UPDATE source_entries SET state='wanted',entry_media_path=NULL
                       WHERE video_id=? AND state='published'""", (vid,),
                )
                conn.execute(
                    "UPDATE videos SET state='purged',give_up_at=NULL,media_bytes=NULL,media_duration_s=NULL WHERE video_id=?",
                    (vid,),
                )
                activate_video(conn, config, vid, 50)
        referenced = {row[0] for row in conn.execute("SELECT entry_media_path FROM source_entries WHERE entry_media_path IS NOT NULL")}
        orphans = sorted(
            str(file.relative_to(root)) for file in root.glob("*/*.m4a")
            if file.is_file() and str(file.relative_to(root)) not in referenced
        )
        if orphans:
            log.warning("Found %s unregistered audio files. First entries: %s", len(orphans), orphans[:20])
        set_meta(conn, "orphan_audio", json.dumps(orphans))
        set_meta(conn, "last_reconcile_at", stamp())
    return orphans
