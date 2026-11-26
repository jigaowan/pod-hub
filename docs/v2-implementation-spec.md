# Pod Hub V2 Implementation Specification

> Target Executor: Codex (`gpt-6.2-sol`, reasoning effort `high`)
> Target Branch: `v2` (clean-slate rewrite; do NOT constrain yourself to the legacy V1 code in `app/`)
> Deployment Target: NixOS VPS (Nix Flake + NixOS module; NO Docker)

---

## 1. Mission & Scope

Build **Pod Hub V2**, a self-hosted, resilient YouTube-to-Podcast ingestion and publishing service designed for long-term unattended operation on a NixOS VPS.

### Core Goals
1. **Multi-source YouTube ingestion**: Subscribe to YouTube **channels**, **playlists**, and **manual single-video collections** (`manual` feed for ad-hoc videos).
2. **Channel-folder-organized output directory**: Store fetched audio files, covers, and future sidecar artifacts cleanly under `<OUTPUT_DIR>/<source_slug>/` so the directory is directly usable by external tools, file browsers, or sync tools.
3. **App-served multi-output layer**: The application serves Podcast RSS 2.0 feeds, media files (with HTTP `Range`/`HEAD`/`ETag`), covers, and OPML via a clean `outputs/` abstraction so additional output formats (JSON Feed, static exports, etc.) can be added later without changing core storage or sync logic.
4. **Rock-solid discovery, persistent queue, and structured error handling**:
   - Decoupled discovery and downloading;
   - Single-source-of-truth persistent SQLite (WAL) state machine;
   - Isolated `yt-dlp` subprocess shim emitting structured JSON Lines;
   - Disciplined `(category, scope, confidence)` error taxonomy with bounded retries, exponential backoff, circuit breakers, crash recovery, and zero silent infinite loops.
5. **Simple, high-utility Web Admin Panel** (SSR + HTMX, zero Node/npm build pipeline) for visibility and day-2 operations.
6. **Reserved Post-Processing Extension Architecture**: Provide a clean, decoupled post-processing hook/protocol & artifact storage boundary for future modules (e.g., audio transcription/VTT, AI summaries, chapter extraction, loudness normalization) **without implementing transcription or heavy post-processors now**.
7. **First-class NixOS deployment**: Replace Docker completely with a clean `flake.nix` exporting `devShells`, `packages.default`, and `nixosModules.default` (`services.pod-hub`).

### Explicit Non-Goals (Do NOT Build in V2 MVP)
- **Externalized anti-bot / IP risk management**: Do NOT build PO Token sidecars (`bgutil`), proxy pools, IP rotation, or active anti-bot evasion. Support standard proxy configuration (`ytdlp_proxy` / env) and optional per-source cookie files, plus safe cooldown circuit-breaking if upstream returns 429/bot-check so we never hammer YouTube.
- **No transcription implementation**: Remove legacy `app/transcription/` and Cloudflare AI Gateway code. Only build the lightweight `postprocess` extension skeleton (verified with a mock processor in unit tests).
- **No Docker**: Remove `Dockerfile` and `docker-compose.yml` on branch `v2`.
- **No heavy frontend toolchain / WebSockets / multi-user auth**: Panel binds `127.0.0.1` by default (reverse proxy / Tailscale handles auth externally). Use Jinja2 + HTMX + minimal CSS (inline/static, no CDN required or optional vendored HTMX).
- **No in-app `yt-dlp` venv installer/auto-updater**: Toolchain binaries (`ytdlp_python`, `ffmpeg_path`, `js_runtime`) are supplied by NixOS/system configuration and checked at runtime.

---

## 2. Repository Hygiene on Branch `v2`

You are working on git branch `v2`. Replace the legacy V1 implementation cleanly:
- Remove obsolete Docker files (`Dockerfile`, `docker-compose.yml`), legacy V1 `docs/` (keep `docs/v2-implementation-spec.md` and write updated `README.md`), legacy `app/` modules, and legacy `tests/`.
- Keep the codebase clean, modular, strongly typed (Python 3.12+ with `from __future__ import annotations`, dataclasses/Pydantic, `mypy`/type-friendly code), and thoroughly tested with `pytest`.

### Recommended Package Structure

