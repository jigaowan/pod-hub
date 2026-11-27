from __future__ import annotations

import asyncio
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from podhub.core.discovery import list_entries
from podhub.core.retention import reconcile, sweep
from podhub.core.scheduler import Scheduler
from podhub.db.connection import set_meta
from podhub.db.repositories import claim_video, start_attempt, upsert_video
from podhub.domain.models import stamp, utc_now
from podhub.main import create_app
from podhub.outputs.rss import ITUNES, PODCAST
from podhub.postprocess.base import ArtifactResult
from podhub.storage.media import STAGING_PREFIX, save_cover
from podhub.storage.paths import validate_folder_name
from podhub.ytdlp.runner import RunResult

from conftest import OTHER, THIRD, VIDEO


async def download_one(services, media):
    services.runner.media = media
    with services.db.transaction() as conn:
        claim = claim_video(conn, stamp())
        assert claim
        video, sid = claim
        attempt = start_attempt(conn, "download", sid, video["video_id"])
    await services.downloader.execute(video, sid, attempt)


async def test_atomic_publish_hardlink_deletion_and_reconciliation(services, source_factory, sample_media):
    a = await source_factory("中文　Cafe\u0301")
    b = await source_factory("日本語 放送")
    root = services.config.paths.output_dir
    services.add_video(a, VIDEO)
    await download_one(services, sample_media)
    filename = f"20260105 - 中文节目 ]] [{VIDEO}].m4a"
    original = root / "中文 Café" / filename
    assert original.read_bytes() == sample_media.read_bytes()
    assert list(original.parent.iterdir()) == [original]

    with services.db.transaction() as conn:
        upsert_video(conn, {"id": VIDEO, "title": "上游改标题", "upload_date": "20260106"})
    services.add_video(b, VIDEO)
    services.downloader.link_ready()
    sibling = root / "日本語 放送" / filename
    assert original.samefile(sibling)
    with services.db.connect() as conn:
        entries = dict(conn.execute("SELECT source_id, entry_media_path FROM source_entries WHERE video_id=?", (VIDEO,)))
        assert entries == {a: str(original.relative_to(root)), b: str(sibling.relative_to(root))}
    assert [mode for mode, _ in services.runner.calls].count("fetch") == 1
    sibling.unlink()
    orphan = original.with_name(f"{OTHER}.m4a")
    shutil.copyfile(original, orphan)
    assert reconcile(services.config, services.db) == [str(orphan.relative_to(root))]
    assert original.samefile(sibling)
    await source_factory("Unused")
    services.delete_source(a, delete_files=True)
    assert not original.exists()
    assert sibling.is_file()
    sibling.unlink()
    assert reconcile(services.config, services.db) == []
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "pending"
        assert conn.execute("SELECT state FROM source_entries WHERE source_id=?", (b,)).fetchone()[0] == "wanted"


async def test_generated_folder_suffix_fits_byte_limit(services, source_factory):
    name = "聲" * 40
    first = await source_factory(name)
    second = await source_factory(f"{name}音")
    assert services.source(first)["slug"] == name
    slug = services.source(second)["slug"]
    assert slug == "聲" * 39 + "-2"
    assert validate_folder_name(slug) == slug


