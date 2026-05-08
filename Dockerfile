FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    BASE_URL=http://localhost:8000 \
    FETCH_BATCH_SIZE=10 \
    SYNC_INTERVAL=30 \
    MEDIA_DIR=/data/media \
    DATABASE_PATH=/data/db/podhub.db \
    LOG_LEVEL=INFO \
    YTDLP_BIN=yt-dlp \
    YTDLP_JS_RUNTIMES=node

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg nodejs \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml /app/pyproject.toml
COPY app /app/app
COPY docs /app/docs

RUN pip install --upgrade pip \
    && pip install . \
    && pip install "yt-dlp[default]==2026.3.17"

EXPOSE 8000

VOLUME ["/data"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
