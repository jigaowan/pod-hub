from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from podhub.cli import main
from podhub.core.services import Services
from podhub.db.repositories import claim_video
from podhub.domain.models import stamp
from podhub.errors.taxonomy import classify
from podhub.main import create_app
from podhub.ytdlp.runner import RunResult

from conftest import CHANNEL, COOKIES, OTHER, THIRD, VIDEO


def csrf_headers(client: TestClient) -> dict[str, str]:
    page = client.get("/admin")
    token = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)[1]
    return {"X-CSRF-Token": token}


def test_admin_source_preview_confirm_and_actions(services):
    services.runner.entries = [
        {"id": VIDEO, "title": "Included long episode", "duration": 100},
        {"id": OTHER, "title": "Short episode", "duration": 2},
    ]
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        overview_html = client.get("/admin").text
        assert "Recent attempts" in overview_html
        assert 'class="version"' in overview_html
        csrf = {"X-CSRF-Token": re.search(r'<meta name="csrf-token" content="([^"]+)"', overview_html)[1]}
        assert "overview-panel" in client.get("/admin/status").text
        assert client.get("/healthz").text == "ok\n"
        assert "2.0.8" in client.get("/static/htmx.min.js").text
        assert client.get("/admin/sources").status_code == 200
        response = client.post("/admin/sources/preview", data={
            "source_input": "@example", "min_duration_s": "60",
        }, headers={"HX-Request": "true", **csrf})
        assert response.status_code == 200
        assert CHANNEL in response.text and "Included long episode" in response.text
        assert "Filtered · Below minimum duration" in response.text
        assert 'name="title" value="Example Channel"' in response.text
        assert 'name="slug" value="Example Channel"' in response.text
        ticket = re.search(r'name="ticket" value="([^"]+)"', response.text)[1]
        assert client.post("/admin/sources", data={"ticket": f"{ticket}a"}, headers=csrf).status_code == 400
        confirm = client.post("/admin/sources", data={"ticket": ticket, "fetch_history": "on"}, headers=csrf, follow_redirects=False)
        assert confirm.status_code == 303
        detail = confirm.headers["location"]
        panel = client.get(detail)
        assert panel.status_code == 200
        assert "Keep files on disk" in panel.text and "Delete source folder on disk" in panel.text
        sid = int(detail.rsplit("/", 1)[1])
        assert services.source(sid)["external_id"] == CHANNEL
        previous = services.source(sid)["feed_token"]
        assert client.post(detail, data={"action": "rotate"}, headers=csrf).status_code == 200
        assert services.source(sid)["feed_token"] != previous
        assert client.post(detail, data={"action": "sync"}, headers=csrf).status_code == 200
        assert client.post(detail, data={"action": "pause"}, headers=csrf).status_code == 200
        assert services.source(sid)["enabled"] == 0
        assert client.post(detail, data={"action": "resume"}, headers=csrf).status_code == 200
        assert client.post(detail, data={"action": "add-video", "video": f"https://youtu.be/{VIDEO}"}, headers=csrf).status_code == 200
        assert client.post(f"{detail}/episodes/{VIDEO}", data={"action": "pin"}, headers=csrf).status_code == 200
        assert client.post(detail, data={"action": "policy", "keep_count": 10}, headers=csrf).status_code == 200
        assert services.source(sid)["policy_rev"] == 2
        assert client.post(detail, data={"action": "delete"}, headers=csrf).status_code == 400
        # CSRF blocks missing/wrong token, cross-site, and same-site, while allowing valid token on any temporary proxy/IP Origin.
        assert client.post("/admin/system", data={"action": "pause"}).status_code == 403
        assert client.post("/admin/system", data={"action": "pause"}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post("/admin/system", data={"action": "pause"}, headers={"Sec-Fetch-Site": "cross-site", **csrf}).status_code == 403
        assert client.post("/admin/system", data={"action": "pause"}, headers={"Sec-Fetch-Site": "same-site", **csrf}).status_code == 403
        assert client.post("/admin/system", data={"action": "pause"}, headers={
            "Sec-Fetch-Site": "same-origin", "Origin": "https://temp-proxy.example", **csrf,
        }).status_code == 200
        assert client.post("/admin/system", data={"action": "resume", "csrf_token": csrf["X-CSRF-Token"]}, headers={
            "Origin": "http://192.168.1.10:9000",
        }).status_code == 200
        assert "Paused" in client.post("/admin/system", data={"action": "pause"}, headers=csrf).text
        assert "Running" in client.post("/admin/system", data={"action": "resume"}, headers=csrf).text
        assert "ffmpeg test" in client.post("/admin/system", data={"action": "doctor"}, headers=csrf).text
        assert client.post(detail, data={"action": "delete", "files": "keep"}, headers=csrf).status_code == 200
        assert client.get(detail).status_code == 404


def test_admin_slug_override_rejects_invalid_and_conflicting_folders(services):
    root = services.config.paths.output_dir
    (root / "example channel").mkdir()
    (root / "Cafe\u0301 音楽").mkdir()
    (root / "dangling").symlink_to(root / "missing")
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        csrf = csrf_headers(client)
        preview = client.post("/admin/sources/preview", data={"source_input": "@example"}, headers=csrf)
        assert 'name="slug" value="Example Channel-2"' in preview.text
        ticket = re.search(r'name="ticket" value="([^"]+)"', preview.text)[1]
        form = {"ticket": ticket}
        for slug in ("../escape", ".hidden", "a" * 121, "番" * 41, "Example Channel", "Café 音楽", "dangling", "NUL"):
            assert client.post("/admin/sources", data={**form, "slug": slug}, headers=csrf).status_code == 400
        assert not services.sources()
        confirmed = client.post("/admin/sources", data={
            **form, "title": " 深夜ラジオ ", "slug": "  か\u3099く　档案 ",
        }, headers=csrf, follow_redirects=False)
        assert confirmed.status_code == 303
        source = services.sources()[0]
        assert (source["title"], source["slug"]) == ("深夜ラジオ", "がく 档案")


def test_admin_title_controls_default_folder_and_can_be_renamed(services):
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        csrf = csrf_headers(client)
        preview = client.post("/admin/sources/preview", data={"source_input": "初期設定", "kind": "manual"}, headers=csrf)
        assert 'name="title" value="初期設定"' in preview.text
        assert 'name="slug" value="初期設定"' in preview.text
        ticket = re.search(r'name="ticket" value="([^"]+)"', preview.text)[1]
        form = {"ticket": ticket, "slug": ""}
        assert client.post("/admin/sources", data={**form, "title": "  "}, headers=csrf).status_code == 400
        confirmed = client.post("/admin/sources", data={**form, "title": "今日：中文节目"}, headers=csrf, follow_redirects=False)
        assert confirmed.status_code == 303
        source = services.sources()[0]
        assert source["slug"] == "今日：中文节目"
        folder = services.config.paths.output_dir / source["slug"]
        folder.mkdir()
        (folder / "keep").touch()
        detail = confirmed.headers["location"]
        feed_url = f"/feeds/{source['feed_token']}.xml"
        feed = client.get(feed_url)
        assert ET.fromstring(feed.content).findtext("channel/title") == "今日：中文节目"
        assert 'name="title" value="今日：中文节目"' in client.get(detail).text
        assert client.post(detail, data={"action": "rename", "title": "   "}, headers=csrf).status_code == 400
        updated = client.post(detail, data={"action": "rename", "title": " 新しい名前 "}, headers=csrf)
        assert updated.status_code == 200 and "新しい名前" in updated.text
        updated_source = services.source(source["id"])
        assert updated_source["slug"] == source["slug"]
        assert updated_source["feed_token"] == source["feed_token"]
        assert updated_source["policy_rev"] == source["policy_rev"]
        assert (folder / "keep").is_file()
        renamed_feed = client.get(feed_url, headers={"If-None-Match": feed.headers["etag"]})
        assert renamed_feed.status_code == 200
        assert ET.fromstring(renamed_feed.content).findtext("channel/title") == "新しい名前"


async def test_admin_queue_status_bulk_retry_and_video_timeline(services, source_factory):
    a = await source_factory()
    services.add_video(a, VIDEO)
    services.add_video(a, OTHER)
    with services.db.transaction() as conn:
        conn.execute("UPDATE videos SET state='failed',last_category='unclassified',unclassified=1 WHERE video_id=?", (VIDEO,))
        conn.execute("UPDATE videos SET state='failed',last_category='corrupted_artifact' WHERE video_id=?", (OTHER,))
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        csrf = csrf_headers(client)
        for tab in ("active", "deferred", "failed", "unclassified", "unavailable", "ready"):
            assert client.get(f"/admin/videos?status={tab}").status_code == 200
        found = client.get("/admin/videos?status=unclassified").text
        assert VIDEO in found and OTHER not in found
        assert "Attempt timeline" in client.get(f"/admin/videos/{VIDEO}").text
        assert client.post("/admin/videos", data={"action": "pacing", "max_concurrent_downloads": "2", "min_launch_gap_s": "10", "launch_jitter_s": "15", "backfill_min_gap_s": "45", "backfill_jitter_s": "30", "backfill_daily_cap": "12", "daily_download_cap": "50"}, headers=csrf).status_code == 200
        assert services.effective_pacing().backfill_daily_cap == 12
        assert services.effective_pacing().max_concurrent_downloads == 2
        assert "&#34;backfill_daily_cap&#34;: 12" in client.get("/admin/cookies").text
        assert client.post("/admin/videos", data={"action": "reset-pacing"}, headers=csrf).status_code == 200
        assert services.effective_pacing().backfill_daily_cap == services.config.pacing.backfill_daily_cap
        assert client.post("/admin/videos", data={"action": "retry", "category": "unclassified"}, headers=csrf).status_code == 200
        with services.db.connect() as conn:
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "pending"
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (OTHER,)).fetchone()[0] == "failed"


