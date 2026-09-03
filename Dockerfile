# ffmpeg, Chromium and a Japanese font are all runtime requirements, not
# conveniences: without them video rendering, PDF export and telop all fail.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
    SNSAUTO_WORKSPACE=/data/workspace \
    SNSAUTO_DB_URL=sqlite:////data/snsauto.db

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-noto-cjk \
        fonts-noto-color-emoji \
        fontconfig \
        ca-certificates \
        curl \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir ".[llm,web,pdf,storage]" \
    && python -m playwright install --with-deps chromium \
    && rm -rf /root/.cache

COPY alembic.ini ./
COPY alembic ./alembic

# Run as a non-root user; the app writes only to /data.
RUN useradd --create-home --uid 10001 snsauto \
    && mkdir -p /data/workspace \
    && chown -R snsauto:snsauto /data /app
USER snsauto

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["snsauto", "serve", "--host", "0.0.0.0", "--port", "8000"]
