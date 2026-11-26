from __future__ import annotations

import json
import sqlite3

from podhub.core.notifications import Transition
from podhub.domain.enums import Category, Scope
from podhub.domain.models import stamp
from podhub.errors.breaker import Breakers
from podhub.errors.taxonomy import Classification
from podhub.ytdlp.runner import RunResult


def interrupt_attempt(conn: sqlite3.Connection, breakers: Breakers, attempt: int, reason: str) -> None:
    conn.execute(
        """UPDATE attempts SET outcome='skipped',ended_at=?,error_excerpt=?
           WHERE id=? AND outcome='running'""", (stamp(), reason, attempt),
    )
    breakers.release(conn, attempt)


def finish_attempt(
    conn: sqlite3.Connection, breakers: Breakers, attempt: int, run: RunResult, outcome: str,
    failure: Classification | None = None, cookie_id: int | None = None,
) -> tuple[Classification | None, list[Transition]]:
    ended = stamp()
    conn.execute(
        """UPDATE attempts SET ended_at=?,phase_reached=?,outcome=?,category=?,scope=?,confidence=?,
           exit_code=?,http_status=?,error_excerpt=?,warnings_json=?,ytdlp_version=?,bytes_downloaded=?,avg_bps=?
           WHERE id=?""",
        (ended, run.phase, outcome, failure.category if failure else None,
         failure.scope if failure else None, failure.confidence if failure else None, run.exit_code,
         run.error.get("http_status"), failure.reason if failure else None, json.dumps(run.warnings),
         run.version, run.bytes_downloaded, run.avg_bps, attempt),
    )
    transitions = []
    key = None
    if failure and outcome != "ok":
        failure = breakers.correlate(conn, failure)
        conn.execute(
            "UPDATE attempts SET category=?,scope=?,confidence=? WHERE id=?",
            (failure.category, failure.scope, failure.confidence, attempt),
        )
        if failure.scope == Scope.COOKIE and cookie_id:
            key = f"cookie:{cookie_id}"
        elif failure.scope not in (Scope.VIDEO, Scope.COOKIE):
            key = failure.scope
        if key and (event := breakers.trip(conn, key, failure, run.version)):
            transitions.append(event)
    if cookie_id and outcome == "ok":
        conn.execute(
            "UPDATE cookie_profiles SET last_ok_at=? WHERE id=? AND status IN ('active','suspect')",
            (ended, cookie_id),
        )
    if outcome in ("ok", "deferred") or (outcome == "skipped" and run.probe):
        transitions.extend(breakers.complete_probe(conn, attempt, key))
    elif run.error.get("http_status") not in (429, 403) and failure and failure.category in (
        Category.PERMANENT_UNAVAILABLE, Category.SOFT_UNAVAILABLE, Category.AUTH_REQUIRED,
    ):
        transitions.extend(breakers.complete_probe(conn, attempt, key))
    breakers.release(conn, attempt)
    return failure, transitions
