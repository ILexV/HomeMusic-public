# Build context: repository root (see docker-compose.yml).

# --- PWA ---------------------------------------------------------------------------------
FROM node:22.20.0-bookworm-slim AS web
WORKDIR /src/web
COPY web/package.json web/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- Deno (JS runtime yt-dlp needs for YouTube) --------------------------------------------
FROM denoland/deno:bin-2.9.8 AS deno

# --- Hub -------------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg restic ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app
COPY hub/pyproject.toml hub/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY hub/ ./
RUN rm -rf /app/.venv /app/data && uv sync --frozen --no-dev
COPY --from=web /src/hub/app/static /app/app/static
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
