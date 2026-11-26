#!/bin/sh
# Production API entrypoint. Migrations are NOT run here: they are a separate release step
# (`alembic upgrade head`, run once before new app containers start).
set -eu

# Prometheus multi-process mode: each uvicorn worker writes its metrics here; start clean.
if [ -n "${PROMETHEUS_MULTIPROC_DIR:-}" ]; then
  rm -rf "$PROMETHEUS_MULTIPROC_DIR"
  mkdir -p "$PROMETHEUS_MULTIPROC_DIR"
fi

# Workers come from WEB_CONCURRENCY (uvicorn reads it). On SIGTERM uvicorn stops accepting,
# closes WebSockets with 1012 (clients reconnect elsewhere) and waits up to the graceful
# timeout for in-flight requests before exiting.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --no-server-header \
  --timeout-keep-alive 5 \
  --timeout-graceful-shutdown "${GRACEFUL_SHUTDOWN_SECONDS:-20}"
