# Testing And Verification

## Environment

Project dependencies are managed through `direnv` + the Nix dev shell declared in `flake.nix`.

Initial setup:

```sh
direnv allow
direnv exec . pip install -e '.[dev]'
```

Routine checks:

```sh
direnv exec . pytest -q
direnv exec . yt-dlp --version
direnv exec . ffmpeg -version
```

Notes:

- `direnv exec . pytest -q` is the expected test entry point.
- If `flake.nix` changes, `direnv` will rebuild its cache on the next shell entry.
- If the shell warns about a Python version mismatch, delete `.venv` and enter the shell again.

## Automated Tests

Current unit coverage is limited to:

- slug generation
- sync planning
- feed rendering

Run the suite with:

```sh
direnv exec . pytest -q
```

Expected result at the time this document was updated:

```text
5 passed
```

## Local HTTP Smoke Test

Use a temporary SQLite database and media directory so the verification does not touch production data.

Example startup:

```sh
BASE_URL=http://127.0.0.1:8010 \
MEDIA_DIR=/tmp/podhub-smoke/media \
DATABASE_PATH=/tmp/podhub-smoke/podhub.db \
LOG_LEVEL=INFO \
direnv exec . .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8010
```

Recommended checks:

```sh
curl -i http://127.0.0.1:8010/
curl -i http://127.0.0.1:8010/admin/channels
curl -i http://127.0.0.1:8010/admin/channels/new
curl -i http://127.0.0.1:8010/feeds/<channel-slug>.xml
curl -i http://127.0.0.1:8010/media/<episode-id>.m4a
```

Expected behavior:

- `/` redirects to `/admin/channels`
- channel list/detail pages render HTML successfully
- feed endpoints return RSS XML
- media endpoints return `200` only for `audio_ready` episodes with an existing file
- missing feed or media resources return `404`

If you test form submission with `curl`, use `--form-string` for `@handle` values:

```sh
curl -i -X POST --form-string 'source_input=@TED' http://127.0.0.1:8010/admin/channels
```

Do not use `-F 'source_input=@TED'`, because `curl` treats it as a file upload path.

## Live Downloader Checks

The downloader depends on live YouTube responses, so results can change over time.

Useful checks:

```sh
direnv exec . yt-dlp --flat-playlist --playlist-end 1 --dump-single-json https://www.youtube.com/@TED
direnv exec . yt-dlp --flat-playlist --playlist-end 1 --dump-single-json https://www.youtube.com/playlist?list=PL...
```

This confirms that channel/playlist resolution is working. Full sync and audio download checks are slower and may take several minutes depending on network conditions and the selected video or channel.

Notes:

- `app/downloader/client.py` uses `--flat-playlist --dump-single-json` for channel listing, which can be slow on large channels.
- Live media downloads can leave `.part` files in the temporary media directory while `yt-dlp` is still running.
- A failed live download is not always a code regression; the target video may have become unavailable.

## Docker Verification

OrbStack can be used as the local Docker runtime.

Recommended checks:

```sh
docker build -t pod-hub:local .
docker compose up --build
```

Notes:

- The first image build is slow because the image installs `ffmpeg` from Debian packages.
- Set `YTDLP_JS_RUNTIMES` in the shell before `docker compose up` only when the pinned `yt-dlp` version supports `--js-runtimes` and a matching runtime is available in the image, for example `YTDLP_JS_RUNTIMES=node`.
- Use a mounted throwaway `./data` directory or temporary volume for smoke tests.
- Container verification should include at least one HTTP request to `/admin/channels` after startup.

## Known Limits

- The current automated suite does not cover web routes, scheduler execution, or end-to-end download success.
- Full channel sync validation requires external network access and can be significantly slower than unit tests.
- Live verification should be treated as a smoke test, not as a stable deterministic test.
