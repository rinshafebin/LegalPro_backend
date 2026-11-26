# syntax=docker/dockerfile:1.7

# ---------- Build stage: resolve and install deps with uv ----------
FROM python:3.13.18-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install dependencies first (cached layer), then the project source.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---------- Runtime stage ----------
FROM python:3.13.18-slim AS runtime

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

# Only trust X-Forwarded-For / X-Forwarded-Proto from these proxy IPs (uvicorn reads this env var).
# Set it to the reverse proxy / load balancer address in production, never "*".
# WEB_CONCURRENCY: uvicorn worker processes (about 2 per CPU core is a good start).
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    WEB_CONCURRENCY=2 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus

RUN mkdir -p /tmp/prometheus && chown app:app /tmp/prometheus

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# Production: several workers, no reload, graceful shutdown. Migrations run separately.
CMD ["/app/docker/start-api.sh"]
