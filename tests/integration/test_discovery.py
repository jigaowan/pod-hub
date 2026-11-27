from __future__ import annotations

import httpx

from podhub.core.discovery import list_entries, normalize_source
from podhub.db.connection import get_meta, set_meta
from podhub.db.repositories import start_attempt
from podhub.domain.enums import SourceKind
from podhub.domain.models import SourceIdentity, stamp
from podhub.domain.policy import SourcePolicy
from podhub.ytdlp.rss import AtomResult, fetch_atom

from conftest import CHANNEL, OTHER, THIRD, VIDEO


async def test_atom_conditionals_and_exact_timestamp():
    xml = f'''<feed xmlns="http://www.w3.org/2005/Atom"
       xmlns:yt="http://www.youtube.com/xml/schemas/2015"
       xmlns:media="http://search.yahoo.com/mrss/">
      <entry><yt:videoId>{VIDEO}</yt:videoId><title>Title</title><published>2026-01-02T03:04:05+00:00</published>
      <media:group><media:thumbnail url="https://i.ytimg.com/test.jpg"/></media:group></entry>
    </feed>'''

    def handler(request):
        assert request.url.params["playlist_id"] == "UULF" + CHANNEL[2:]
        if request.headers.get("If-None-Match"):
            assert request.headers["If-None-Match"] == '"hash"'
            assert request.headers["If-Modified-Since"] == "Mon, 02 Jan 2006 15:04:05 GMT"
            return httpx.Response(304)
        return httpx.Response(200, content=xml, headers={"etag": '"hash"', "last-modified": "Mon, 02 Jan 2006 15:04:05 GMT"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await fetch_atom(client, CHANNEL, long_form=True)
        second = await fetch_atom(client, CHANNEL, long_form=True, etag=first.etag, last_modified=first.last_modified)
    assert first.entries[0]["timestamp"] == "2026-01-02T03:04:05+00:00"
    assert first.entries[0]["id"] == VIDEO
    assert first.entries[0]["thumbnail"].endswith("test.jpg")
    assert second.not_modified and second.entries == []


async def test_discovery_new_only_default_and_fetch_history_toggle(services):
    url = "https://www.youtube.com/playlist?list=PLplaylist"
    assert normalize_source(url) == (SourceKind.PLAYLIST, url)
    identity = SourceIdentity("playlist", "PLplaylist", "Playlist", url)
    policy = SourcePolicy(min_duration_s=60, fetch_history=False)
    sid = await services.add_source(identity, url, policy)
    services.runner.entries = [
        {"id": VIDEO, "timestamp": "2026-04-11", "duration": 85, "sort_key": 1},
        {"id": OTHER, "timestamp": "2026-04-10", "sort_key": 2},
        {"id": THIRD, "timestamp": "2026-04-09", "duration": 7, "sort_key": 3},
    ]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        assert dict(conn.execute("SELECT video_id,state FROM source_entries")) == {
            VIDEO: "candidate", OTHER: "candidate", THIRD: "filtered",
        }
    # New uploads after initial baseline are automatically wanted with priority 50.
    services.runner.entries = [
        *services.runner.entries[:2],
        {"id": "tailfeed001", "timestamp": "2026-04-12", "duration": 90, "sort_key": 3},
    ]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        assert conn.execute(
            "SELECT e.state,v.priority FROM source_entries e JOIN videos v USING(video_id) WHERE v.video_id='tailfeed001'",
        ).fetchone()[:] == ("wanted", 50)
        assert conn.execute("SELECT state FROM source_entries WHERE video_id=?", (THIRD,)).fetchone()[0] == "filtered"
    # Turning fetch_history ON promotes eligible candidates & newly un-filtered items and triggers deep history scan.
    services.update_source(sid, SourcePolicy(min_duration_s=0, fetch_history=True), None)
    with services.db.connect() as conn:
        assert get_meta(conn, f"history_scan:{sid}") == "1"
        states = {row[0]: row[1:] for row in conn.execute(
            "SELECT e.video_id,e.state,v.priority FROM source_entries e JOIN videos v USING(video_id) WHERE e.source_id=?",
            (sid,),
        )}
        assert states[VIDEO] == states[OTHER] == states[THIRD] == ("wanted", 10)
        assert states["tailfeed001"] == ("wanted", 50)
    # Turning fetch_history OFF reverts unstarted historical items back to candidate, keeping new uploads wanted.
    services.update_source(sid, SourcePolicy(min_duration_s=0, fetch_history=False), None)
    with services.db.connect() as conn:
        assert get_meta(conn, f"history_scan:{sid}") == ""
        assert dict(conn.execute("SELECT video_id,state FROM source_entries WHERE source_id=?", (sid,))) == {
            VIDEO: "candidate", OTHER: "candidate", THIRD: "candidate", "tailfeed001": "wanted",
        }


async def test_incremental_head_scan_stops_after_consecutive_known(services):
    identity = SourceIdentity("playlist", "PLhead", "Head", "https://www.youtube.com/playlist?list=PLhead")
    policy = SourcePolicy(playlist_scan_mode="head_early_stop", early_stop_known=2)
    sid = await services.add_source(identity, identity.normalized_url, policy)
    source = services.source(sid)
    with services.db.transaction() as conn:
        list_entries(conn, services.config, source, [{"id": VIDEO}, {"id": OTHER}])
        conn.execute("UPDATE sources SET last_ok_at=? WHERE id=?", (stamp(), sid))
        attempt = start_attempt(conn, "discovery", sid)
    services.runner.entries = [{"id": VIDEO}, {"id": THIRD}, {"id": VIDEO}, {"id": OTHER}, {"id": "tailfeed001"}]
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        ids = {row[0] for row in conn.execute("SELECT video_id FROM source_entries")}
        assert THIRD in ids and "tailfeed001" not in ids


async def test_channel_atom_failure_uses_bounded_listing_and_keeps_exact_date(services, monkeypatch):
    url = f"https://www.youtube.com/channel/{CHANNEL}"
    identity = SourceIdentity("channel", CHANNEL, "Channel", url)
    sid = await services.add_source(identity, url, SourcePolicy())

    async def failed_atom(*args, **kwargs):
        raise httpx.ReadTimeout("no Atom response")

    monkeypatch.setattr(services.discovery, "atom", failed_atom)
    exact = "2026-02-03T04:05:06+00:00"
    with services.db.transaction() as conn:
        list_entries(conn, services.config, services.source(sid), [{"id": VIDEO, "timestamp": exact}])
        conn.execute("UPDATE sources SET last_ok_at=? WHERE id=?", (stamp(), sid))
        attempt = start_attempt(conn, "discovery", sid)
    services.runner.entries = [{"id": VIDEO, "timestamp": "2026-02-03"}, {"id": OTHER}]
    await services.discovery.sync(services.source(sid), attempt)
    mode, request = services.runner.calls[-1]
    assert mode == "list" and request["limit"] == 30
    with services.db.connect() as conn:
        assert conn.execute("SELECT published_at FROM videos WHERE video_id=?", (VIDEO,)).fetchone()[0] == "2026-02-03T04:05:06.000000+00:00"
        assert conn.execute("SELECT warnings_json FROM attempts WHERE id=?", (attempt,)).fetchone()[0]


async def test_channel_without_history_uses_normal_atom_discovery(services, monkeypatch):
    url = f"https://www.youtube.com/channel/{CHANNEL}"
    sid = await services.add_source(SourceIdentity("channel", CHANNEL, "Channel", url), url, SourcePolicy())

    async def atom(source, policy):
        return AtomResult([{"id": OTHER, "timestamp": "2026-02-04"}], None, None), "channel"

    monkeypatch.setattr(services.discovery, "atom", atom)
    with services.db.transaction() as conn:
        conn.execute("UPDATE sources SET last_ok_at=? WHERE id=?", (stamp(), sid))
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    assert not services.runner.calls
    with services.db.connect() as conn:
        assert conn.execute(
            "SELECT e.state,v.priority FROM source_entries e JOIN videos v USING(video_id) WHERE v.video_id=?",
            (OTHER,),
        ).fetchone()[:] == ("wanted", 50)
        assert get_meta(conn, f"history_scan:{sid}") == ""


async def test_fetch_history_enqueues_all_history_and_keeps_new_uploads_high_priority(services):
    url = f"https://www.youtube.com/channel/{CHANNEL}"
    sid = await services.add_source(SourceIdentity("channel", CHANNEL, "Channel", url), url, SourcePolicy(fetch_history=False))
    with services.db.transaction() as conn:
        list_entries(conn, services.config, services.source(sid), [
            {"id": VIDEO, "timestamp": "2026-02-03"},
            {"id": OTHER, "timestamp": "2026-02-02"},
        ])
        conn.execute("UPDATE sources SET last_ok_at=?,next_check_at=? WHERE id=?", (stamp(), stamp(seconds=3600), sid))
        attempt = start_attempt(conn, "discovery", sid)

    services.update_source(sid, SourcePolicy(fetch_history=True), None)
    assert services.source(sid)["next_check_at"] <= stamp()
    with services.db.connect() as conn:
        assert get_meta(conn, f"history_scan:{sid}") == "1"
    services.runner.entries = [
        {"id": "newvideo001", "timestamp": "2026-02-05"},
        {"id": "newvideo002", "upload_date": "20260204"},
        {"id": VIDEO, "timestamp": "2026-02-03"},
        {"id": "oldvideo001", "timestamp": "2026-02-01"},
        {"id": "oldvideo002", "timestamp": "2026-01-01"},
    ]
    await services.discovery.sync(services.source(sid), attempt)
    assert [mode for mode, _ in services.runner.calls] == ["list"]
    with services.db.connect() as conn:
        states = {row[0]: row[1:] for row in conn.execute(
            "SELECT e.video_id,e.state,v.priority FROM source_entries e JOIN videos v USING(video_id) WHERE e.source_id=?",
            (sid,),
        )}
        assert states["newvideo001"] == states["newvideo002"] == ("wanted", 50)
        assert states[VIDEO] == states[OTHER] == states["oldvideo001"] == states["oldvideo002"] == ("wanted", 10)
        assert get_meta(conn, f"history_scan:{sid}") == ""


async def test_fetch_history_paginates_when_channel_exceeds_scan_limit(services):
    url = f"https://www.youtube.com/channel/{CHANNEL}"
    sid = await services.add_source(
        SourceIdentity("channel", CHANNEL, "Channel", url), url,
        SourcePolicy(fetch_history=True, max_scan_items=2),
    )
    services.runner.entries = [
        {"id": VIDEO, "sort_key": 1},
        {"id": OTHER, "sort_key": 2},
    ]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        assert get_meta(conn, f"history_scan:{sid}") == "1"
        assert get_meta(conn, f"history_cursor:{sid}") == "3"
    # Second page returns fewer than max_scan_items -> history scan finishes.
    services.runner.entries = [{"id": THIRD, "sort_key": 3}]
    with services.db.transaction() as conn:
        attempt2 = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt2)
    assert services.runner.calls[-1][1]["start"] == 3
    with services.db.connect() as conn:
        assert get_meta(conn, f"history_scan:{sid}") == ""
        assert get_meta(conn, f"history_cursor:{sid}") == ""
        assert dict(conn.execute("SELECT video_id,state FROM source_entries WHERE source_id=?", (sid,))) == {
            VIDEO: "wanted", OTHER: "wanted", THIRD: "wanted",
        }


async def test_fetch_history_off_uses_entry_origin_preserves_admin_skip_and_clears_deadlines(services):
    url1 = f"https://www.youtube.com/channel/{CHANNEL}"
    url2 = "https://www.youtube.com/playlist?list=PLsecondsource"
    sid1 = await services.add_source(SourceIdentity("channel", CHANNEL, "C1", url1), url1, SourcePolicy(fetch_history=True))
    sid2 = await services.add_source(SourceIdentity("playlist", "PLsecondsource", "P2", url2), url2, SourcePolicy(fetch_history=False))
    # Initial history on sid1: VIDEO and OTHER (both is_history=1).
    services.runner.entries = [{"id": VIDEO, "timestamp": "2026-01-02"}, {"id": OTHER, "timestamp": "2026-01-01"}]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid1)
    await services.discovery.sync(services.source(sid1), attempt)
    # Mark sid2 as established, then discover VIDEO as a new upload (priority 50) on sid2.
    with services.db.transaction() as conn:
        conn.execute("UPDATE sources SET last_ok_at=? WHERE id=?", (stamp(), sid2))
        list_entries(conn, services.config, conn.execute("SELECT * FROM sources WHERE id=?", (sid2,)).fetchone(), [
            {"id": VIDEO, "timestamp": "2026-01-02"},
        ])
    # New upload THIRD on sid1 fails once and drops to priority 30.
    with services.db.transaction() as conn:
        list_entries(conn, services.config, conn.execute("SELECT * FROM sources WHERE id=?", (sid1,)).fetchone(), [
            {"id": THIRD, "timestamp": "2026-02-01"},
        ])
        conn.execute("UPDATE videos SET priority=30,attempt_count=1 WHERE video_id=?", (THIRD,))
    # Admin skips OTHER on sid1; unrelated keep_count change must NOT undo the admin skip.
    services.episode_command(sid1, OTHER, "skip")
    services.update_source(sid1, SourcePolicy(fetch_history=True, keep_count=20), None)
    with services.db.connect() as conn:
        assert conn.execute("SELECT state,filter_reason FROM source_entries WHERE source_id=? AND video_id=?", (sid1, OTHER)).fetchone()[:] == ("filtered", "Skipped by administrator")
    # Turning fetch_history OFF on sid1 must revert VIDEO (is_history=1 even though global priority=50)
    # and keep THIRD (new upload retrying at priority=30, is_history=0).
    services.update_source(sid1, SourcePolicy(fetch_history=False, keep_count=20), None)
    with services.db.connect() as conn:
        assert dict(conn.execute("SELECT video_id,state FROM source_entries WHERE source_id=?", (sid1,))) == {
            VIDEO: "candidate", OTHER: "filtered", THIRD: "wanted",
        }
        assert conn.execute("SELECT state FROM source_entries WHERE source_id=? AND video_id=?", (sid2, VIDEO)).fetchone()[0] == "wanted"


async def test_stale_inflight_history_scan_cannot_overwrite_restart(services, monkeypatch):
    url = f"https://www.youtube.com/channel/{CHANNEL}"
    sid = await services.add_source(SourceIdentity("channel", CHANNEL, "C1", url), url, SourcePolicy(fetch_history=True, max_scan_items=2))
    real_run = services.runner.run

    async def run_with_toggle(mode, **kwargs):
        result = await real_run(mode, **kwargs)
        # While scan is in-flight, user toggles OFF then back ON (new history_gen, cursor reset to 1).
        services.update_source(sid, SourcePolicy(fetch_history=False, max_scan_items=2), None)
        services.update_source(sid, SourcePolicy(fetch_history=True, max_scan_items=2), None)
        return result

    monkeypatch.setattr(services.runner, "run", run_with_toggle)
    services.runner.entries = [{"id": VIDEO, "sort_key": 1}]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        # Stale short page must NOT clear the newly requested history_scan.
        assert get_meta(conn, f"history_scan:{sid}") == "1"
        assert get_meta(conn, f"history_cursor:{sid}") == ""