async def test_cookies_create_update_expire_validate_and_delete(services, source_factory):
    services.config.notify.webhook_url = ""
    bound = await source_factory("Bound source")
    unbound = await source_factory("Unbound source")
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        csrf = csrf_headers(client)
        create = client.post("/admin/cookies", data={"action": "save", "name": "Login", "contents": COOKIES}, headers=csrf)
        assert create.status_code == 200
        assert "sample-cookie-secret" not in create.text
        assert "Effective configuration" in create.text
        row = services.cookies()[0]
        cookie_id = row["id"]
        services.update_source(bound, services.config.defaults, cookie_id, 1800)
        for sid, vid in ((bound, VIDEO), (bound, OTHER), (unbound, THIRD)):
            services.add_video(sid, vid)
        with services.db.transaction() as conn:
            conn.execute("UPDATE videos SET state='unavailable',last_category='auth_required',give_up_at=?", (stamp(seconds=-86400),))
            conn.execute("UPDATE source_entries SET state='candidate' WHERE video_id=?", (OTHER,))
            invalid = classify(RunResult(warnings=["cookies are no longer valid"]), used_cookie=True)
            services.breakers.trip(conn, f"cookie:{cookie_id}", invalid)
        assert services.cookies()[0]["status"] == "expired"
        assert client.post("/admin/cookies", data={
            "action": "save", "id": cookie_id, "name": "New login", "contents": COOKIES,
        }, headers=csrf).status_code == 200
        with services.db.connect() as conn:
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "unavailable"
        response = client.post("/admin/cookies", data={"action": "validate", "id": cookie_id, "video": VIDEO}, headers=csrf)
        assert response.status_code == 200 and "Cookie validated." in response.text
        assert services.cookies()[0]["status"] == "active"
        assert services.list_breakers()[-1]["state"] == "closed"
        with services.db.connect() as conn:
            assert dict(conn.execute("SELECT video_id,state FROM videos")) == {
                VIDEO: "pending", OTHER: "unavailable", THIRD: "unavailable",
            }
        assert client.post("/admin/cookies", data={"action": "delete", "id": cookie_id}, headers=csrf).status_code == 200
        assert not services.cookies()
        assert not (services.config.paths.state_dir / "cookies" / f"{cookie_id}.txt").exists()