```text
pod-hub/
├── flake.nix                         # devShell, package, nixosModules.default
├── pyproject.toml                    # hatchling build, console script `pod-hub`
├── README.md                         # NixOS deployment, config.toml, CLI & architecture overview
├── docs/
│   └── v2-implementation-spec.md     # This specification
├── podhub/
│   ├── __init__.py
│   ├── cli.py                        # CLI entrypoints: serve, doctor, sync, pause, resume, etc.
│   ├── config.py                     # TOML + env config loader & validation
│   ├── main.py                       # FastAPI app factory, lifespan, background runtime wiring
│   ├── db/
│   │   ├── connection.py             # SQLite WAL engine, BEGIN IMMEDIATE helper, backups
│   │   ├── migrations.py             # Schema creation & versioned SQL migrations
│   │   └── repositories.py           # Clean query/command repositories (or split per entity)
│   ├── domain/
│   │   ├── enums.py                  # SourceKind, VideoState, EntryState, ErrorCategory, ErrorScope, etc.
│   │   ├── models.py                 # Typed domain dataclasses/DTOs
│   │   └── policy.py                 # SourceFilterPolicy, RetentionPolicy, matching & validation
│   ├── ytdlp/
│   │   ├── shim.py                   # Standalone subprocess script (stdlib + yt_dlp ONLY!) emitting JSONL
│   │   ├── runner.py                 # Parent subprocess manager, process-group kill, watchdog, JSONL parser
│   │   ├── rss.py                    # YouTube official Atom RSS fetcher (ETag / If-Modified-Since / UULF)
│   │   └── ffprobe.py                # Media validation (duration, readability, faststart check)
│   ├── errors/
│   │   ├── taxonomy.py               # (category, scope, confidence) classifier + version-aware rule table
│   │   ├── retry.py                  # Backoff schedules, per-category budgets, give_up_at, recheck_at
│   │   └── breaker.py                # Circuit breakers: global, toolchain, cookie:<id>, local pause + correlation
│   ├── storage/
│   │   ├── paths.py                  # Safe slug/path builders, strict STATE_DIR vs OUTPUT_DIR isolation
│   │   ├── media.py                  # Atomic publish to <OUTPUT_DIR>/<slug>/, cross-source hardlink dedup, eviction
│   │   └── cookies.py                # 0600 cookie file management & per-attempt isolated copy
│   ├── core/
│   │   ├── discovery.py              # Dual-channel discovery (RSS fast path + flat-playlist early-stop)
│   │   ├── scheduler.py              # Single-process asyncio 5s tick loop: discovery, download, maintenance
│   │   ├── downloader.py             # Single-video execution pipeline + error classification + breaker update
│   │   ├── retention.py              # Keep-N / keep-days / grace period (`retiring` -> `evicted`) + reconciliation
│   │   ├── notifications.py          # Webhook notifier (breaker state transitions + optional digest)
│   │   └── services.py               # Application service facade shared by Web UI & CLI
│   ├── postprocess/
│   │   ├── base.py                   # PostProcessor Protocol, PostProcessContext, ArtifactOutput
│   │   ├── registry.py               # Processor registration & lookup
│   │   └── worker.py                 # Decoupled async hook consuming ready videos into postprocess_tasks
│   ├── outputs/
│   │   ├── base.py                   # OutputRenderer Protocol (extensible for future outputs)
│   │   ├── rss.py                    # Podcast RSS 2.0 + iTunes + Podcast 2.0 renderer (XML 1.0 sanitized)
│   │   └── opml.py                   # OPML subscription export
│   └── web/
│       ├── app.py                    # FastAPI router assembly, static files, /healthz
│       ├── routes/
│       │   ├── admin.py              # 4-page SSR + HTMX admin routes
│       │   └── public.py             # /feeds/{token}.xml, /media/{token}/{filename}, /covers/{token}/{filename}
│       ├── templates/                # Jinja2 templates (base, overview, sources, source_detail, videos, cookies)
│       └── static/                   # Minimal CSS (+ vendored htmx.min.js or progressive enhancement)
└── tests/
    ├── conftest.py
    ├── unit/
    └── integration/
```

---

## 3. Storage & Directory Layout (Channel-Folder Organized)

Keep runtime state (`STATE_DIR`) and user-facing media output (`OUTPUT_DIR`) strictly separated. Never leave `.tmp` files, `.part` files, SQLite databases, or cookies inside `OUTPUT_DIR`.

```text
<STATE_DIR>/                           # e.g. /var/lib/pod-hub
├── app.db                             # SQLite database (WAL mode)
├── tmp/
│   └── <attempt_id>/                  # Per-attempt isolated download/transcode/cookie workspace
├── cookies/
│   └── <profile_id>.txt               # 0600 permissions; cookie contents NEVER stored in DB
└── backups/
    └── app-YYYYMMDD-HHMMSS.db         # Daily SQLite VACUUM INTO backup (keep last 7)

<OUTPUT_DIR>/                          # e.g. /srv/podcasts (configurable by user/NixOS module)
└── <source_slug>/                     # Stable, URL-safe, filesystem-safe directory per channel/playlist/manual
    ├── cover.<hash>.jpg               # Content-hashed artwork filename (busts sticky podcast client cache)
    ├── <video_id>.m4a                 # Stable audio filename (NO date prefix; dates can change on premieres/lives)
    └── artifacts/                     # Reserved clean subdir for future post-processors (e.g., <video_id>.vtt)
```

### Key Storage Rules
1. **Stable slug & filenames**:
   - `sources.slug` is generated on creation (unique, filesystem-safe `[a-z0-9-]`, collision suffix `-2`, `-3`) and **immutable** unless explicitly changed by admin.
   - Audio filenames use `<video_id>.<ext>` (default `.m4a`). Note: YouTube `video_id` may start with `-`; always pass full canonical URL `https://www.youtube.com/watch?v=<video_id>` to `yt-dlp` and `--` before file paths in `ffmpeg`/`ffprobe` invocations!
2. **Cross-source deduplication with self-contained channel folders**:
   - Videos are tracked globally by `video_id` in `videos` so the same video is **never downloaded twice** even if it belongs to both a channel and a playlist.
   - When a video is already `ready` in Source A (`<OUTPUT_DIR>/<slug_a>/<video_id>.m4a`) and becomes `wanted` in Source B:
     - Publish directly to `<OUTPUT_DIR>/<slug_b>/<video_id>.m4a` using an **OS hardlink** (`os.link`) if on the same filesystem (zero extra disk space, self-contained folder, `rclone`/Audiobookshelf friendly without broken symlinks!), falling back to atomic copy (`tmp` + `fsync` + `os.replace`) if `EXDEV`.
   - Record each source entry's relative file path (`entry_media_path`, relative to `OUTPUT_DIR`) in `source_entries`.
3. **Atomic Publish Order (No Partial Files Ever Visible)**:
   - Download/remux inside `STATE_DIR/tmp/<attempt_id>/` → `ffprobe` validation → if same filesystem, `fsync` and `os.replace` into `<OUTPUT_DIR>/<slug>/<video_id>.m4a`; if `EXDEV`, copy to `<OUTPUT_DIR>/<slug>/.<video_id>.tmp` → `fsync` → `os.replace` → delete temp file → update DB (`videos.state = 'ready'`, `source_entries.state = 'published'`).
4. **Safe Retention & Eviction Order (Zero RSS 404s)**:
   - Retention policy per source (`keep_count`, `keep_days`, `unpublish_grace_hours` default `48`):
   - Step 1: Mark excess oldest `published` entries as `retiring` and set `unpublished_at = now`. Because RSS only outputs `published` entries, the item disappears from the feed immediately while its file remains downloadable!
   - Step 2: After `unpublish_grace_hours` has elapsed, delete `<OUTPUT_DIR>/<slug>/<video_id>.m4a` (and any per-source artifacts) and mark `source_entries.state = 'evicted'`, `evicted_at = now`.
   - Step 3: `evicted` is terminal for that source — discovery MUST NOT re-enqueue an `evicted` video for that source.
   - Step 4: If NO `source_entries` rows remain in `published` or `retiring` for a `video_id`, set `videos.state = 'purged'` (and clear `media_path`).
   - Support `pinned = 1` on `source_entries` to exempt favorite episodes from automatic eviction.