async def test_unicode_folder_recovery_cover_and_media_routes(services, source_factory, sample_media):
    sid = await source_factory("東京 音訊")
    source = services.source(sid)
    paths = services.config.paths
    folder = paths.output_dir / source["slug"]
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    artwork = paths.state_dir / "tmp" / "fixture.jpg"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32",
        "-frames:v", "1", "--", str(artwork),
    ], check=True, capture_output=True)
    cover_bytes = artwork.read_bytes()
    cover_name = save_cover(artwork, paths.output_dir, source["slug"])
    with services.db.transaction() as conn:
        conn.execute("UPDATE sources SET cover_filename=? WHERE id=?", (cover_name, sid))

    artifact_dir = folder / "artifacts"
    artifact_dir.mkdir()
    stale_files = [directory / f"{STAGING_PREFIX}stale" for directory in (folder, artifact_dir)]
    for file in stale_files:
        file.touch()
    hidden = paths.output_dir / ".hidden"
    hidden.mkdir()
    untouched = hidden / f"{STAGING_PREFIX}stale"
    untouched.touch()
    Scheduler(services).recover()
    assert all(not file.exists() for file in stale_files)
    assert untouched.is_file()
    assert not reconcile(services.config, services.db)

    token = source["feed_token"]
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        feed = ET.fromstring(client.get(f"/feeds/{token}.xml").content)
        assert feed.findtext("channel/title") == "東京 音訊"
        cover_url = f"/covers/{token}/{cover_name}"
        image = feed.find(f"channel/{{{ITUNES}}}image")
        assert image.attrib["href"] == f"{services.config.web.base_url}{cover_url}"
        cover = client.get(cover_url)
        assert cover.status_code == 200 and cover.content == cover_bytes
        media_url = f"/media/{token}/{VIDEO}.m4a"
        assert feed.find("channel/item/enclosure").attrib["url"] == f"{services.config.web.base_url}{media_url}"
        media = client.get(media_url, headers={"Range": "bytes=0-15"})
        assert media.status_code == 206 and media.content == sample_media.read_bytes()[:16]


async def test_media_endpoint_confines_stored_path_to_source_folder(services, source_factory, sample_media):
    sid = await source_factory("節目")
    source = services.source(sid)
    root = services.config.paths.output_dir
    folder = root / source["slug"]
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    foreign = root / "別の番組" / "private.m4a"
    foreign.parent.mkdir()
    shutil.copyfile(sample_media, foreign)
    symlink = folder / "alias.m4a"
    symlink.symlink_to(foreign)
    nested = folder / "artifacts" / "nested.m4a"
    nested.parent.mkdir()
    shutil.copyfile(sample_media, nested)
    media_url = f"/media/{source['feed_token']}/{VIDEO}.m4a"
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        assert client.head(media_url).status_code == 200
        for path in (str(foreign), str(foreign.relative_to(root)), str(symlink.relative_to(root)),
                     str(nested.relative_to(root)), f"{source['slug']}/../別の番組/private.m4a"):
            with services.db.transaction() as conn:
                conn.execute(
                    "UPDATE source_entries SET entry_media_path=? WHERE source_id=? AND video_id=?", (path, sid, VIDEO),
                )
            assert client.get(media_url).status_code == 404


