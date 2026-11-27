from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from podhub.config import Breaker
from podhub.core.notifications import Transition
from podhub.db.connection import set_meta
from podhub.domain.enums import Category, Scope
from podhub.domain.models import stamp, utc_now
from podhub.errors.taxonomy import Classification
from podhub.ytdlp.shim import version_parts


class Breakers:
    def __init__(self, settings: Breaker):
        self.settings = settings

    def trip(
        self, conn: sqlite3.Connection, key: str, failure: Classification, version: str | None = None,
    ) -> Transition | None:
        now = utc_now()
        row = conn.execute("SELECT * FROM circuit_breakers WHERE key=?", (key,)).fetchone()
        previous = row["state"] if row else "closed"
        if previous in ("open", "manual_hold"):
            return None
        context = json.loads(row["context_json"]) if row else {}
        history = [tick for tick in context.get("trips", []) if tick >= stamp(now, seconds=-86400)]
        history.append(stamp(now))
        context.update(trips=history, ytdlp_version=version or context.get("ytdlp_version"))
        context.pop("probe_attempt", None)
        count = len(history)
        steps = self.settings.cooldown_steps_s
        state = "open"
        until = stamp(now, seconds=steps[min(count - 1, len(steps) - 1)])
        if key == "global" and count >= self.settings.max_trips_per_24h:
            state, until = "manual_hold", None
        elif key != "global":
            until = None
        conn.execute(
            """INSERT INTO circuit_breakers VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET state=excluded.state,open_until=excluded.open_until,
                 trip_count=excluded.trip_count,reason_category=excluded.reason_category,
                 reason_message=excluded.reason_message,opened_at=excluded.opened_at,
                 context_json=excluded.context_json,updated_at=excluded.updated_at""",
            (key, state, until, count, failure.category, failure.reason, stamp(now), json.dumps(context), stamp(now)),
        )
        if key == "local":
            set_meta(conn, "global_paused", "1")
            set_meta(conn, "global_pause_reason", failure.reason)
        elif key.startswith("cookie:"):
            conn.execute(
                "UPDATE cookie_profiles SET status='expired',last_fail_at=?,updated_at=? WHERE id=?",
                (stamp(now), stamp(now), int(key.split(":")[1])),
            )
        if previous == "closed" or state == "manual_hold":
            return Transition(key, state, failure.category, failure.reason)
        return None

    def refresh(self, conn: sqlite3.Connection, version: str | None = None) -> None:
        now = stamp()
        conn.execute(
            """UPDATE circuit_breakers SET state='half_open',updated_at=?
               WHERE state='open' AND open_until<=?""", (now, now),
        )
        if version:
            row = conn.execute("SELECT * FROM circuit_breakers WHERE key='toolchain' AND state='open'").fetchone()
            if row:
                old = json.loads(row["context_json"]).get("ytdlp_version")
                if not old or version_parts(version) > version_parts(old):
                    conn.execute("UPDATE circuit_breakers SET state='half_open',updated_at=? WHERE key='toolchain'", (now,))

    def allowed(self, conn: sqlite3.Connection, keys: Iterable[str]) -> bool:
        for key in keys:
            row = conn.execute("SELECT state,context_json FROM circuit_breakers WHERE key=?", (key,)).fetchone()
            if row and (
                row["state"] in ("open", "manual_hold") or
                (row["state"] == "half_open" and json.loads(row["context_json"]).get("probe_attempt"))
            ):
                return False
        return True

    def reserve(self, conn: sqlite3.Connection, keys: Iterable[str], attempt: int) -> None:
        for key in keys:
            conn.execute(
                """UPDATE circuit_breakers SET context_json=json_set(context_json,'$.probe_attempt',?)
                   WHERE key=? AND state='half_open'""", (attempt, key),
            )

    def clear_reservations(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE circuit_breakers SET context_json=json_remove(context_json,'$.probe_attempt')")

    def reset(self, conn: sqlite3.Connection, key: str, *, manual: bool = True) -> Transition | None:
        row = conn.execute("SELECT * FROM circuit_breakers WHERE key=?", (key,)).fetchone()
        if row is None or row["state"] == "closed":
            return None
        context = {} if manual else json.loads(row["context_json"])
        context.pop("probe_attempt", None)
        conn.execute(
            """UPDATE circuit_breakers SET state='closed',open_until=NULL,trip_count=?,reason_category=NULL,
               reason_message=NULL,context_json=?,updated_at=? WHERE key=?""",
            (len(context.get("trips", [])), json.dumps(context), stamp(), key),
        )
        if key == "local":
            set_meta(conn, "global_paused", "0")
            set_meta(conn, "global_pause_reason", "")
        return Transition(key, "closed", row["reason_category"], row["reason_message"])

    def complete_probe(
        self, conn: sqlite3.Connection, attempt: int, failed_key: str | None, *, used_toolchain: bool = True,
    ) -> list[Transition]:
        events = []
        for row in conn.execute(
            """SELECT key FROM circuit_breakers WHERE state='half_open'
               AND json_extract(context_json,'$.probe_attempt')=?""", (attempt,),
        ).fetchall():
            if row["key"] == "toolchain" and not used_toolchain:
                continue
            if row["key"] != failed_key and (event := self.reset(conn, row["key"], manual=False)):
                events.append(event)
        return events

    def release(self, conn: sqlite3.Connection, attempt: int) -> None:
        conn.execute(
            """UPDATE circuit_breakers SET context_json=json_remove(context_json,'$.probe_attempt')
               WHERE json_extract(context_json,'$.probe_attempt')=?""", (attempt,),
        )

    def correlate(self, conn: sqlite3.Connection, failure: Classification) -> Classification:
        key = failure.correlation_key
        if not key or failure.scope != Scope.VIDEO:
            return failure
        window_start = stamp(seconds=-self.settings.correlation_window_s)
        if key == "format_unavailable":
            count = conn.execute(
                """SELECT count(DISTINCT source_id) FROM attempts WHERE ended_at>=? AND outcome='error'
                   AND category IN ('unclassified','toolchain_broken')
                   AND (lower(error_excerpt) LIKE '%requested format is not available%'
                        OR lower(error_excerpt) LIKE '%no video formats found%')""", (window_start,),
            ).fetchone()[0]
        else:
            count = conn.execute(
                """SELECT count(DISTINCT source_id) FROM attempts WHERE ended_at>=? AND outcome='error'
                   AND category IN ('download_403','rate_limit_or_bot')""", (window_start,),
            ).fetchone()[0]
        if count < self.settings.correlation_distinct_sources:
            return failure
        if key == "format_unavailable":
            return failure.escalate(Category.TOOLCHAIN_BROKEN, Scope.TOOLCHAIN)
        return failure.escalate(Category.RATE_LIMIT_OR_BOT, Scope.GLOBAL)