5. **Daily Bidirectional Reconciliation**:
   - If DB says `published` (and `videos.state == 'ready'`) but the file at `OUTPUT_DIR/<entry_media_path>` is missing: check if another source still has the file (re-hardlink it); otherwise reset `videos.state = 'pending'` so it re-downloads cleanly.
   - Log (and surface in overview/doctor) orphaned audio files on disk not referenced in DB (do NOT auto-delete unknown user files aggressively without explicit cleanup action).

---

## 4. Multi-Output Layer (`podhub/outputs/`)

Keep output generation strictly decoupled from storage and scheduling via an `OutputRenderer` protocol:

```python
class OutputRenderer(Protocol):
    name: str
    content_type: str
    def render_source(self, ctx: RenderSourceContext) -> RenderedOutput: ...
```

### Phase 1 Built-in Outputs
1. **Podcast RSS 2.0 (`outputs/rss.py`)**:
   - Route: `GET /feeds/{feed_token}.xml` (plus optional `HEAD`, returns `ETag` based on rendered SHA-256 hash and supports `If-None-Match` → `304 Not Modified`).
   - Namespaces: `xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"` and `xmlns:podcast="https://podcastindex.org/namespace/1.0"`.
   - **Immutable GUID**: `<guid isPermaLink="false">yt:video:{video_id}</guid>` — NEVER dependent on `BASE_URL`, `feed_token`, `slug`, or DB integer IDs.
   - `BASE_URL` is applied dynamically at render time (so changing VPS domain or rotating `feed_token` seamlessly migrates enclosure URLs without duplicate client downloads).
   - Enclosure: `url="{base_url}/media/{feed_token}/{video_id}.m4a"`, `length="{exact_bytes}"`, `type="audio/mp4"` (never `audio/m4a`).
   - `itunes:duration`: taken from `ffprobe` measured `media_duration`, not YouTube metadata.
   - `itunes:block`: `Yes` (privacy default for self-hosted personal feeds).
   - `X-Robots-Tag: noindex` response header on all public feed/media routes.
   - **Playlist ordering semantics** (configurable per source in `policy_json.feed_order`):
     - `published_desc` (default for channels): ordered by `published_at DESC`, real video upload date as `pubDate`.
     - `playlist_serial` (optional for course/series playlists): `<itunes:type>serial</itunes:type>`, `itunes:episode` set from `sort_key`, monotonic synthetic `pubDate` if needed so clients play in order.
   - **Feed item cap**: configurable (default `max_feed_items = 300`) so huge channels don't produce multi-megabyte XML that chokes podcast clients (older items remain on disk if within retention).
   - **XML 1.0 Sanitization**: Strip illegal XML 1.0 control characters (`[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]`) from titles and descriptions; escape `]]>` safely.
   - **Artifact integration hook**: If a video has ready artifacts registered in `video_artifacts` (e.g., future `kind = 'transcript_vtt'`), the RSS renderer automatically emits `<podcast:transcript url="..." type="text/vtt" rel="captions" />` when served.
2. **Public Media & Cover Serving (`web/routes/public.py`)**:
   - `GET /media/{feed_token}/{filename}` and `HEAD /media/{feed_token}/{filename}`:
     - Validates `feed_token` belongs to a source AND that the video is `published` or `retiring` for that source (prevents arbitrary file traversal or enumeration!).
     - **Full HTTP `Range` (`206 Partial Content`, `Accept-Ranges: bytes`, `Content-Range`) and `HEAD` support** (strictly required by Apple Podcasts!). Starlette's `FileResponse` handles Range/HEAD properly — write an integration test verifying `206`, `Content-Range`, `HEAD`, and `404` on invalid/evicted files.
   - `GET /covers/{feed_token}/{filename}`: serves source cover image with long cache headers (safe because filename is content-hashed `cover.<hash>.jpg`).
   - `GET /artifacts/{feed_token}/{filename}`: serves registered public sidecar artifacts (future post-processing outputs).
3. **OPML Export (`outputs/opml.py`)**:
   - Admin endpoint `GET /admin/export/opml` exporting all enabled sources' RSS feed URLs as valid OPML 2.0 for 1-click import into podcast clients.

---

## 5. Discovery Engine (`podhub/core/discovery.py`)

### Source Kinds
- `channel`: YouTube channel (accepts URL, `@handle`, or `UC...` ID). On creation, resolve and store canonical `external_id` (`UC...`).
- `playlist`: YouTube playlist (`PL...`, `UU...`, etc.). Resolve and store canonical `external_id`.
- `manual`: User-curated collection ("Listen Later" / ad-hoc single videos). Skips automated discovery; user pastes arbitrary YouTube video URLs via panel or CLI.

### Dual-Channel Discovery Algorithm
1. **Fast Path — Official YouTube Atom RSS** (for `kind = 'channel'` without cookie requirement):
   - Default RSS URL:
     - If `policy.include_shorts` is `False` and `external_id` starts with `UC`: try `https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{external_id[2:]}` (long-form uploads only); if that returns 404/error, fall back to `https://www.youtube.com/feeds/videos.xml?channel_id={external_id}`.
     - Otherwise use `channel_id={external_id}`.
   - Send `If-None-Match` (`http_etag`) and `If-Modified-Since` (`http_last_modified`). On `304 Not Modified`, mark check succeeded with 0 new items immediately!
   - Parse exact `<published>` timestamp, `yt:videoId`, title, thumbnail.
   - **Auto-fallback**: If RSS returns HTTP error (404/5xx/timeout) or malformed XML, fall back to Deep Path with `--playlist-end 30` and log a warning.