async def test_feed_range_etag_tokens_and_retention(services, source_factory, sample_media):
    sid = await source_factory("Podcast", keep_count=1, unpublish_grace_hours=1)
    row = services.source(sid)
    token = row["feed_token"]
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    services.add_video(sid, OTHER)
    await download_one(services, sample_media)
    with services.db.transaction() as conn:
        conn.execute("UPDATE videos SET published_at=? WHERE video_id=?", (stamp(seconds=-86400), VIDEO))
        conn.execute("UPDATE videos SET published_at=?,thumbnail_url=? WHERE video_id=?", (stamp(), "https://evil.test/a", OTHER))
        saved_path = conn.execute(
            "SELECT entry_media_path FROM source_entries WHERE source_id=? AND video_id=?", (sid, VIDEO),
        ).fetchone()[0]
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        endpoint = f"/feeds/{token}.xml"
        url = f"/media/{token}/{VIDEO}.m4a"
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.headers["x-robots-tag"] == "noindex"
        assert client.get(endpoint, headers={"If-None-Match": f'W/{response.headers["etag"]}'}).status_code == 304
        assert client.head(endpoint).content == b""
        root = ET.fromstring(response.content)
        assert root.attrib == {"version": "2.0"}
        assert root.findtext(f"channel/{{{ITUNES}}}author") == "Podcast"
        item = root.find(f".//item[guid='yt:video:{VIDEO}']")
        assert item.find("guid").attrib == {"isPermaLink": "false"}
        assert "\x01" not in item.findtext("title") and "]]>" in item.findtext("title")
        assert item.findtext("link") == f"https://www.youtube.com/watch?v={VIDEO}"
        assert item.find(f"{{{ITUNES}}}image").get("href") == f"https://i.ytimg.com/vi/{VIDEO}/default.jpg"
        assert root.find(f".//item[guid='yt:video:{OTHER}']/{{{ITUNES}}}image") is None
        assert item.find("enclosure").attrib == {
            "url": f"{services.config.web.base_url}{url}", "length": str(sample_media.stat().st_size), "type": "audio/mp4",
        }
        assert item.findtext(f"{{{ITUNES}}}duration") == "2"

        head = client.head(url)
        assert head.status_code == 200 and not head.content
        assert head.headers["content-length"] == str(sample_media.stat().st_size)
        assert head.headers["accept-ranges"] == "bytes"
        partial = client.get(url, headers={"Range": "bytes=0-1023"})
        assert partial.status_code == 206 and partial.content == sample_media.read_bytes()[:1024]
        assert partial.headers["content-range"] == f"bytes 0-1023/{sample_media.stat().st_size}"
        assert client.get(url, headers={"If-None-Match": head.headers["etag"]}).status_code == 304
        assert client.get(url, headers={"Range": "bytes=99999999-"}).status_code == 416
        assert client.get(url.replace(token, "wrong")).status_code == 404
        assert client.get(f"/media/{token}/app.db").status_code == 404

        services.config.web.base_url = "https://new-domain.example"
        moved = ET.fromstring(client.get(endpoint).content)
        assert moved.findtext(f".//item[guid='yt:video:{VIDEO}']/guid") == f"yt:video:{VIDEO}"
        assert moved.find(f".//item[guid='yt:video:{VIDEO}']/enclosure").attrib["url"].startswith("https://new-domain.example/")
        now = utc_now()
        sweep(services.config, services.db, now)
        assert f"yt:video:{VIDEO}" not in client.get(endpoint).text
        assert client.get(url).status_code == 200
        sweep(services.config, services.db, now + timedelta(hours=1, seconds=1))
        assert client.get(url).status_code == 404
        assert not (services.config.paths.output_dir / saved_path).exists()
        with services.db.transaction() as conn:
            list_entries(conn, services.config, row, [{"id": VIDEO, "title": "Rediscovered"}])
            assert conn.execute("SELECT state FROM source_entries WHERE source_id=? AND video_id=?", (sid, VIDEO)).fetchone()[0] == "evicted"
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "purged"
        services.source_command(sid, "rotate")
        assert client.get(endpoint).status_code == 404
        assert client.get(f"/feeds/{services.source(sid)['feed_token']}.xml").status_code == 200


async def test_playlist_serial_and_opml_enabled_sources(services, source_factory, sample_media):
    sid = await source_factory("Learning Series", feed_order="playlist_serial", max_feed_items=2)
    disabled = await source_factory("Removed from OPML")
    services.source_command(disabled, "pause")
    for vid in (VIDEO, OTHER):
        services.add_video(sid, vid)
        await download_one(services, sample_media)
    with services.db.transaction() as conn:
        conn.execute("UPDATE source_entries SET sort_key=3 WHERE video_id=?", (VIDEO,))
        conn.execute("UPDATE source_entries SET sort_key=2 WHERE video_id=?", (OTHER,))
    token = services.source(sid)["feed_token"]
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        feed = ET.fromstring(client.get(f"/feeds/{token}.xml").content)
        ns = {"i": ITUNES}
        assert feed.findtext("channel/i:type", namespaces=ns) == "serial"
        assert [item.findtext("i:episode", namespaces=ns) for item in feed.findall(".//item")] == ["2", "3"]
        assert [item.findtext("guid") for item in feed.findall(".//item")] == [f"yt:video:{OTHER}", f"yt:video:{VIDEO}"]
        opml = ET.fromstring(client.get("/admin/export/opml").content)
        assert opml.attrib["version"] == "2.0"
        outlines = opml.findall(".//outline")
        assert len(outlines) == 1 and outlines[0].attrib["xmlUrl"].endswith(f"/feeds/{token}.xml")


