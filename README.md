# Pod Hub V2

Pod Hub is a self-hosted YouTube-to-podcast service for NixOS. It subscribes to channels and playlists and supports manual collections. It publishes tokenized RSS feeds, audio, artwork, and optional sidecar artifacts.

## Deploy on NixOS

Add this repository as a flake input named `pod-hub` and import `pod-hub.nixosModules.default` into your NixOS configuration.

```nix
{
  services.pod-hub = {
    enable = true;
    baseUrl = "https://podcast.example.com";
    outputDir = "/srv/podcasts";
    environmentFile = "/run/secrets/pod-hub.env";
    settings = {
      defaults.keep_count = 100;
      pacing.daily_download_cap = 150;
    };
  };
}
```

The default listener is `127.0.0.1:8000`. Use a reverse proxy or Tailscale to reach the service. Restrict `/admin` and `/admin/*` to authorized users, including the OPML export. Podcast clients need access to `/feeds`, `/media`, `/covers`, and `/artifacts`. Treat feed URLs as credentials. Rotating a source token revokes its old URLs and preserves its episode GUIDs.

The NixOS module creates the default service account, provisions both directories, and runs the service with systemd hardening. It takes the Python interpreter from `ytdlpPackage` and constructs an environment in which the standalone shim can import that exact yt-dlp version. Set `ytdlpPackage` to a newer nixpkgs package when needed. Override `ffmpegPackage` and `jsRuntimePackage` to select those tools, or set `jsRuntimePackage = null` to disable the runtime. Explicit `settings.toolchain` or environment overrides take precedence over tool defaults.

Write `stateDir`, `outputDir`, and `environmentFile` as quoted absolute path strings. systemd reads the optional environment file at runtime.

## Local development

```sh
nix develop
python -m podhub.cli --help
python -m pytest
nix flake check --all-systems
nix eval --impure --file tests/nixos-module.nix
```

The development shell runs directly from the checkout. To install the `pod-hub` command in editable mode, create a writable virtual environment with `python -m venv --system-site-packages .venv` and run `.venv/bin/python -m pip install --no-build-isolation --no-deps -e .`. Set separate writable directories for local use.

```toml
[paths]
state_dir = "./data/state"
output_dir = "./data/podcasts"

[web]
listen_host = "127.0.0.1"
listen_port = 8000
base_url = "http://127.0.0.1:8000"
```

Run `pod-hub serve --config ./config.toml` or set `PODHUB_CONFIG`. Overrides accept `PODHUB_SECTION__FIELD` or `PODHUB_FIELD`, with the qualified form taking precedence. Use the systemd environment file for values such as `PODHUB_NOTIFY__WEBHOOK_URL`, `PODHUB_NOTIFY__WEBHOOK_HEADERS_JSON`, and `PODHUB_TOOLCHAIN__YTDLP_PROXY`. Use JSON syntax for array and object overrides. The admin panel redacts the proxy and webhook credentials. See the [implementation specification](docs/v2-implementation-spec.md) for configuration defaults.

```sh
pod-hub doctor
pod-hub pause
pod-hub resume
pod-hub sources list
pod-hub sources add @channel --initial-backfill-count 3 --title "城市电台" --slug "東京 音訊"
pod-hub sources add "Listen Later" --kind manual
pod-hub sources sync 1
pod-hub sources add-video 2 'https://www.youtube.com/watch?v=VIDEO_ID'
pod-hub videos retry --category transient_network
pod-hub videos retry --video-id VIDEO_ID
pod-hub breakers list
pod-hub breakers reset global
```

For a bare video ID that starts with `-`, use `sources add-video <source-id> -- <video-id>` or `videos retry --video-id=<video-id>`.

The CLI and admin panel share the same SQLite database and service operations. Run CLI commands for a deployed instance as the service user with the instance's configuration. `sources sync` schedules discovery on the running service's next tick.

## Runtime and storage

Channel discovery normally uses conditional YouTube Atom requests. Initial, playlist, cookie-backed and reconciliation scans use bounded lazy listings. Discovery populates a durable SQLite WAL queue. Download workers claim it atomically and apply pacing, retry budgets, deadlines, and circuit breakers. yt-dlp runs in a dedicated process group and streams JSONL events to a watchdog. FFmpeg produces faststart M4A and ffprobe validates it before publishing.

Temporarily unavailable videos follow `retry.soft_unavailable_recheck_days`. The default delays are 1, 7, and 30 days, after which the video stays unavailable if the last recheck also fails.

Set the podcast title and folder name in the source preview or with `--title` and `--slug` on the CLI. Leave the folder name blank in the preview to regenerate it from the edited title. Pod Hub cleans the title first, then falls back to the channel handle or source ID if needed. The source detail page lets you edit the podcast title in the RSS feed while keeping the existing folder and feed URL.

Folder names support Unicode, including Chinese and Japanese. Pod Hub normalizes them to NFC, collapses whitespace, and limits each name to 120 UTF-8 bytes. Names must be portable across Linux, macOS, and Windows and cannot start with a dot. Collision checks are case-insensitive, and generated names get a numeric suffix when needed.

New audio files use `YYYYMMDD - Title [video_id].m4a` in the source folder. If the date or title is unavailable, Pod Hub omits that part. If both are unavailable, it uses `video_id.m4a`. Filenames are sanitized for portable storage, while the feed displays the original title. Published files retain their names across upstream title changes, and shared audio reuses the same filename when hardlinked to another source. The public media URL uses `/media/{token}/{video_id}.m4a`.

The M4A embeds the original title, channel or uploader, upload date, YouTube link, and description when available. YouTube chapters and a JPEG episode cover are embedded when possible. If FFmpeg cannot embed artwork or metadata, Pod Hub publishes the audio with the remaining supported information. The feed includes the podcast author and links to the original video and supported YouTube episode images.

Each source folder also contains content-hashed channel artwork when available. Retiring episodes leave the feed first and remain downloadable through the grace period. Daily maintenance reports unregistered files and retains seven SQLite backups in the private state directory. The published folders and the private cookie files should be backed up separately if needed.

The post-processing registry starts empty. To extend an embedded instance, register a `PostProcessor` and pass its registry to `Services`. The worker passes a private staging directory as `PostProcessContext.artifacts_dir`. A processor returns file names relative to the final output directory, such as `my-source/artifacts/VIDEO_ID.vtt`, and the worker atomically publishes the staged files. RSS includes public `transcript_vtt` artifacts automatically.
