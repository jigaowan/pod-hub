from __future__ import annotations

import asyncio
import logging
import random
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from podhub.config import Config
from podhub.core.attempts import finish_attempt, interrupt_attempt
from podhub.core.notifications import Notifier
from podhub.db.connection import Database, get_meta, set_meta
from podhub.db.repositories import activate_video, policy_for, start_attempt, upsert_video
from podhub.domain.enums import Category, SourceKind
from podhub.domain.models import SourceIdentity, parse_date, stamp, utc_now
from podhub.domain.policy import SourcePolicy
from podhub.errors.breaker import Breakers
from podhub.errors.taxonomy import classify
from podhub.storage.cookies import copy_cookie
from podhub.storage.paths import VIDEO_ID, YOUTUBE_HOSTS
from podhub.ytdlp.rss import AtomResult, fetch_atom
from podhub.ytdlp.runner import RunResult, Runner, redact
from podhub.ytdlp.shim import CHANNEL_ID, PLAYLIST_ID, version_parts

log = logging.getLogger(__name__)
NETWORK_GATES = ("global", "toolchain", "local")


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Preview:
    identity: SourceIdentity
    entries: list[dict[str, Any]]


def normalize_source(value: str, kind: SourceKind | None = None) -> tuple[SourceKind, str]:
    value = value.strip()
    if kind == SourceKind.MANUAL:
        raise ValueError("manual sources do not use YouTube discovery")
    if not value:
        raise ValueError("enter a YouTube channel or playlist")
    if value.startswith("@") and len(value) > 1 and not any(char in value for char in "/?#"):
        if kind == SourceKind.PLAYLIST:
            raise ValueError("enter a YouTube playlist")
        return SourceKind.CHANNEL, f"https://www.youtube.com/{value}"
    if CHANNEL_ID.fullmatch(value) and kind != SourceKind.PLAYLIST:
        return SourceKind.CHANNEL, f"https://www.youtube.com/channel/{value}"
    if PLAYLIST_ID.fullmatch(value) and (kind == SourceKind.PLAYLIST or value.startswith(("PL", "UU", "UULF", "OLAK", "RD"))):
        return SourceKind.PLAYLIST, f"https://www.youtube.com/playlist?list={value}"
    url = urlsplit(value if "://" in value else f"https://{value}")
    if url.scheme not in ("https", "http") or url.hostname not in YOUTUBE_HOSTS or url.username or url.password:
        raise ValueError("enter a YouTube channel or playlist URL")
    parts = url.path.strip("/").split("/")
    playlist = parse_qs(url.query).get("list", [""])[0]
    if playlist and kind != SourceKind.CHANNEL and PLAYLIST_ID.fullmatch(playlist):
        return SourceKind.PLAYLIST, f"https://www.youtube.com/playlist?list={playlist}"
    if kind != SourceKind.PLAYLIST:
        if parts[0].startswith("@") and len(parts[0]) > 1:
            return SourceKind.CHANNEL, f"https://www.youtube.com/{parts[0]}"
        if len(parts) >= 2 and parts[0] in ("channel", "user", "c") and parts[1]:
            return SourceKind.CHANNEL, f"https://www.youtube.com/{parts[0]}/{parts[1]}"
    raise ValueError("enter a YouTube channel or playlist URL")


def listing_url(kind: str, external_id: str, url: str) -> str:
    if kind == "channel" and CHANNEL_ID.fullmatch(external_id):
        return f"https://www.youtube.com/playlist?list=UU{external_id[2:]}"
    return url


def cookie_for(conn: sqlite3.Connection, cookie_id: int | None) -> sqlite3.Row | None:
    if cookie_id is None:
        return None
    return conn.execute("SELECT * FROM cookie_profiles WHERE id=?", (cookie_id,)).fetchone()