class DummyPostProcessor:
    def __init__(self, slug, name="dummy"):
        self.name = name
        self.slug = slug

    def is_enabled_for_source(self, source_policy):
        return source_policy["postprocess"].get(self.name, False)

    async def process(self, ctx):
        await asyncio.sleep(0)
        if self.name == "broken":
            raise RuntimeError("dummy processor error")
        assert ctx.audio_path.is_file() and ctx.metadata["id"] == ctx.video_id
        filename = f"{ctx.video_id}.vtt"
        (ctx.artifacts_dir / filename).write_text("WEBVTT\n")
        return [ArtifactResult("transcript_vtt", f"{self.slug}/artifacts/{filename}", "text/vtt")]


async def test_postprocess_enqueues_publishes_and_isolates_failure(services, source_factory, sample_media):
    sid = await source_factory("日本語 字幕", postprocess={"dummy": True, "broken": True})
    source = services.source(sid)
    services.registry.register(DummyPostProcessor(source["slug"]))
    services.registry.register(DummyPostProcessor(source["slug"], "broken"))
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    with services.db.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT state FROM postprocess_tasks ORDER BY id")] == ["pending", "pending"]
    await services.worker.run_next()
    await services.worker.run_next()
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM videos").fetchone()[0] == "ready"
        assert [r[0] for r in conn.execute("SELECT state FROM postprocess_tasks ORDER BY id")] == ["done", "failed"]
    token = source["feed_token"]
    path = services.config.paths.output_dir / source["slug"] / "artifacts" / f"{VIDEO}.vtt"
    assert path.read_text() == "WEBVTT\n"
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        feed = ET.fromstring(client.get(f"/feeds/{token}.xml").content)
        transcript = feed.find(f".//{{{PODCAST}}}transcript")
        assert transcript.attrib["rel"] == "captions"
        assert transcript.attrib["type"] == "text/vtt"
        assert client.get(f"/artifacts/{token}/{VIDEO}.vtt").text == "WEBVTT\n"


async def test_scheduler_dispatches_and_honors_pause_and_daily_cap(services, source_factory, sample_media):
    sid = await source_factory("Scheduler")
    services.runner.media = sample_media
    services.config.pacing.daily_download_cap = 1
    services.add_video(sid, VIDEO)
    services.add_video(sid, OTHER)
    scheduler = Scheduler(services)
    with services.db.transaction() as conn:
        for key in ("last_retention_at", "last_reconcile_at", "last_backup_at", "last_doctor_tick"):
            set_meta(conn, key, stamp())
    services.pause(True)
    await scheduler.tick()
    assert not scheduler.tasks
    services.pause(False)
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    await scheduler.tick()
    assert not scheduler.tasks["download"]
    with services.db.connect() as conn:
        assert {row[0] for row in conn.execute("SELECT state FROM videos")} == {"pending", "ready"}


async def test_scheduler_backfill_daily_cap_does_not_block_new_uploads(services, source_factory, sample_media):
    sid = await source_factory("BackfillCap")
    services.runner.media = sample_media
    services.update_pacing({"backfill_daily_cap": 1, "backfill_min_gap_s": 0, "backfill_jitter_s": 0, "min_launch_gap_s": 0, "launch_jitter_s": 0})
    services.add_video(sid, VIDEO)
    services.add_video(sid, OTHER)
    services.add_video(sid, THIRD)
    with services.db.transaction() as conn:
        for key in ("last_retention_at", "last_reconcile_at", "last_backup_at", "last_doctor_tick"):
            set_meta(conn, key, stamp())
        conn.execute("UPDATE videos SET priority=10,published_at='2026-01-02T00:00:00+00:00' WHERE video_id=?", (VIDEO,))
        conn.execute("UPDATE videos SET priority=10,published_at='2026-01-01T00:00:00+00:00' WHERE video_id=?", (OTHER,))
        conn.execute("UPDATE videos SET priority=50,published_at='2026-02-01T00:00:00+00:00' WHERE video_id=?", (THIRD,))
    scheduler = Scheduler(services)
    # 1st tick claims high-priority new upload (THIRD); does not consume backfill cap.
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    # 2nd tick claims newest historical video (VIDEO); hits backfill_daily_cap=1.
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    # 3rd tick must NOT claim remaining historical video (OTHER) because backfill cap is reached.
    await scheduler.tick()
    assert not scheduler.tasks.get("download")
    with services.db.connect() as conn:
        assert dict(conn.execute("SELECT video_id,state FROM videos")) == {
            THIRD: "ready", VIDEO: "ready", OTHER: "pending",
        }
    # A new upload that fails once (drops to priority 30) must NOT be counted as history and MUST still retry when history cap is full.
    retry_vid = "retrynew001"
    services.add_video(sid, retry_vid)
    with services.db.transaction() as conn:
        conn.execute("UPDATE videos SET priority=30,attempt_count=1,last_category='transient_network' WHERE video_id=?", (retry_vid,))
    assert services.pacing_stats()["today_backfill"] == 1
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM videos WHERE video_id=?", (retry_vid,)).fetchone()[0] == "ready"
        assert services.pacing_stats()["today_backfill"] == 1