async def test_binding_active_cookie_requeues_only_auth_blocked_wanted_videos(services, source_factory):
    sid = await source_factory()
    active = services.save_cookie("Active", COOKIES)
    expired = services.save_cookie("Expired", COOKIES)
    for vid in (VIDEO, OTHER):
        services.add_video(sid, vid)
    with services.db.transaction() as conn:
        conn.execute(
            "UPDATE videos SET state='unavailable',last_category='auth_required',give_up_at=?,attempt_count=2",
            (stamp(seconds=-86400),),
        )
        conn.execute("UPDATE videos SET last_category='soft_unavailable' WHERE video_id=?", (OTHER,))
        services.breakers.trip(conn, f"cookie:{expired}", classify(
            RunResult(warnings=["cookies are no longer valid"]), used_cookie=True,
        ))
    services.update_source(sid, services.config.defaults, expired, 1800)
    with services.db.transaction() as conn:
        assert claim_video(conn, stamp()) is None
    policy_rev = services.source(sid)["policy_rev"]
    services.update_source(sid, services.config.defaults, active, 1800)
    assert services.source(sid)["policy_rev"] == policy_rev
    with services.db.transaction() as conn:
        row = conn.execute("SELECT * FROM videos WHERE video_id=?", (VIDEO,)).fetchone()
        assert (row["state"], row["priority"], row["attempt_count"], row["next_attempt_at"]) == ("pending", 50, 0, None)
        assert row["give_up_at"] > stamp()
        assert claim_video(conn, stamp())[1] == sid
        assert conn.execute("SELECT state FROM videos WHERE video_id=?", (OTHER,)).fetchone()[0] == "unavailable"