2. **Deep Path — `yt-dlp` Streaming `flat-playlist`**:
   - Used for: (a) initial onboarding / preview, (b) `playlist` sources (playlist RSS only returns first 15 items and misses tail additions), (c) cookie-backed sources, (d) RSS fallback, (e) explicit backfill, (f) low-frequency weekly reconciliation.
   - Run shim in `list` mode (`--flat-playlist --lazy-playlist`, with `youtubetab:approximate_date` extractor arg) streaming JSONL entries one by one.
   - **Incremental Early Termination**: During normal incremental polling (non-initial, non-backfill), once **K consecutive** (`K = 10`, configurable) already-known `video_id`s for this source are seen, cleanly terminate the subprocess! (Note: for playlists where new items may be appended at the end vs prepended, support `policy.playlist_scan_mode = 'head_early_stop' | 'full_flat'` with sensible default `full_flat` capped by `max_scan_items`, e.g. 500).
3. **Never Delete on Missing**:
   - If a previously seen video is absent from a discovery response, DO NOT delete or evict it (YouTube listings jitter and truncate).
4. **Initial Onboarding & Filtering**:
   - On first sync (`last_ok_at IS NULL`):
     - Discover entries, evaluate source filter policy (`min_duration_s`, `max_duration_s`, `include_shorts`, `include_live_archives`, `title_include_regex`, `title_exclude_regex`).
     - Note: `flat-playlist` metadata may be incomplete (e.g., missing exact `duration` or `live_status`). Only apply filters whose inputs are reliably present during discovery; defer uncertain checks to the shim's in-process `probe` gate!
     - Among eligible entries, mark the most recent `policy.initial_backfill_count` (default `3`) as `wanted` (and ensure corresponding `videos` row exists in `pending`), and mark older entries as `candidate` (not downloaded unless user promotes or triggers backfill).
   - **Policy Re-evaluation**: Incrementing `sources.policy_rev` when a user edits filter rules triggers re-evaluation of `filtered` and `candidate` entries (never touching `evicted`, `retiring`, or `published`).
5. **Per-Source Adaptive Intervals + Jitter**:
   - Each source has `check_interval_s` (e.g., 1800s default for RSS channels, 3600s for playlists) + random jitter (`±15%`) so sources never thunder-herd on the exact same second.

---

## 6. Persistent Queue & Isolated Subprocess Shim

### Single-Source-of-Truth Reconcile Loop (`podhub/core/scheduler.py`)
Run a single-process `asyncio` scheduler with a 5-second tick (no APScheduler/Celery/Redis needed):
1. **Startup Crash Recovery** (before starting ticks):
   - Reset any `videos` stuck in `running` back to `pending` (clearing `running_since`, NOT incrementing `attempt_count`), and delete stale `STATE_DIR/tmp/*` directories.
   - Ensure `STATE_DIR`, `OUTPUT_DIR`, `STATE_DIR/tmp`, `STATE_DIR/cookies`, `STATE_DIR/backups` exist with proper permissions.
2. **Discovery Dispatcher** (concurrency `1`):
   - Skip if `meta.paused == '1'`.
   - Pick sources where `enabled = 1 AND kind != 'manual' AND next_check_at <= now`.
3. **Download Dispatcher** (configurable `max_concurrent`, default `1`):
   - Check gates before claiming work:
     - Global pause (`meta.paused == '1'`) → skip.
     - Disk space preflight check (`shutil.disk_usage(output_dir).free < min_free_gb` or `state_dir`) → trip `local` breaker & pause!
     - `global` or `toolchain` circuit breaker `open` / `manual_hold` → skip (if `open` and `open_until <= now`, transition to `half_open` and allow 1 probe/attempt).
     - `min_launch_gap_s + random(0, launch_jitter_s)` since last download launch → wait.
     - Optional `daily_download_cap` reached → wait until next UTC day (0 = unlimited).
   - **Atomic Claim** (`BEGIN IMMEDIATE`):
     - Select eligible `videos` where:
       - `state IN ('pending', 'deferred', 'unavailable')`
       - `(next_attempt_at IS NULL OR next_attempt_at <= now)`
       - Has at least one `source_entries` row with `state = 'wanted'` belonging to an `enabled = 1` source whose `cookie_profile_id` (if set) does NOT have an open `cookie:<id>` breaker.
     - Order by: `priority DESC` (`manual=100`, `new=50`, `retry=30`, `backfill=10`), then **per-source round-robin fairness** (least recently attempted source first within same priority), then `published_at DESC`.
     - Atomically `UPDATE videos SET state='running', running_since=now WHERE video_id=? RETURNING *`.
4. **Post-Process Dispatcher** (decoupled, concurrency `1`):
   - Polls `postprocess_tasks` where `state = 'pending'` and runs registered `PostProcessor` implementations without touching download slots or video readiness.
5. **Maintenance Jobs** (scheduled by timestamps in `meta`):
   - Retention sweep (every 15m): transition excess `published` → `retiring`, and expired `retiring` → `evicted` + delete files.
   - Daily bidirectional disk/DB reconciliation & `VACUUM INTO` DB backup (keep 7) & `attempts` table pruning (keep 30 days / max 10k rows).

### Subprocess Shim (`podhub/ytdlp/shim.py`) — Exact Protocol

**Crucial invariant**: `podhub/ytdlp/shim.py` MUST only import Python stdlib + `yt_dlp` so it can be executed directly by the configured `ytdlp_python` interpreter (even if `yt-dlp` lives in a separate Nix package/venv from the web app).

Launch shim in its own process group (`start_new_session=True`), passing JSON request via stdin or CLI args, reading **JSON Lines** from stdout:

#### Request Modes
1. `doctor`: emit `hello` (versions of `yt_dlp`, `yt_dlp_ejs` if importable, Python, `ffmpeg`, JS runtime check) and exit 0.
2. `resolve`: resolve a source URL / `@handle` / `UC...` / playlist URL into canonical `(kind, external_id, title, normalized_url, cover_url)`.
3. `list`: run `extract_info(url, download=False, process=False)` / lazy flat-playlist iteration, emitting `{"type": "entry", ...}` line by line so parent can stop cleanly after K consecutive known IDs.
4. `fetch`: **single invocation combining probe + gate + download**:
   ```python
   ydl = YoutubeDL(params)  # single instance; ignore_no_formats_error=True, noplaylist=True, ignoreconfig=True
   raw = ydl.extract_info(canon_url, download=False, process=False)
   emit({"type": "probe", "info": pick_probe_fields(raw)})
   gate_result = evaluate_gate(raw, policy)
   if gate_result.action != "proceed":
       emit({"type": "gate", "action": gate_result.action, "reason": gate_result.reason, "release_timestamp": ...})
       sys.exit(0)
   info = ydl.process_ie_result(raw, download=True)
   # Capture final output path via post_hooks (or requested_downloads[-1]['filepath'])
   emit({"type": "result", "filepath": final_path, "info": pick_result_fields(info)})
   ```

