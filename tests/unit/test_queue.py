from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from podhub.core.scheduler import Scheduler
from podhub.db.repositories import claim_video, start_attempt
from podhub.domain.models import stamp

from conftest import OTHER, THIRD, VIDEO


async def test_atomic_claim_single_winner(services, source_factory):
    sid = await source_factory()
    services.add_video(sid, VIDEO)

    def claim(_):
        with services.db.transaction() as conn:
            result = claim_video(conn, stamp())
            return result[0]["video_id"] if result else None

    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(claim, range(8)))
    assert results.count(VIDEO) == 1


async def test_priority_and_source_fairness(services, source_factory):
    a, b = await source_factory("A"), await source_factory("B")
    backfill, manual, new, retry = VIDEO, OTHER, THIRD, "retry123456"
    services.add_video(a, backfill)
    services.add_video(a, manual)
    services.add_video(a, new)
    services.add_video(b, retry)
    with services.db.transaction() as conn:
        for vid, priority in [(backfill, 10), (manual, 100), (new, 50), (retry, 30)]:
            conn.execute("UPDATE videos SET priority=? WHERE video_id=?", (priority, vid))
        assert [claim_video(conn, stamp())[0]["video_id"] for _ in range(4)] == [manual, new, retry, backfill]
        conn.execute("UPDATE videos SET state='pending',priority=50")
        attempt = start_attempt(conn, "download", a, manual)
        assert claim_video(conn, stamp())[1] == b
        conn.execute("UPDATE attempts SET outcome='ok' WHERE id=?", (attempt,))
        start_attempt(conn, "download", b, retry)
        assert claim_video(conn, stamp())[1] == a


async def test_recovery_resets_claim_and_cleans_private_temporary_files(services, source_factory):
    sid = await source_factory()
    services.add_video(sid, VIDEO)
    with services.db.transaction() as conn:
        claim_video(conn, stamp())
        start_attempt(conn, "download", sid, VIDEO)
    work = services.config.paths.state_dir / "tmp" / "stale"
    work.mkdir()
    (work / "media.part").write_bytes(b"old data")
    Scheduler(services).recover()
    assert not work.exists()
    with services.db.connect() as conn:
        video = conn.execute("SELECT * FROM videos").fetchone()
        assert video["state"] == "pending" and video["running_since"] is None and video["attempt_count"] == 0
        assert conn.execute("SELECT outcome FROM attempts").fetchone()[0] == "skipped"


def test_sqlite_invariants_and_backup(services):
    db = services.db
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    backup = db.backup(services.config.paths.state_dir / "backups")
    assert backup.is_file() and backup.stat().st_size > 0
