"""Security middlewares (pure ASGI, so they also see WebSocket scopes and pass them through)."""

import contextlib
import json
from collections.abc import Iterable

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

# JSON API: nothing may load, frame or submit anything.
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# Swagger UI / ReDoc (off in production): their assets come from jsDelivr and they use inline
# scripts, so they get a looser policy than the API, still with no framing.
DOCS_CSP = (
    "default-src 'none'; "
    "script-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
    "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.jsdelivr.net; "
    "font-src https://cdn.jsdelivr.net; worker-src blob:; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
HSTS = "max-age=63072000; includeSubDomains"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.hsts = get_settings().is_production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        csp = DOCS_CSP if scope["path"] in DOCS_PATHS else API_CSP

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() != b"content-security-policy"
                ]
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"cross-origin-opener-policy", b"same-origin"),
                    (b"content-security-policy", csp.encode()),
                ]
                if self.hsts:  # only behind HTTPS in production; never on local http
                    headers.append((b"strict-transport-security", HSTS.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _BodyTooLargeError(Exception):
    pass


class BodySizeLimitMiddleware:
    """413 for oversized request bodies.

    Content-Length is checked before the app runs. Bodies without one (chunked) are counted
    as they stream; if the limit is crossed, whatever the app answers is replaced by a 413.
    Per-endpoint limits (file size, webhook size) still apply on top of this.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    @staticmethod
    def _limit(scope: Scope) -> int:
        s = get_settings()
        content_type = dict(scope.get("headers", [])).get(b"content-type", b"").lower()
        if content_type.startswith(b"multipart/form-data"):
            return s.max_upload_body_bytes
        return s.max_json_body_kb * 1024

    @staticmethod
    async def _reject(send: Send, limit: int) -> None:
        body = json.dumps(
            {
                "error": {
                    "code": "payload_too_large",
                    "message": f"Request body exceeds {limit // 1024} KB",
                    "details": None,
                }
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(scope)
        declared = dict(scope.get("headers", [])).get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > limit
            except ValueError:
                too_big = False
            if too_big:
                await self._reject(send, limit)
                return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded:
                return  # the app's (error) response is replaced by ours
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        with contextlib.suppress(_BodyTooLargeError):
            await self.app(scope, limited_receive, guarded_send)
        if exceeded and not started:
            await self._reject(send, limit)


class ProbeFriendlyTrustedHostMiddleware(TrustedHostMiddleware):
    """Rejects unknown Host headers, except on health probes (load balancers and container
    health checks call those by IP)."""

    exempt_paths = frozenset({"/health", "/health/ready", "/metrics"})  # metrics: token-gated

    def __init__(self, app: ASGIApp, allowed_hosts: Iterable[str]) -> None:
        super().__init__(app, allowed_hosts=list(allowed_hosts))
        self.inner = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in self.exempt_paths:
            await self.inner(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