#### JSONL Event Types Emitted by Shim
- `{"type": "hello", "ytdlp_version": "...", "python_version": "...", "ejs_available": true, "js_runtime": "..."}`
- `{"type": "phase", "phase": "extract" | "gate" | "download" | "postprocess"}`
- `{"type": "probe", "info": {"id", "title", "description", "duration", "upload_date", "timestamp", "release_timestamp", "live_status", "availability", "thumbnail", "channel_id", "formats_count", "_type"}}`
- `{"type": "gate", "action": "defer" | "filter" | "skip", "reason": "...", "release_timestamp": 1234567890}`
- `{"type": "progress", "downloaded_bytes": 12345, "total_bytes": 67890, "speed": 102400}`
- `{"type": "warning", "msg": "..."}` (captured via custom yt-dlp `logger`! Crucial for detecting cookie invalidation warnings)
- `{"type": "result", "filepath": "/tmp/.../abc.m4a", "info": {...}}`
- `{"type": "error", "phase": "extract" | "download" | "postprocess", "exc_class": "...", "expected": true, "http_status": 403, "msg": "...", "traceback_tail": "..."}`

#### Audio Extraction & Validation Details
- Default format selector: `bestaudio[ext=m4a]/bestaudio/best`.
- If downloaded stream is already M4A/AAC, **remux only** (with `-movflags +faststart` via ffmpeg postprocessor, near-zero CPU). If non-M4A (e.g. opus/webm), transcode to AAC (`audio/mp4`, 128k) with `faststart`.
- Always pass `--no-playlist`, `--ignore-config`, `--socket-timeout 30`, `--extractor-retries 1`, `--fragment-retries 5`.
- Never do cross-phase retries inside shim: **1 shim invocation = 1 attempt**.
- **Parent Watchdog**:
  - Kill entire process group (`os.killpg(proc.pid, SIGTERM)` then `SIGKILL` after 5s) if:
    - `probe_timeout_s` (default 120s) exceeded before `probe` event;
    - `stall_timeout_s` (default 180s) elapsed with zero `downloaded_bytes` increase during `download`;
    - `total_timeout_s = max(1800, int(duration * 2.0) + 300)` exceeded.
- **Post-Download `ffprobe` Validation**:
  - Verify file exists, size >= 16 KB, audio stream present, `media_duration > 0`.
  - If YouTube reported `duration >= 60` and `abs(media_duration - duration) / duration > 0.05`, classify as `corrupted_artifact`.
- **Cookie Isolation**:
  - If source has a `cookie_profile_id`, copy `<STATE_DIR>/cookies/<profile_id>.txt` to `<STATE_DIR>/tmp/<attempt_id>/cookies.txt` (`0600`) before passing to shim so `yt-dlp`'s cookie write-back can never corrupt the master file.

---

## 7. Error Taxonomy, Retry & Circuit Breaker Matrix (`podhub/errors/`)

Every failed attempt is classified into a structured triple:
**`Classification(category, scope, confidence, retryable, reason)`**
- `scope`: `video` | `global` | `toolchain` | `cookie` | `local`
- `confidence`: `high` | `medium` | `low`

Low-confidence errors MUST take the conservative action (never permanently skip; never immediately trip a global breaker without corroboration).

### Complete Classification Matrix

| # | `category` | Typical Signals / Phase | `scope` | Action & Backoff | Counts Toward Video Budget |
| --- | --- | --- | --- | --- | --- |
| 1 | `transient_network` | Timeout, socket reset, DNS/TLS error, HTTP 5xx, fragment drop, watchdog stall/timeout | `video` | Exponential backoff + ±20% jitter: `2m → 10m → 30m → 2h → 6h` (max 5 attempts). Keep `.part` files on retry. | Yes (max 5) |
| 2 | `download_403` | `phase == 'download'` and `http_status == 403` | `video` (escalatable) | Clear `.part`, retry fresh extraction after `2m → 15m → 1h` (max 3). Escalates to `global`/`toolchain` if ≥3 distinct sources hit within 10m. | Yes (max 3) |
| 3 | `rate_limit_or_bot` | HTTP 429, "Sign in to confirm you're not a bot", "rate-limited", "try again later" | `global` | Trip `global` cooldown breaker (`30m → 2h → 6h → manual_hold` after 3 trips/24h). Return video to `pending` WITHOUT consuming budget. Notify on breaker open. | No |
| 4 | `toolchain_broken` | nsig/signature extraction failed, "Unable to extract", missing JS runtime / ffmpeg, Python traceback with "please report", `ytdlp_version < min_ytdlp_version`, or ≥3 distinct sources failing with "Requested format is not available" in 10m | `toolchain` | Trip `toolchain` breaker storing current `ytdlp_version` in `context_json`. Pause downloads & high-priority alert. **Auto-recovery**: if `hello` (via doctor/periodic check) sees a newer `ytdlp_version`, auto-transition to `half_open`. | No |
| 5 | `cookie_invalid` | Shim `warning` ("cookies are no longer valid") or auth error when attempt USED a cookie | `cookie` | Trip `cookie:<id>` breaker & mark `cookie_profiles.status = 'expired'`. Blocks ONLY sources bound to that cookie; unauthenticated sources continue normally. | No |
| 6 | `auth_required` | "members-only", "Join this channel", "confirm your age", "Sign in" when attempt had NO cookie | `video` | If source has an active cookie profile not yet tried, retry with cookie; else mark `unavailable(reason='auth_required')` (no auto-retry). | No |
| 7 | `not_ready` | `live_status IN ('is_upcoming', 'is_live', 'post_live')`, "Premieres in", "live event will begin", live archive processing | `video` | Set `videos.state = 'deferred'`. If `release_timestamp` known, `next_attempt_at = release_timestamp + 900s`; else `30m → 1h → 2h`. Max defer window 14 days, then `unavailable`. **Never alarms.** | No |
| 8 | `permanent_unavailable` | Video deleted, copyright takedown, account terminated, DRM protected | `video` | `videos.state = 'unavailable'`, `next_attempt_at = NULL` (no auto-retry; manual retry still allowed in UI). | — |
| 9 | `soft_unavailable` | Private video, generic "Video unavailable", geo-blocked | `video` | `videos.state = 'unavailable'` with low-frequency `recheck_at`: `+1d`, then `+7d`, then `+30d`, then stop. Handles videos temporarily privated or later unblocked. | — |
| 10 | `degradable_postprocess` | Thumbnail download/embed failed, metadata tag warning while audio is valid | `video` | If `ffprobe` validates audio, **succeed** and record warning in `attempts`. Never block publishing! | No |
| 11 | `corrupted_artifact` | `ffprobe` failure, unreadable file, duration mismatch > 5%, file < 16 KB | `video` | Wipe temp dir, retry clean download once (`10m`), second failure → `failed`. | Yes (max 2) |
| 12 | `local_resource` | Disk free < `min_free_gb`, `ENOSPC`, `EACCES`, exit code 137 (OOM) | `local` | Set `meta.paused = '1'` (or trip `local` breaker), stop all downloads immediately, high-priority alert. | No |
| 13 | `unclassified` | No rule matched / unexpected internal error | `video` | **Never permanently skip!** Conservative retry `10m → 1h → 6h` (max 3), then `failed` tagged `unclassified` so admin sees it in dedicated UI tab. | Yes (max 3) |