def list_entries(
    conn: sqlite3.Connection, config: Config, source: sqlite3.Row, entries: list[dict[str, Any]],
    *, backfill: int | None = None,
) -> None:
    policy = policy_for(source)
    seen = set()
    initial = source["last_ok_at"] is None
    latest_known = None
    if backfill is not None and not initial:
        # Compare new uploads against the source history before this scan changes it.
        latest_known = conn.execute(
            """SELECT max(v.published_at) FROM source_entries e JOIN videos v USING(video_id)
               WHERE e.source_id=?""", (source["id"],),
        ).fetchone()[0]
    dated = [(parse_date(item.get("timestamp")) or parse_date(item.get("upload_date")), item) for item in entries]
    if initial or backfill is not None:
        dated.sort(key=lambda entry: entry[0] or "", reverse=backfill is None)
    budget = backfill if backfill is not None else policy.initial_backfill_count
    for published, item in dated:
        vid = item.get("id")
        if not isinstance(vid, str) or not VIDEO_ID.fullmatch(vid) or vid in seen:
            continue
        seen.add(vid)
        upsert_video(conn, item)
        existing = conn.execute(
            "SELECT state FROM source_entries WHERE source_id=? AND video_id=?", (source["id"], vid),
        ).fetchone()
        if existing:
            if item.get("sort_key") is not None:
                conn.execute(
                    "UPDATE source_entries SET sort_key=? WHERE source_id=? AND video_id=?",
                    (item["sort_key"], source["id"], vid),
                )
            continue
        reason = policy.rejection(item)
        historical = backfill is not None and not (latest_known and published and published > latest_known)
        limited = initial or historical
        if reason:
            state = "filtered"
        elif limited and budget <= 0:
            state = "candidate"
        else:
            state = "wanted"
            if limited:
                budget -= 1
        conn.execute(
            """INSERT INTO source_entries(source_id,video_id,state,filter_reason,evaluated_rev,sort_key,discovered_at)
               VALUES (?,?,?,?,?,?,?)""",
            (source["id"], vid, state, reason, source["policy_rev"], item.get("sort_key"), stamp()),
        )
        if state == "wanted":
            activate_video(conn, config, vid, 10 if limited else 50)


