from __future__ import annotations

import json
from datetime import timedelta

import pytest

from podhub.core.attempts import finish_attempt
from podhub.db.repositories import claim_video, start_attempt
from podhub.domain.models import stamp, utc_now
from podhub.errors.retry import plan_retry
from podhub.errors.taxonomy import classify
from podhub.ytdlp.runner import RunResult

from conftest import COOKIES, OTHER, VIDEO


@pytest.mark.parametrize("run, cookie, expected", [
    (RunResult(error={"http_status": 503, "msg": "busy"}), False, ("transient_network", "video", "high")),
    (RunResult(error={"http_status": 403}, phase="download"), False, ("download_403", "video", "high")),
    (RunResult(error={"http_status": 429}), False, ("rate_limit_or_bot", "global", "high")),
    (RunResult(error={"msg": "nsig extraction failed"}), False, ("toolchain_broken", "toolchain", "high")),
    (RunResult(warnings=["The supplied cookies are no longer valid"]), True, ("cookie_invalid", "cookie", "high")),
    (RunResult(error={"msg": "members-only content"}), False, ("auth_required", "video", "high")),
    (RunResult(gate={"action": "defer", "reason": "is_upcoming"}), False, ("not_ready", "video", "high")),
    (RunResult(error={"msg": "Video has been deleted"}), False, ("permanent_unavailable", "video", "high")),
    (RunResult(error={"msg": "Private video"}), False, ("soft_unavailable", "video", "medium")),
    (RunResult(phase="postprocess", error={"msg": "Thumbnail embed failed"}), False, ("degradable_postprocess", "video", "medium")),
    (RunResult(error={"exc_class": "CorruptedArtifact"}), False, ("corrupted_artifact", "video", "high")),
    (RunResult(exit_code=137), False, ("local_resource", "local", "high")),
    (RunResult(error={"msg": "unexpected failure"}), False, ("unclassified", "video", "low")),
])
def test_classification_matrix(run, cookie, expected):
    result = classify(run, used_cookie=cookie)
    assert (result.category, result.scope, result.confidence) == expected


async def test_unknown_retries_and_deadline(services, source_factory):
    sid = await source_factory()
    services.add_video(sid, VIDEO)
    with services.db.transaction() as conn:
        video = conn.execute("SELECT * FROM videos").fetchone()
        error = classify(RunResult(error={"msg": "Unknown"}))
        decision = plan_retry(services.config.retry, video, error)
        assert decision.state == "pending" and decision.unclassified and decision.next_attempt_at
        conn.execute("UPDATE videos SET category_attempts_json=? WHERE video_id=?", ('{"unclassified":2}', VIDEO))
        video = conn.execute("SELECT * FROM videos").fetchone()
        outcome = plan_retry(services.config.retry, video, error)
        assert outcome.state == "failed" and outcome.unclassified and outcome.next_attempt_at is None
        result = plan_retry(services.config.retry, video, error, now=utc_now() + timedelta(days=15))
        assert result.state == "failed"
        assert claim_video(conn, stamp(seconds=15 * 86400)) is None
        assert conn.execute("SELECT state FROM videos").fetchone()[0] == "failed"


def test_global_breaker_cooldown_and_hold(services):
    b = services.breakers
    bot = classify(RunResult(error={"msg": "Sign in to confirm you're not a bot"}))
    with services.db.transaction() as conn:
        for expected in ("open", "open", "manual_hold"):
            b.trip(conn, "global", bot)
            assert not b.allowed(conn, ("global",))
            assert conn.execute("SELECT state FROM circuit_breakers").fetchone()[0] == expected
            if expected == "manual_hold":
                break
            assert b.trip(conn, "global", bot) is None
            conn.execute("UPDATE circuit_breakers SET open_until=?", (stamp(seconds=-1),))
            b.refresh(conn)
            assert b.allowed(conn, ("global",))
            attempt = start_attempt(conn, "download")
            b.reserve(conn, ("global",), attempt)
            assert not b.allowed(conn, ("global",))
        assert b.reset(conn, "global").state == "closed"


def test_toolchain_version_auto_half_open(services):
    b = services.breakers
    invalid = classify(RunResult(error={"msg": "Unable to extract"}))
    with services.db.transaction() as conn:
        b.trip(conn, "toolchain", invalid, "2025.10.08")
        row = conn.execute("SELECT * FROM circuit_breakers").fetchone()
        assert json.loads(row["context_json"])["ytdlp_version"] == "2025.10.08"
        b.refresh(conn, "2025.9.30")
        assert not b.allowed(conn, ("toolchain",))
        b.refresh(conn, "2025.11.01")
        assert b.allowed(conn, ("toolchain",))
        attempt = start_attempt(conn, "download")
        b.reserve(conn, ("toolchain",), attempt)
        assert b.complete_probe(conn, attempt, None)[0].state == "closed"


async def test_cookie_breaker_scope(services, source_factory):
    a, b = await source_factory("Cookie-backed"), await source_factory("Anonymous")
    cid = services.save_cookie("Login", COOKIES)
    services.update_source(a, services.config.defaults, cid, 60)
    services.add_video(a, VIDEO)
    services.add_video(b, OTHER)
    with services.db.transaction() as conn:
        reason = classify(RunResult(warnings=["cookies are no longer valid"]), used_cookie=True)
        services.breakers.trip(conn, f"cookie:{cid}", reason)
        assert conn.execute("SELECT status FROM cookie_profiles").fetchone()[0] == "expired"
        assert claim_video(conn, stamp())[1] == b
        assert claim_video(conn, stamp()) is None


@pytest.mark.parametrize("message, status, phase, category", [
    ("HTTP error", 403, "download", "rate_limit_or_bot"),
    ("Requested format is not available", None, "download", "toolchain_broken"),
])
async def test_distinct_source_correlation(services, source_factory, message, status, phase, category):
    sources = [await source_factory(f"Source {index}") for index in range(4)]
    run = RunResult(error={"msg": message, "http_status": status}, phase=phase, hello={"ytdlp_version": "2026.11.01"})
    with services.db.transaction() as conn:
        for index, sid in enumerate([sources[0], sources[0], sources[1], sources[2]]):
            attempt = start_attempt(conn, "download", sid)
            result, _ = finish_attempt(conn, services.breakers, attempt, run, "error", classify(run))
            assert (result.category == category) == (index == 3)
        conn.execute("UPDATE attempts SET ended_at=? WHERE source_id=?", (stamp(seconds=-601), sources[0]))
        assert services.breakers.correlate(conn, classify(run)).category != category
