from __future__ import annotations

import re

from fastapi.testclient import TestClient

from podhub.cli import main
from podhub.core.services import Services
from podhub.errors.taxonomy import classify
from podhub.main import create_app
from podhub.ytdlp.runner import RunResult

from conftest import CHANNEL, COOKIES, OTHER, VIDEO


def test_admin_source_preview_confirm_and_actions(services):
    services.runner.entries = [
        {"id": VIDEO, "title": "Included long episode", "duration": 100},
        {"id": OTHER, "title": "Short episode", "duration": 2},
    ]
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        assert "Recent attempts" in client.get("/admin").text
        assert "overview-panel" in client.get("/admin/status").text
        assert client.get("/healthz").text == "ok\n"
        assert "2.0.8" in client.get("/static/htmx.min.js").text
        assert client.get("/admin/sources").status_code == 200
        response = client.post("/admin/sources/preview", data={
            "source_input": "@example", "min_duration_s": "60",
        }, headers={"HX-Request": "true"})
        assert response.status_code == 200
        assert CHANNEL in response.text and "Included long episode" in response.text
        assert "Filtered · Below minimum duration" in response.text
        ticket = re.search(r'name="ticket" value="([^"]+)"', response.text)[1]
        assert client.post("/admin/sources", data={"ticket": f"{ticket}a", "initial_backfill_count": 1}).status_code == 400
        confirm = client.post("/admin/sources", data={"ticket": ticket, "initial_backfill_count": 1}, follow_redirects=False)
        assert confirm.status_code == 303
        detail = confirm.headers["location"]
        panel = client.get(detail)
        assert panel.status_code == 200
        assert "Keep files on disk" in panel.text and "Delete source folder on disk" in panel.text
        sid = int(detail.rsplit("/", 1)[1])
        assert services.source(sid)["external_id"] == CHANNEL
        previous = services.source(sid)["feed_token"]
        assert client.post(detail, data={"action": "rotate"}).status_code == 200
        assert services.source(sid)["feed_token"] != previous
        assert client.post(detail, data={"action": "sync"}).status_code == 200
        assert client.post(detail, data={"action": "pause"}).status_code == 200
        assert services.source(sid)["enabled"] == 0
        assert client.post(detail, data={"action": "resume"}).status_code == 200
        assert client.post(detail, data={"action": "add-video", "video": f"https://youtu.be/{VIDEO}"}).status_code == 200
        assert client.post(f"{detail}/episodes/{VIDEO}", data={"action": "pin"}).status_code == 200
        assert client.post(detail, data={"action": "policy", "check_interval": 60, "keep_count": 10}).status_code == 200
        assert services.source(sid)["policy_rev"] == 2
        assert client.post(detail, data={"action": "delete"}).status_code == 400
        assert client.post("/admin/system", data={"action": "pause"}, headers={"Origin": "https://elsewhere.example"}).status_code == 403
        assert "Paused" in client.post("/admin/system", data={"action": "pause"}).text
        assert "Running" in client.post("/admin/system", data={"action": "resume"}).text
        assert "ffmpeg test" in client.post("/admin/system", data={"action": "doctor"}).text
        assert client.post(detail, data={"action": "delete", "files": "keep"}).status_code == 200
        assert client.get(detail).status_code == 404


async def test_admin_queue_status_bulk_retry_and_video_timeline(services, source_factory):
    a = await source_factory()
    services.add_video(a, VIDEO)
    services.add_video(a, OTHER)
    with services.db.transaction() as conn:
        conn.execute("UPDATE videos SET state='failed',last_category='unclassified',unclassified=1 WHERE video_id=?", (VIDEO,))
        conn.execute("UPDATE videos SET state='failed',last_category='corrupted_artifact' WHERE video_id=?", (OTHER,))
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        for tab in ("active", "deferred", "failed", "unclassified", "unavailable", "ready"):
            assert client.get(f"/admin/videos?status={tab}").status_code == 200
        found = client.get("/admin/videos?status=unclassified").text
        assert VIDEO in found and OTHER not in found
        assert "Attempt timeline" in client.get(f"/admin/videos/{VIDEO}").text
        assert client.post("/admin/videos", data={"category": "unclassified"}).status_code == 200
        with services.db.connect() as conn:
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "pending"
            assert conn.execute("SELECT state FROM videos WHERE video_id=?", (OTHER,)).fetchone()[0] == "failed"


def test_cookies_create_update_expire_validate_and_delete(services):
    services.config.notify.webhook_url = ""
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        create = client.post("/admin/cookies", data={"action": "save", "name": "Login", "contents": COOKIES})
        assert create.status_code == 200
        assert "sample-cookie-secret" not in create.text
        assert "Effective configuration" in create.text
        row = services.cookies()[0]
        cookie_id = row["id"]
        with services.db.transaction() as conn:
            invalid = classify(RunResult(warnings=["cookies are no longer valid"]), used_cookie=True)
            services.breakers.trip(conn, f"cookie:{cookie_id}", invalid)
        assert services.cookies()[0]["status"] == "expired"
        assert client.post("/admin/cookies", data={
            "action": "save", "id": cookie_id, "name": "New login", "contents": COOKIES,
        }).status_code == 200
        response = client.post("/admin/cookies", data={"action": "validate", "id": cookie_id, "video": VIDEO})
        assert response.status_code == 200 and "Cookie validated." in response.text
        assert services.cookies()[0]["status"] == "active"
        assert services.list_breakers()[-1]["state"] == "closed"
        assert client.post("/admin/cookies", data={"action": "delete", "id": cookie_id}).status_code == 200
        assert not services.cookies()
        assert not (services.config.paths.state_dir / "cookies" / f"{cookie_id}.txt").exists()


def test_cli_uses_shared_services(config, tmp_path, capsys):
    path = tmp_path / "service.toml"
    path.write_text(
        f'[paths]\nstate_dir="{config.paths.state_dir}"\noutput_dir="{config.paths.output_dir}"\n',
    )
    flags = ["--config", str(path)]
    assert main([*flags, "sources", "add", "Listen Later", "--kind", "manual"]) == 0
    sid = capsys.readouterr().out.strip()
    assert main([*flags, "sources", "add-video", sid, "--", VIDEO]) == 0
    assert main([*flags, "pause"]) == 0
    service = Services(config)
    with service.db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='global_paused'").fetchone()[0] == "1"
        assert conn.execute("SELECT state FROM source_entries").fetchone()[0] == "wanted"