### Anti-Loop & Anti-Storm Guarantees
- **Hard Video Deadline (`give_up_at`)**: Set `give_up_at = first_enqueued_at + 14 days` (configurable). Regardless of category, once past `give_up_at`, transition to `failed` (manual intervention).
- **10-Minute Cross-Source Window Correlation**: Query `attempts` over last 10 minutes: if ≥ 3 **distinct `source_id`s** fail with `download_403` / `rate_limit_or_bot` / `format_unavailable`, escalate scope to `global` or `toolchain` breaker!
- **Alert Deduplication**: Send webhook alerts ONLY on circuit breaker state transitions (`closed → open`, `open → manual_hold`, `open → closed`), cookie expiration, or `local` pause — NEVER per-episode spam!

---

## 8. Decoupled Post-Processing Extension Architecture (`podhub/postprocess/`)

Design a clean, minimal, zero-overhead extension seam so future features (Whisper/Cloudflare VTT transcription, AI show-notes summarization, SponsorBlock cutting, loudness normalization) can be added as standalone plugins without modifying discovery, download queue, or error classification.

### Interface Design
```python
@dataclass(frozen=True)
class PostProcessContext:
    video_id: str
    audio_path: Path                 # Validated ready audio file
    artifacts_dir: Path              # e.g. <OUTPUT_DIR>/<slug>/artifacts/
    metadata: dict[str, Any]         # Video metadata

@dataclass(frozen=True)
class ArtifactResult:
    kind: str                        # e.g. "transcript_vtt", "summary_md", "chapters_json"
    rel_path: str                    # Relative to OUTPUT_DIR
    mime_type: str
    public: bool = True              # Whether outputs (like RSS) may link to it

class PostProcessor(Protocol):
    name: str
    def is_enabled_for_source(self, source_policy: dict[str, Any]) -> bool: ...
    async def process(self, ctx: PostProcessContext) -> list[ArtifactResult]: ...
```

### Integration Semantics
1. When a video finishes downloading and enters `videos.state = 'ready'`, the core downloader calls `postprocess_worker.enqueue_for_video(video_id, source_id)` which inspects registered processors in `PostProcessorRegistry`.
2. If no processors are registered/enabled (the default in V2 MVP), this is an instant no-op!
3. If processors are registered, rows are inserted into `postprocess_tasks(video_id, processor_name, state='pending')` and executed asynchronously by the independent post-process loop.
4. Outputs are persisted in `video_artifacts` and written atomically into `<OUTPUT_DIR>/<source_slug>/artifacts/`.
5. **Failure isolation**: A post-processor failure NEVER reverts `videos.state` from `ready` and NEVER removes the audio from the RSS feed.
6. Write unit tests with a `DummyPostProcessor` proving: enqueueing on ready, artifact creation, failure isolation, and RSS `<podcast:transcript>` rendering when a `transcript_vtt` artifact exists.

---

## 9. Database Schema (SQLite WAL, 9 Focused Tables)

Configure SQLite on every connection:
`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`

```sql
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
    entry_media_path TEXT,            -- relative to OUTPUT_DIR: <slug>/<video_id>.m4a
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
    rel_path TEXT NOT NULL,           -- relative to OUTPUT_DIR
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
```

Add proper indexes for hot paths:
- `INDEX idx_videos_sched ON videos(state, next_attempt_at, priority DESC)`
- `INDEX idx_entries_source_state ON source_entries(source_id, state, published_at DESC)`
- `INDEX idx_entries_video ON source_entries(video_id, state)`
- `INDEX idx_attempts_corr ON attempts(ended_at, category, source_id)`
- `INDEX idx_attempts_video ON attempts(video_id, id DESC)`

---

## 10. Configuration (`config.toml` + Env Overrides)

Load from `PODHUB_CONFIG` path (TOML) with optional `PODHUB_*` env overrides (especially for secrets/URLs):

