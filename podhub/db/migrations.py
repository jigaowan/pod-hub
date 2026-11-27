from __future__ import annotations

MIGRATIONS = (
    """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
); -- schema_version, global_paused, global_pause_reason, last_reconcile_at, last_backup_at

CREATE TABLE cookie_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    file_path TEXT NOT NULL,          -- relative to STATE_DIR/cookies/
    status TEXT NOT NULL DEFAULT 'active', -- active | suspect | expired | disabled
    earliest_expiry TEXT,
    last_ok_at TEXT,
    last_fail_at TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- channel | playlist | manual
    source_input TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE, -- canonical UC... / PL... / manual:<uuid>
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,        -- folder name under OUTPUT_DIR/
    feed_token TEXT NOT NULL UNIQUE,  -- secrets.token_urlsafe(16)
    cover_filename TEXT,              -- cover.<hash>.jpg under OUTPUT_DIR/<slug>/
    enabled INTEGER NOT NULL DEFAULT 1,
    policy_json TEXT NOT NULL,        -- filter, retention, feed_order, backfill_count, postprocess opts
    policy_rev INTEGER NOT NULL DEFAULT 1,
    cookie_profile_id INTEGER REFERENCES cookie_profiles(id) ON DELETE SET NULL,
    check_interval_s INTEGER NOT NULL DEFAULT 1800,
    next_check_at TEXT,
    last_checked_at TEXT,
    last_ok_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT,
    last_error_message TEXT,
    http_etag TEXT,
    http_last_modified TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE videos (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    channel_id TEXT,
    description TEXT,
    published_at TEXT,
    upload_date TEXT,
    duration_s INTEGER,
    live_status TEXT,
    availability TEXT,
    release_timestamp INTEGER,
    thumbnail_url TEXT,
    meta_json TEXT,
    state TEXT NOT NULL DEFAULT 'pending', -- pending | running | ready | deferred | unavailable | failed | purged
    priority INTEGER NOT NULL DEFAULT 50,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    category_attempts_json TEXT NOT NULL DEFAULT '{}',
    next_attempt_at TEXT,
    give_up_at TEXT,
    last_category TEXT,
    last_scope TEXT,
    last_error TEXT,
    unclassified INTEGER NOT NULL DEFAULT 0,
    running_since TEXT,
    media_bytes INTEGER,
    media_duration_s INTEGER,
    media_mime TEXT,
    ready_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE source_entries (
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    state TEXT NOT NULL,              -- candidate | filtered | wanted | published | retiring | evicted
    filter_reason TEXT,
    evaluated_rev INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    sort_key INTEGER,                 -- playlist position or sequence
    entry_media_path TEXT,            -- relative to OUTPUT_DIR: <slug>/<audio filename>
    discovered_at TEXT NOT NULL,
    published_at TEXT,
    unpublished_at TEXT,
    evicted_at TEXT,
    PRIMARY KEY (source_id, video_id)
);

CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,               -- discovery | download | doctor | cookie_check
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    video_id TEXT REFERENCES videos(video_id) ON DELETE SET NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    phase_reached TEXT,
    outcome TEXT NOT NULL,            -- ok | skipped | deferred | error
    category TEXT,
    scope TEXT,
    confidence TEXT,
    exit_code INTEGER,
    http_status INTEGER,
    error_excerpt TEXT,
    warnings_json TEXT,
    ytdlp_version TEXT,
    cookie_profile_id INTEGER,
    bytes_downloaded INTEGER,
    avg_bps INTEGER
);

CREATE TABLE circuit_breakers (
    key TEXT PRIMARY KEY,             -- global | toolchain | local | cookie:<id>
    state TEXT NOT NULL DEFAULT 'closed', -- closed | open | half_open | manual_hold
    open_until TEXT,
    trip_count INTEGER NOT NULL DEFAULT 0,
    reason_category TEXT,
    reason_message TEXT,
    opened_at TEXT,
    context_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE video_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,               -- e.g. transcript_vtt, summary_md, chapters_json
    rel_path TEXT NOT NULL UNIQUE,    -- relative to OUTPUT_DIR
    mime_type TEXT NOT NULL,
    is_public INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(video_id, source_id, kind)
);

CREATE TABLE postprocess_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    source_id INTEGER REFERENCES sources(id) ON DELETE CASCADE,
    processor_name TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', -- pending | running | done | failed
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(video_id, source_id, processor_name)
);

CREATE INDEX idx_videos_sched ON videos(state, next_attempt_at, priority DESC);
CREATE INDEX idx_entries_source_state ON source_entries(source_id, state, published_at DESC);
CREATE INDEX idx_entries_video ON source_entries(video_id, state);
CREATE INDEX idx_attempts_corr ON attempts(ended_at, category, source_id);
CREATE INDEX idx_attempts_video ON attempts(video_id, id DESC);
CREATE INDEX idx_attempts_fairness ON attempts(kind, source_id, started_at DESC);
CREATE INDEX idx_sources_due ON sources(enabled, next_check_at);
CREATE INDEX idx_postprocess_due ON postprocess_tasks(state, next_attempt_at);
    """,
    """
ALTER TABLE source_entries ADD COLUMN is_history INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN is_backfill INTEGER NOT NULL DEFAULT 0;
    """,
)