async def test_unattempted_queued_history_never_expires_before_first_attempt(services, source_factory, sample_media):
    sid = await source_factory("LongQueue")
    services.add_video(sid, VIDEO)
    with services.db.transaction() as conn:
        # Simulate a video that waited past 14 days in queue before ever being attempted.
        conn.execute("UPDATE videos SET priority=10,give_up_at=? WHERE video_id=?", (stamp(seconds=-86400), VIDEO))
    scheduler = Scheduler(services)
    with services.db.transaction() as conn:
        for key in ("last_retention_at", "last_reconcile_at", "last_backup_at", "last_doctor_tick"):
            set_meta(conn, key, stamp())

    async def transient_fail():
        return RunResult(error={"msg": "Connection reset by peer"}, phase="download")

    services.runner.before_fetch = transient_fail
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    with services.db.connect() as conn:
        row = conn.execute("SELECT state,priority,attempt_count,give_up_at FROM videos WHERE video_id=?", (VIDEO,)).fetchone()
        # First actual failure refreshes give_up_at to the future and keeps history priority at 10 (not promoted to 30).
        assert row["state"] == "pending"
        assert row["priority"] == 10
        assert row["attempt_count"] == 1
        assert row["give_up_at"] > stamp()


async def test_rate_limit_cooldown_preserves_budget_and_recovers(services, source_factory, sample_media):
    sid = await source_factory("Cooldown")
    services.add_video(sid, VIDEO)

    async def rate_limit():
        return RunResult(error={"http_status": 429, "msg": "Too many requests"}, phase="download")

    services.runner.before_fetch = rate_limit
    await download_one(services, sample_media)
    with services.db.transaction() as conn:
        video = conn.execute("SELECT * FROM videos WHERE video_id=?", (VIDEO,)).fetchone()
        assert video["state"] == "pending" and video["attempt_count"] == 0
        assert video["category_attempts_json"] == "{}"
        assert not services.breakers.allowed(conn, ("global",))
        for key in ("last_retention_at", "last_reconcile_at", "last_backup_at", "last_doctor_tick"):
            set_meta(conn, key, stamp())
    scheduler = Scheduler(services)
    await scheduler.tick()
    assert not scheduler.tasks
    services.runner.before_fetch = None
    with services.db.transaction() as conn:
        conn.execute("UPDATE circuit_breakers SET open_until=? WHERE key='global'", (stamp(seconds=-1),))
    await scheduler.tick()
    await asyncio.gather(*scheduler.tasks["download"])
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM circuit_breakers").fetchone()[0] == "closed"
        assert conn.execute("SELECT state FROM videos").fetchone()[0] == "ready"


@pytest.mark.parametrize("pin", [True, False])
async def test_retention_pinning(services, source_factory, sample_media, pin):
    sid = await source_factory("Retention", keep_days=1)
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    with services.db.transaction() as conn:
        conn.execute("UPDATE videos SET published_at=? WHERE video_id=?", (stamp(seconds=-2 * 86400), VIDEO))
    if pin:
        services.episode_command(sid, VIDEO, "pin")
    sweep(services.config, services.db)
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM source_entries").fetchone()[0] == ("published" if pin else "retiring")