```toml
[paths]
state_dir = "/var/lib/pod-hub"
output_dir = "/srv/podcasts"

[web]
listen_host = "127.0.0.1"
listen_port = 8000
base_url = "https://podcast.example.com"

[toolchain]
ytdlp_python = "python3"           # Python interpreter that can `import yt_dlp`
ffmpeg_path = "ffmpeg"
ffprobe_path = "ffprobe"
js_runtime = "deno"                # Passed to yt-dlp js_runtimes if non-empty
min_ytdlp_version = "2025.01.01"
ytdlp_proxy = ""                   # External proxy URL if needed (handled externally)
ytdlp_extractor_args = ""          # Optional escape hatch

[pacing]
max_concurrent_downloads = 1
min_launch_gap_s = 20
launch_jitter_s = 25
daily_download_cap = 0             # 0 = unlimited; set e.g. 150 to cap daily fetches
default_check_interval_s = 1800
playlist_check_interval_s = 3600

[watchdog]
probe_timeout_s = 120
stall_timeout_s = 180
total_timeout_base_s = 1800
total_timeout_duration_factor = 2.0

[retry]
give_up_after_days = 14
transient_backoff_s = [120, 600, 1800, 7200, 21600]
download_403_backoff_s = [120, 900, 3600]
unclassified_backoff_s = [600, 3600, 21600]
soft_unavailable_recheck_days = [1, 7, 30]

[breaker]
correlation_window_s = 600
correlation_distinct_sources = 3
cooldown_steps_s = [1800, 7200, 21600]
max_trips_per_24h = 3

[defaults]
audio_format = "bestaudio[ext=m4a]/bestaudio/best"
initial_backfill_count = 3
include_shorts = false
include_live_archives = true
min_duration_s = 60
max_duration_s = 0                 # 0 = no limit
keep_count = 0                     # 0 = keep all
keep_days = 0                      # 0 = keep forever
unpublish_grace_hours = 48
max_feed_items = 300

[disk]
min_free_gb = 2.0

[notify]
webhook_url = ""                   # Optional JSON POST webhook (or Telegram/ntfy compatible)
webhook_headers_json = ""
```

---

## 11. Simple Management Panel & CLI

### Web Panel (`GET /admin/...`)
Clean, responsive, minimal dark/light CSS + HTMX partial updates, **4 focused pages**:
1. **Overview (`/admin`)**:
   - Global status banner (`Running` / `Paused` + Pause/Resume button);
   - Circuit breaker cards (`global`, `toolchain`, `local`, active `cookie:*`) showing state, reason, `open_until`, and Reset button;
   - Toolchain & System Health card (`yt-dlp`, `ffmpeg`, `js_runtime` versions, Run Doctor Check button, disk free space, **time since last successful download**, queue counters);
   - Recent attempts feed (last 20 attempts).
2. **Sources (`/admin/sources`)**:
   - **Add Source with Live Preview**:
     - Step 1: Paste URL / `@handle` / `UC...` / playlist URL (or create a `manual` source) + initial filter rules → click **Preview**.
     - Step 2: Shows resolved canonical ID, title, cover, and dry-run table of ~20 recent videos showing `Included` vs `Filtered (reason)` → user adjusts `initial_backfill_count` and clicks **Confirm & Add**.
   - Source table: title, kind, slug/folder, status, last check, published/wanted/failed counts, Copy RSS URL, quick Sync Now / Pause / Resume.
   - **Source Detail (`/admin/sources/{id}`)**:
     - Feed URL + Rotate Token button;
     - Edit filter & retention policy (increments `policy_rev` and re-evaluates `filtered`/`candidate`);
     - Trigger Sync Now, Trigger Backfill (+N oldest candidates), Add Single Video URL (works on `manual` or any source!);
     - Episode table for this source with Pin/Unpin, Promote `candidate` → `wanted`, Skip, Retry;
     - Delete Source modal with explicit choice: **Keep files on disk** vs **Delete source folder on disk**.
3. **Videos & Queue (`/admin/videos`)**:
   - Status filter tabs: `Active (running/pending)`, `Deferred (live/upcoming)`, `Failed`, `Unclassified`, `Unavailable`, `Ready`.
   - **Bulk Action**: "Retry all in category `<X>`" button (high-leverage operational action!).
   - Video detail view: metadata, channel folder path, status, `next_attempt_at`, `give_up_at`, full `attempts` timeline (phase, category, scope, confidence, exit_code, http_status, yt-dlp version, error excerpt, warnings), single actions (Retry Now, Prioritize, Skip/Mark Unavailable).
4. **Cookies & Config (`/admin/cookies`)**:
   - Create/update/delete Cookie Profile (writes `0600` file in `STATE_DIR/cookies/`);
   - **Validate Cookie** button (runs lightweight shim probe with that cookie; if valid, marks `active`, closes `cookie:<id>` breaker, unblocks dependent videos);
   - Read-only view of effective config (with secrets redacted) + OPML download link.

### CLI (`pod-hub`)
Share the exact same service layer as the Web UI:
- `pod-hub serve [--config /path/to/config.toml]`
- `pod-hub doctor [--config ...]`
- `pod-hub pause / resume`
- `pod-hub sources list / add / sync <id>`
- `pod-hub videos retry [--category <cat> | --video-id <id>]`
- `pod-hub breakers list / reset <key>`

---

## 12. NixOS First-Class Packaging (`flake.nix`)

Provide a production-ready `flake.nix` (using `nixpkgs` flake input, supporting `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`, `aarch64-darwin`):

1. **`devShells.<system>.default`**:
   - Python 3.12 + editable dev dependencies (`fastapi`, `uvicorn`, `jinja2`, `python-multipart`, `httpx`, `pytest`, `pytest-asyncio`, `yt-dlp`, etc.) + `pkgs.ffmpeg` + `pkgs.deno` + `pkgs.sqlite`.
2. **`packages.<system>.default`**:
   - Builds `pod-hub` via `python312Packages.buildPythonApplication` (or `pkgs.python312.pkgs.buildPythonApplication`) with `pyproject.toml`.
   - Exposes `bin/pod-hub`.
   - Does NOT hardcode immutable tool paths when overridden by config; defaults cleanly to wrapped `pkgs.ffmpeg`, `pkgs.deno`, and a Python environment containing `pkgs.python312Packages.yt-dlp` (or `pkgs.yt-dlp` python env).
