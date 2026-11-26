"""Prometheus metrics at GET /metrics, off unless METRICS_TOKEN is set.

Not for the public internet: scrapers must send `Authorization: Bearer <METRICS_TOKEN>`, and
the proxy shouldn't route /metrics at all. With several uvicorn workers, set
PROMETHEUS_MULTIPROC_DIR (the Dockerfile does) so every worker's numbers are combined.
"""

import hmac
import os
import time

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import NotFoundError, UnauthorizedError

REQUESTS = Counter(
    "http_requests_total", "HTTP requests by route template", ["method", "route", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency by route template", ["method", "route"]
)
WS_CONNECTIONS = Gauge(
    "websocket_connections", "Open WebSocket connections", multiprocess_mode="livesum"
)


def _route(scope: Scope) -> str:
    """The route template ("/api/v1/cases/{case_id}"), never the raw path, so label values
    stay bounded. Requests that matched no route all share one label."""
    if scope.get("route") is None:
        return "unmatched"
    segments = str(scope["path"]).split("/")
    for name, value in (scope.get("path_params") or {}).items():
        segments = [f"{{{name}}}" if seg == str(value) else seg for seg in segments]
    return "/".join(segments)


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/metrics":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route(scope)
            REQUESTS.labels(scope["method"], route, str(status)).inc()
            LATENCY.labels(scope["method"], route).observe(time.perf_counter() - start)


router = APIRouter()


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    token = get_settings().metrics_token
    if token is None or not token.get_secret_value():
        raise NotFoundError("Not found")
    expected = f"Bearer {token.get_secret_value()}"
    if not hmac.compare_digest(request.headers.get("authorization", ""), expected):
        raise UnauthorizedError("Invalid metrics token")
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        data = generate_latest(registry)
    else:
        data = generate_latest()
    return Response(data, media_type=CONTENT_TYPE_LATEST)


def mark_process_dead() -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        multiprocess.mark_process_dead(os.getpid())  # type: ignore[no-untyped-call]