class Discovery:
    def __init__(self, config: Config, db: Database, runner: Runner, breakers: Breakers, notifier: Notifier):
        self.config = config
        self.db = db
        self.runner = runner
        self.breakers = breakers
        self.notifier = notifier

    def check_version(self, run: RunResult) -> None:
        minimum = self.config.toolchain.min_ytdlp_version
        if run.version and version_parts(run.version) < version_parts(minimum):
            run.error = {"exc_class": "ToolchainError", "msg": f"yt-dlp {run.version} is older than {minimum}"}

    async def preview(self, value: str, kind: SourceKind | None, cookie_id: int | None) -> Preview:
        selected_kind, url = normalize_source(value, kind)
        gates = (*NETWORK_GATES, f"cookie:{cookie_id}") if cookie_id is not None else NETWORK_GATES
        with self.db.transaction() as conn:
            cookie = cookie_for(conn, cookie_id)
            if cookie_id is not None and (not cookie or cookie["status"] not in ("active", "suspect")):
                raise SourceError("cookie profile needs validation")
            self.breakers.refresh(conn)
            if not self.breakers.allowed(conn, gates):
                raise SourceError("discovery is blocked by an open circuit breaker")
            attempt = start_attempt(conn, "discovery", cookie_id=cookie_id)
            self.breakers.reserve(conn, gates, attempt)
        run = RunResult()
        entries: list[dict[str, Any]] = []
        identity = None
        try:
            with TemporaryDirectory(dir=self.config.paths.state_dir / "tmp") as work:
                extras = {"cookiefile": copy_cookie(self.config.paths.state_dir / "cookies", cookie["file_path"], Path(work))} if cookie else {}
                run = await self.runner.run("resolve", url=url, kind=selected_kind, limit=1, **extras)
                self.check_version(run)
                if not run.error and run.result:
                    identity = SourceIdentity(**{key: run.result.get(key) for key in SourceIdentity.__dataclass_fields__})
                    run = await self.runner.run(
                        "list", url=listing_url(identity.kind, identity.external_id, identity.normalized_url),
                        limit=20, on_entry=lambda item: entries.append(item) is None, **extras,
                    )
                    self.check_version(run)
                if identity is None and not run.error:
                    run.error = {"msg": "could not resolve the source"}
        except asyncio.CancelledError:
            with self.db.transaction() as conn:
                interrupt_attempt(conn, self.breakers, attempt, "Preview interrupted")
            raise
        except Exception as exc:
            run.error = {"exc_class": type(exc).__name__, "msg": redact(str(exc))}
        failure = classify(run, used_cookie=bool(cookie)) if run.error or run.warnings else None
        if failure and failure.category == Category.COOKIE_INVALID:
            run.error = {"msg": failure.reason}
        with self.db.transaction() as conn:
            _, events = finish_attempt(conn, self.breakers, attempt, run, "error" if run.error else "ok",
                                       failure if run.error else None, cookie_id)
        await self.notifier.send(events)
        if run.error or identity is None:
            raise SourceError((failure.reason if failure else "could not resolve the source"))
        return Preview(identity, entries)

    async def atom(self, source: sqlite3.Row, policy: SourcePolicy) -> tuple[AtomResult, str]:
        cached_hint = source["http_etag"]
        # Validators are only valid for the same URL.
        with self.db.connect() as conn:
            cached_kind = get_meta(conn, f"atom_variant:{source['id']}")
        proxy = self.config.toolchain.ytdlp_proxy or None
        async with httpx.AsyncClient(timeout=20, proxy=proxy, follow_redirects=True) as client:
            for long_form in ([True, False] if not policy.include_shorts else [False]):
                kind = "long" if long_form else "channel"
                try:
                    result = await fetch_atom(
                        client, source["external_id"], long_form=long_form,
                        etag=cached_hint if kind == cached_kind else None,
                        last_modified=source["http_last_modified"] if kind == cached_kind else None,
                    )
                except (httpx.HTTPError, ET.ParseError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
                        raise
                    if long_form:
                        continue
                    raise
                return result, kind
        raise RuntimeError("none of the Atom endpoints were available")

    async def sync(self, source: sqlite3.Row, attempt: int) -> None:
        policy = policy_for(source)
        run = RunResult()
        entries: list[dict[str, Any]] = []
        atom = None
        atom_kind = None
        deep = False
        force_deep = ""
        backfill = None
        cookie_id = source["cookie_profile_id"]
        try:
            with self.db.connect() as conn:
                cookie = cookie_for(conn, cookie_id)
                known = {row[0] for row in conn.execute("SELECT video_id FROM source_entries WHERE source_id=?", (source["id"],))}
                last_deep = get_meta(conn, f"last_deep:{source['id']}")
                force_deep = get_meta(conn, f"force_deep:{source['id']}")
                pending_backfill = int(force_deep or "0")
                if pending_backfill > 0:
                    backfill = pending_backfill
            quick = source["kind"] == "channel" and not cookie and source["last_ok_at"] and backfill is None
            rss_fallback = False
            weekly = bool(last_deep and last_deep <= stamp(seconds=-7 * 86400))
            if quick and not weekly:
                try:
                    atom, atom_kind = await self.atom(source, policy)
                    entries = atom.entries
                except (httpx.HTTPError, ET.ParseError) as exc:
                    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                    if status == 429:
                        run.error = {"http_status": 429, "msg": "YouTube Atom rate limited"}
                    else:
                        rss_fallback = True
                        run.warnings.append("YouTube Atom failed; using a bounded flat listing")
                        log.warning("Atom fallback for source %s", source["id"])
            if atom is None and not run.error:
                early = bool(source["last_ok_at"] and not weekly and backfill is None and (
                    source["kind"] == "channel" or policy.playlist_scan_mode == "head_early_stop"
                ))
                consecutive = 0

                def collect(item: dict[str, Any]) -> bool:
                    nonlocal consecutive
                    entries.append({**item, '_flat_listing': True})
                    consecutive = consecutive + 1 if item.get("id") in known else 0
                    return not early or consecutive < policy.early_stop_known

                with TemporaryDirectory(dir=self.config.paths.state_dir / "tmp") as work:
                    extras = {"cookiefile": copy_cookie(self.config.paths.state_dir / "cookies", cookie["file_path"], Path(work))} if cookie else {}
                    warnings = run.warnings
                    run = await self.runner.run(
                        "list", url=listing_url(source["kind"], source["external_id"], source["url"]),
                        limit=min(30, policy.max_scan_items) if rss_fallback else policy.max_scan_items,
                        on_entry=collect, **extras,
                    )
                    run.warnings[:0] = warnings
                    self.check_version(run)
                deep = not rss_fallback and not early
        except Exception as exc:
            run.error = {"exc_class": type(exc).__name__, "msg": redact(str(exc))}
        failure = classify(run, used_cookie=bool(cookie_id), minimum=self.config.toolchain.min_ytdlp_version) if run.error or run.warnings else None
        if failure and failure.category == Category.COOKIE_INVALID:
            run.error = {"msg": failure.reason}
        now = utc_now()
        with self.db.transaction() as conn:
            current = conn.execute("SELECT * FROM sources WHERE id=?", (source["id"],)).fetchone()
            if current and current["enabled"] and current["cookie_profile_id"] == cookie_id:
                next_check = stamp(now, seconds=current["check_interval_s"] * random.uniform(0.85, 1.15))
                next_check_overridden = current["next_check_at"] != source["next_check_at"]
                if run.error:
                    failures = current["consecutive_failures"] + 1
                    next_check = stamp(now, seconds=min(max(21600, current["check_interval_s"]), current["check_interval_s"] * 2 ** min(failures, 6)))
                    conn.execute(
                        """UPDATE sources SET last_checked_at=?,next_check_at=?,consecutive_failures=?,
                           last_error_category=?,last_error_message=?,updated_at=? WHERE id=?""",
                        (stamp(now), current["next_check_at"] if next_check_overridden else next_check, failures,
                         failure.category if failure else None,
                         failure.reason if failure else None, stamp(now), current["id"]),
                    )
                else:
                    list_entries(conn, self.config, current, entries, backfill=backfill)
                    conn.execute(
                        """UPDATE sources SET last_checked_at=?,last_ok_at=?,next_check_at=?,consecutive_failures=0,
                           last_error_category=NULL,last_error_message=NULL,http_etag=?,http_last_modified=?,updated_at=? WHERE id=?""",
                        (stamp(now), stamp(now), current["next_check_at"] if next_check_overridden else next_check,
                         atom.etag if atom else current["http_etag"],
                         atom.last_modified if atom else current["http_last_modified"], stamp(now), current["id"]),
                    )
                    if atom_kind:
                        set_meta(conn, f"atom_variant:{source['id']}", atom_kind)
                    if deep:
                        set_meta(conn, f"last_deep:{source['id']}", stamp(now))
                    if force_deep and (backfill is None or deep):
                        conn.execute("DELETE FROM meta WHERE key=? AND value=?", (f"force_deep:{source['id']}", force_deep))
            _, events = finish_attempt(conn, self.breakers, attempt, run, "error" if run.error else "ok",
                                       failure if run.error else None, cookie_id)
        await self.notifier.send(events)


def due_source(conn: sqlite3.Connection, now: datetime) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM sources WHERE enabled=1 AND kind!='manual' AND (next_check_at IS NULL OR next_check_at<=?)
           AND (cookie_profile_id IS NULL OR EXISTS (
             SELECT 1 FROM cookie_profiles c WHERE c.id=sources.cookie_profile_id AND c.status IN ('active','suspect')
           )) ORDER BY next_check_at,id""", (stamp(now),),
    ).fetchall()