async def test_delete_source_requeues_shared_wanted_video(services, source_factory, sample_media):
    a, b = await source_factory("Source A"), await source_factory("Source B")
    services.add_video(a, VIDEO)
    await download_one(services, sample_media)
    # B wants the same video, but link_ready has not run yet when A is deleted.
    services.add_video(b, VIDEO)
    services.delete_source(a, delete_files=True)
    with services.db.transaction() as conn:
        video = conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()
        assert video["state"] == "pending"
        claim = claim_video(conn, stamp())
        assert claim is not None and claim[1] == b


async def test_link_ready_isolates_source_failure_and_publishes_other_sources(services, source_factory, sample_media, monkeypatch):
    a, b, c = await source_factory("Origin"), await source_factory("Broken"), await source_factory("Healthy")
    services.add_video(a, VIDEO)
    await download_one(services, sample_media)
    services.add_video(b, VIDEO)
    services.add_video(c, VIDEO)
    broken_slug = services.source(b)["slug"]
    real_publish = services.downloader.publish

    def flaky_publish(conn, source, video_id, origin, *, hardlink):
        if source["slug"] == broken_slug:
            raise PermissionError("read-only folder")
        return real_publish(conn, source, video_id, origin, hardlink=hardlink)

    monkeypatch.setattr(services.downloader, "publish", flaky_publish)
    services.downloader.link_ready()
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM source_entries WHERE source_id=? AND video_id=?", (c, VIDEO)).fetchone()[0] == "published"
        assert conn.execute("SELECT last_error_category FROM sources WHERE id=?", (b,)).fetchone()[0] == "local_resource"


async def test_cookie_source_discovery_reactivates_auth_required_video(services, source_factory):
    from conftest import COOKIES

    anon, authed = await source_factory("Anon"), await source_factory("Authed")
    cid = services.save_cookie("Login", COOKIES)
    services.update_source(authed, services.config.defaults, cid, 60)
    services.add_video(anon, VIDEO)
    with services.db.transaction() as conn:
        conn.execute("UPDATE sources SET last_ok_at=? WHERE id=?", (stamp(), authed))
        conn.execute(
            "UPDATE videos SET state='unavailable',last_category='auth_required',next_attempt_at=NULL WHERE video_id=?",
            (VIDEO,),
        )
        authed_row = conn.execute("SELECT * FROM sources WHERE id=?", (authed,)).fetchone()
        list_entries(conn, services.config, authed_row, [{"id": VIDEO, "title": "Members"}], has_active_cookie=True)
        claim = claim_video(conn, stamp())
        assert claim is not None and claim[1] == authed


async def test_validate_cookie_rejects_non_auth_cookie_file(services):
    non_auth = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tPREF\tf6=40000000\n"
    cid = services.save_cookie("NoLogin", non_auth)
    assert not await services.validate_cookie(cid)
    with services.db.connect() as conn:
        assert conn.execute("SELECT status FROM cookie_profiles WHERE id=?", (cid,)).fetchone()[0] == "expired"


async def test_postprocess_rejects_unprefixed_artifact_name(services, source_factory, sample_media):
    class CollidingProcessor:
        name = "colliding"

        def is_enabled_for_source(self, source_policy):
            return True

        async def process(self, ctx):
            (ctx.artifacts_dir / "shared.vtt").write_text("WEBVTT\n")
            return [ArtifactResult("transcript_vtt", f"{slug}/artifacts/shared.vtt", "text/vtt")]

    sid = await source_factory("Artifacts")
    slug = services.source(sid)["slug"]
    services.registry.register(CollidingProcessor())
    services.add_video(sid, VIDEO)
    await download_one(services, sample_media)
    await services.worker.run_next()
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM postprocess_tasks").fetchone()[0] == "failed"
        assert conn.execute("SELECT count(*) FROM video_artifacts").fetchone()[0] == 0
