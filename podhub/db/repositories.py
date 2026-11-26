from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from podhub.config import Config
from podhub.db.connection import Database
from podhub.domain.models import parse_date, stamp, utc_now
from podhub.domain.policy import SourcePolicy


def policy_for(source: sqlite3.Row) -> SourcePolicy:
    return SourcePolicy.model_validate_json(source["policy_json"])


def public_media(conn: sqlite3.Connection, video_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM source_entries WHERE video_id=? AND state IN ('published','retiring')
           AND entry_media_path IS NOT NULL ORDER BY state, source_id""", (video_id,),
    ).fetchall()


def upsert_video(conn: sqlite3.Connection, info: dict[str, Any]) -> None:
    video_id = info["id"]
    row = conn.execute("SELECT meta_json FROM videos WHERE video_id=?", (video_id,)).fetchone()
    metadata = json.loads((row["meta_json"] if row else None) or "{}")
    exact = parse_date(info.get("timestamp"))
    published = exact or parse_date(info.get("upload_date"))
    quality = 1 if info.get("_flat_listing") else (3 if exact else 2)
    if quality < metadata.get("_published_quality", 0):
        published = None
    elif published:
        metadata["_published_quality"] = quality
    duration = info.get("duration")
    if not isinstance(duration, (int, float)) or duration <= 0:
        duration = None
    fields = (
        "channel_id", "description", "published_at", "upload_date", "duration_s",
        "live_status", "availability", "release_timestamp", "thumbnail_url",
    )
    now = stamp()
    conn.execute(
        f"""INSERT INTO videos(video_id,title,{','.join(fields)},created_at,updated_at)
            VALUES (?, ?, {','.join('?' for _ in fields)}, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
              title=CASE WHEN excluded.title=excluded.video_id THEN videos.title ELSE excluded.title END,
              {','.join(f'{key}=coalesce(excluded.{key},videos.{key})' for key in fields)},
              updated_at=excluded.updated_at""",
        (video_id, info.get("title") or video_id, info.get("channel_id"), info.get("description"),
         published, info.get("upload_date"), duration, info.get("live_status"), info.get("availability"),
         info.get("release_timestamp"), info.get("thumbnail"), now, now),
    )
    # Carry forward the fields used for filter re-evaluation when flat metadata is incomplete.
    patch = {key: value for key, value in info.items() if value is not None and key != "_flat_listing"}
    if not published:
        patch.pop("timestamp", None)
        patch.pop("upload_date", None)
    metadata.update(patch)
    conn.execute("UPDATE videos SET meta_json=? WHERE video_id=?", (json.dumps(metadata), video_id))


def activate_video(
    conn: sqlite3.Connection, config: Config, video_id: str, priority: int, *, retry: bool = False,
) -> None:
    now = utc_now()
    if retry:
        conn.execute(
            """UPDATE videos SET state='pending', priority=?, next_attempt_at=NULL, running_since=NULL,
               give_up_at=?, attempt_count=0, category_attempts_json='{}', unclassified=0, updated_at=?
               WHERE video_id=? AND state NOT IN ('ready','running')""",
            (priority, stamp(now, seconds=config.retry.give_up_after_days * 86400), stamp(now), video_id),
        )
    else:
        conn.execute(
            """UPDATE videos SET state='pending',
               priority=CASE WHEN give_up_at IS NULL THEN ? ELSE max(priority,?) END,
               give_up_at=coalesce(give_up_at,?), updated_at=?
               WHERE video_id=? AND state IN ('pending','purged')""",
            (priority, priority, stamp(now, seconds=config.retry.give_up_after_days * 86400), stamp(now), video_id),
        )


def expire_deadlines(conn: sqlite3.Connection, now: str) -> None:
    conn.execute(
        """UPDATE videos SET state=CASE WHEN state='deferred' AND last_category='not_ready' THEN 'unavailable' ELSE 'failed' END,
           next_attempt_at=NULL, updated_at=?
           WHERE state IN ('pending','deferred') AND give_up_at<=?""", (now, now),
    )



def claim_video(conn: sqlite3.Connection, now: str) -> tuple[sqlite3.Row, int] | None:
    expire_deadlines(conn, now)
    selected = conn.execute(
        """SELECT v.video_id, s.id AS source_id FROM videos v
           JOIN source_entries e ON e.video_id=v.video_id
           JOIN sources s ON s.id=e.source_id
           LEFT JOIN cookie_profiles c ON c.id=s.cookie_profile_id
           LEFT JOIN circuit_breakers b ON b.key='cookie:' || c.id
           WHERE (v.state='pending' OR (v.state IN ('deferred','unavailable') AND v.next_attempt_at IS NOT NULL))
           AND (v.next_attempt_at IS NULL OR v.next_attempt_at<=?)
           AND e.state='wanted' AND s.enabled=1
           AND (c.id IS NULL OR c.status IN ('active','suspect'))
           AND coalesce(b.state,'closed')='closed'
           AND (coalesce(v.last_category,'')!='auth_required' OR c.id IS NOT NULL OR NOT EXISTS (
             SELECT 1 FROM source_entries e2 JOIN sources s2 ON e2.source_id=s2.id
             JOIN cookie_profiles c2 ON c2.id=s2.cookie_profile_id
             LEFT JOIN circuit_breakers b2 ON b2.key='cookie:' || c2.id
             WHERE e2.video_id=v.video_id AND e2.state='wanted' AND s2.enabled=1
               AND c2.status IN ('active','suspect') AND coalesce(b2.state,'closed')='closed'
           ))
           ORDER BY v.priority DESC,
             (SELECT max(a.started_at) FROM attempts a WHERE a.kind='download' AND a.source_id=s.id),
             v.published_at DESC, v.created_at, s.id, v.video_id LIMIT 1""", (now,),
    ).fetchone()
    if selected is None:
        return None
    video = conn.execute(
        """UPDATE videos SET state='running', running_since=?, updated_at=?
           WHERE video_id=? RETURNING *""", (now, now, selected["video_id"]),
    ).fetchone()
    return video, selected["source_id"]


def recover(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute("UPDATE videos SET state='pending',running_since=NULL WHERE state='running'")
        conn.execute("UPDATE postprocess_tasks SET state='pending' WHERE state='running'")
        # Incomplete audit reservations must not count towards daily limits.
        conn.execute("UPDATE attempts SET ended_at=?,outcome='skipped',error_excerpt='Interrupted by restart' WHERE outcome='running'", (stamp(),))


def start_attempt(
    conn: sqlite3.Connection, kind: str, source_id: int | None = None,
    video_id: str | None = None, cookie_id: int | None = None,
) -> int:
    now = stamp()
    cursor = conn.execute(
        """INSERT INTO attempts(kind,source_id,video_id,started_at,ended_at,outcome,cookie_profile_id)
           VALUES (?,?,?,?,?,'running',?)""", (kind, source_id, video_id, now, now, cookie_id),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def prune_attempts(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("DELETE FROM attempts WHERE outcome!='running' AND ended_at<?", (stamp(now, seconds=-30 * 86400),))
    conn.execute(
        """DELETE FROM attempts WHERE outcome!='running' AND id NOT IN
           (SELECT id FROM attempts WHERE outcome!='running' ORDER BY id DESC LIMIT 10000)""",
    )


def utc_day() -> str:
    return stamp(datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0))