def test_cli_uses_shared_services(config, tmp_path, capsys):
    path = tmp_path / "service.toml"
    path.write_text(
        f'[paths]\nstate_dir="{config.paths.state_dir}"\noutput_dir="{config.paths.output_dir}"\n',
    )
    flags = ["--config", str(path)]
    assert main([
        *flags, "sources", "add", "Listen Later", "--kind", "manual", "--title", "中文电台", "--slug", "がく 再生",
    ]) == 0
    sid = capsys.readouterr().out.strip()
    assert main([*flags, "sources", "add", "Other source", "--kind", "manual", "--slug", "か\u3099く 再生"]) == 1
    assert main([*flags, "sources", "add-video", sid, "--", VIDEO]) == 0
    assert main([*flags, "pause"]) == 0
    service = Services(config)
    source = service.source(int(sid))
    assert (source["title"], source["slug"]) == ("中文电台", "がく 再生")
    with service.db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='global_paused'").fetchone()[0] == "1"
        assert conn.execute("SELECT state FROM source_entries").fetchone()[0] == "wanted"


def test_app_version_uses_build_override(monkeypatch, services):
    from podhub.version import app_version
    app_version.cache_clear()
    monkeypatch.setenv("PODHUB_BUILD_VERSION", "v2.0.2+abc1234")
    try:
        assert app_version() == "v2.0.2+abc1234"
        with TestClient(create_app(services=services, run_scheduler=False)) as client:
            assert "v2.0.2+abc1234" in client.get("/admin").text
    finally:
        app_version.cache_clear()
