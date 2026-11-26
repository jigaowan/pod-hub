from __future__ import annotations

import asyncio
import shutil
import xml.etree.ElementTree as ET
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from podhub.core.discovery import list_entries
from podhub.core.retention import reconcile, sweep
from podhub.core.scheduler import Scheduler
from podhub.db.connection import set_meta
from podhub.db.repositories import claim_video, start_attempt
from podhub.domain.models import stamp, utc_now
from podhub.main import create_app
from podhub.outputs.rss import ITUNES, PODCAST
from podhub.postprocess.base import ArtifactResult
from podhub.ytdlp.runner import RunResult

from conftest import OTHER, VIDEO


async def download_one(services, media):
    services.runner.media = media
    with services.db.transaction() as conn:
        claim = claim_video(conn, stamp())
        assert claim
        video, sid = claim
        attempt = start_attempt(conn, "download", sid, video["video_id"])
    await services.downloader.execute(video, sid, attempt)


async def test_atomic_publish_hardlink_deletion_and_reconciliation(services, source_factory, sample_media):
    a, b = await source_factory("Shared Café"), await source_factory("Shared Cafe")
    root = services.config.paths.output_dir
    services.add_video(a, VIDEO)
    await download_one(services, sample_media)
    original = root / "shared-cafe" / f"{VIDEO}.m4a"
    assert original.read_bytes() == sample_media.read_bytes()
    assert list(original.parent.iterdir()) == [original]

    services.add_video(b, VIDEO)
    services.downloader.link_ready()
    sibling = root / "shared-cafe-2" / f"{VIDEO}.m4a"
    assert original.samefile(sibling)
    assert [mode for mode, _ in services.runner.calls].count("fetch") == 1
    sibling.unlink()
    orphan = root / "shared-cafe" / f"{OTHER}.m4a"
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
        conn.execute("UPDATE videos SET published_at=? WHERE video_id=?", (stamp(), OTHER))
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
        item = root.find(f".//item[guid='yt:video:{VIDEO}']")
        assert item.find("guid").attrib == {"isPermaLink": "false"}
        assert "\x01" not in item.findtext("title") and "]]>" in item.findtext("title")
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
        assert not (services.config.paths.output_dir / row["slug"] / f"{VIDEO}.m4a").exists()
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
    sid = await source_factory("Queue", postprocess={"dummy": True, "broken": True})
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
