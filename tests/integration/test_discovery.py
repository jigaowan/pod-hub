from __future__ import annotations

import httpx

from podhub.core.discovery import list_entries, normalize_source
from podhub.db.repositories import start_attempt
from podhub.domain.enums import SourceKind
from podhub.domain.models import SourceIdentity, stamp
from podhub.domain.policy import SourcePolicy
from podhub.ytdlp.rss import fetch_atom

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


async def test_discovery_filters_onboarding_tail_scan_and_backfill(services):
    url = "https://www.youtube.com/playlist?list=PLplaylist"
    assert normalize_source(url) == (SourceKind.PLAYLIST, url)
    identity = SourceIdentity("playlist", "PLplaylist", "Playlist", url)
    policy = SourcePolicy(min_duration_s=60, initial_backfill_count=1)
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
            VIDEO: "wanted", OTHER: "candidate", THIRD: "filtered",
        }
    services.runner.entries = [
        *services.runner.entries[:2],
        {"id": "tailfeed001", "timestamp": "2026-04-12", "duration": 90, "sort_key": 3},
    ]
    with services.db.transaction() as conn:
        attempt = start_attempt(conn, "discovery", sid)
    await services.discovery.sync(services.source(sid), attempt)
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM source_entries WHERE video_id='tailfeed001'").fetchone()[0] == "wanted"
        assert conn.execute("SELECT state FROM source_entries WHERE video_id=?", (THIRD,)).fetchone()[0] == "filtered"
    services.update_source(sid, SourcePolicy(min_duration_s=0), None, 1200)
    with services.db.connect() as conn:
        assert conn.execute("SELECT state,evaluated_rev FROM source_entries WHERE video_id=?", (THIRD,)).fetchone()[:] == ("candidate", 2)
    services.source_command(sid, "backfill", count=1)
    with services.db.connect() as conn:
        assert conn.execute("SELECT state FROM source_entries WHERE video_id=?", (THIRD,)).fetchone()[0] == "wanted"
        assert conn.execute("SELECT state FROM source_entries WHERE video_id=?", (OTHER,)).fetchone()[0] == "candidate"


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