3. **`nixosModules.default`** (`services.pod-hub`):
   - Options:
     - `enable` (`bool`, default `false`)
     - `package` (`package`, default `self.packages.${pkgs.system}.default`)
     - `user` (`str`, default `"pod-hub"`), `group` (`str`, default `"pod-hub"`)
     - `listenAddress` (`str`, default `"127.0.0.1"`), `port` (`port`, default `8000`)
     - `baseUrl` (`str`, default `"http://127.0.0.1:8000"`)
     - `stateDir` (`path`, default `"/var/lib/pod-hub"`)
     - `outputDir` (`path`, default `"/var/lib/pod-hub/output"` — user easily sets e.g. `"/srv/podcasts"`)
     - `ytdlpPackage` (`package`, default `pkgs.yt-dlp` — easily overridden to `pkgs-unstable.yt-dlp`!)
     - `ffmpegPackage` (`package`, default `pkgs.ffmpeg`)
     - `jsRuntimePackage` (`nullOr package`, default `pkgs.deno`)
     - `environmentFile` (`nullOr path`, default `null`)
     - `settings` (`attrs`, rendered via `pkgs.formats.toml` to `/etc/pod-hub/config.toml` or Nix store config file)
   - Creates system user/group if using defaults, ensures `stateDir` & `outputDir` via `systemd.tmpfiles.rules`.
   - Defines `systemd.services.pod-hub`:
     - `after = [ "network-online.target" ]`, `wants = [ "network-online.target" ]`, `wantedBy = [ "multi-user.target" ]`
     - `Restart = "on-failure"`, `RestartSec = 5`, `TimeoutStopSec = 30`
     - Systemd hardening: `NoNewPrivileges = true`, `PrivateTmp = true`, `ProtectSystem = "strict"`, `ProtectHome = true`, `ReadWritePaths = [ cfg.stateDir cfg.outputDir ]`.
   - Note: Because `yt-dlp` in nixpkgs wraps its own Python interpreter, document and wire `ytdlp_python` cleanly in the NixOS module (e.g., constructing a tiny `python3.withPackages (ps: [ ps.yt-dlp ])` or using `ytdlpPackage`'s python interpreter so `shim.py` can always `import yt_dlp` reliably on NixOS!).

---

## 13. Testing & Verification Checklist

Before declaring completion, you MUST write comprehensive unit and integration tests and verify everything passes:
1. **Error classifier & breaker tests**:
   - Verify all 13 categories map to exact `(category, scope, confidence)` triples.
   - Verify `unknown` never permanently skips and sets `unclassified = 1`.
   - Verify `global` breaker trips on 429/bot-check and escalates to `manual_hold` after `max_trips_per_24h`.
   - Verify `toolchain` breaker records `ytdlp_version` and auto-transitions to `half_open` when a newer version is reported by `hello`.
   - Verify `cookie:<id>` breaker ONLY blocks sources using that cookie, leaving other sources running.
   - Verify 10-minute cross-source correlation upgrades scope when ≥3 distinct sources fail.
2. **Queue, state machine & crash recovery tests**:
   - Atomic `BEGIN IMMEDIATE` claiming, priority ordering (`manual > new > retry > backfill`), per-source round-robin fairness.
   - Startup recovery resets `running` → `pending` and cleans `tmp/`.
   - `give_up_at` hard deadline stops infinite loops.
3. **Shim & runner tests** (mocked/stubbed subprocess + fake `yt_dlp` module):
   - Single invocation `probe` → `gate` (`defer` on `is_upcoming`/`is_live`, `filter` on duration, `proceed` on normal video).
   - Watchdog kills stalled/timed-out process group cleanly.
   - Cookie copy isolation (modifying temp cookie never mutates master `0600` file).
4. **Storage, hardlink dedup & retention tests**:
   - Atomic publish to `<OUTPUT_DIR>/<source_slug>/<video_id>.m4a`.
   - Cross-source publishing creates hardlink (same inode) and deleting Source A leaves Source B's file intact.
   - Retention grace period: `published` → `retiring` (removed from RSS, file still downloadable!) → after grace period `evicted` (file deleted, discovery does NOT re-enqueue).
5. **Outputs & HTTP tests**:
   - RSS 2.0 validity, immutable GUID (`yt:video:<id>`), `BASE_URL` change doesn't change GUID, XML 1.0 control character stripping, `ETag`/`304`, playlist serial ordering.
   - Public media route supports `HEAD`, `Range: bytes=0-1023` (`206 Partial Content` + `Content-Range`), rejects invalid tokens and `evicted` videos with `404`.
6. **Post-processing extension tests**:
   - `DummyPostProcessor` runs asynchronously after `ready`, writes artifact to `<OUTPUT_DIR>/<slug>/artifacts/`, exposes `<podcast:transcript>` in RSS, and a failing processor never breaks video `ready` state or RSS.
7. **Admin Web UI tests**:
   - Overview, Source Add Preview & Confirm, Source Detail actions, Videos status tabs & bulk retry by category, Cookie create & validate, `/healthz`.
8. **Nix evaluation check**:
   - Verify `nix flake check` (or `nix eval` on packages & nixosModule if sandbox/network constrained) succeeds cleanly with valid syntax.

---

## 14. Implementation Order Recommendation

1. Clean up branch `v2` (remove Docker files & legacy V1 code), set up `pyproject.toml`, `flake.nix`, and `podhub/` skeleton.
2. Implement `config.py`, `domain/`, `db/` (WAL connection, migrations, repositories) + DB tests.
3. Implement `storage/` (paths, atomic publish, hardlink dedup, cookies) + storage tests.
4. Implement `ytdlp/` (`shim.py`, `runner.py`, `rss.py`, `ffprobe.py`) + shim/watchdog tests.
5. Implement `errors/` (`taxonomy.py`, `retry.py`, `breaker.py`) + error/breaker tests.
6. Implement `core/` (`discovery.py`, `downloader.py`, `retention.py`, `scheduler.py`, `notifications.py`, `services.py`) + `postprocess/` skeleton.
7. Implement `outputs/` (`rss.py`, `opml.py`) and `web/` (public routes with Range/HEAD + 4-page SSR/HTMX admin UI) + `cli.py`.
8. Finalize `flake.nix` (`packages.default` + `nixosModules.default`) and `README.md`.
9. Run full `pytest` suite and Nix evaluation checks, fix any issues, and commit cleanly on branch `v2`.
