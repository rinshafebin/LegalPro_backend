"""Structured logging (structlog) for app code and stdlib/uvicorn loggers."""

import logging
import re
import sys
import uuid
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings

# Keys whose values are never logged, whatever they contain.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "mfa_token",
        "ticket",
        "code",
        "otp",
        "secret",
        "authorization",
        "cookie",
        "body",
        "email",
        "to",
        "phone",
    }
)
_PATTERNS = (
    (re.compile(r"(ticket|token|code|password)=[^\s\"&]+", re.I), r"\1=[redacted]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.I), "Bearer [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[jwt]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
)
REDACTED = "[redacted]"


def scrub_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_secrets(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Last line of defence: no tokens, passwords, codes, emails or message text in logs.

    Values under sensitive keys are replaced; every string value (the message itself,
    uvicorn's access lines, rendered tracebacks) has emails, JWTs, Bearer tokens and
    `ticket=`/`token=`/`code=` values masked.
    """
    for key, value in list(event_dict.items()):
        if key.lower() in SENSITIVE_KEYS:
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = scrub_text(value)
    return event_dict


def shared_processors() -> list[structlog.types.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]


def build_formatter(json_output: bool) -> structlog.stdlib.ProcessorFormatter:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            redact_secrets,  # again: rendered exceptions can quote SQL parameters
            renderer,
        ],
    )


def configure_logging() -> None:
    settings = get_settings()
    structlog.configure(
        processors=[*shared_processors(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(build_formatter(settings.log_json))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name, **initial)  # type: ignore[no-any-return]


class RequestIdMiddleware:
    """Binds a request id to the log context and echoes it in `X-Request-ID`."""

    header = b"x-request-id"

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = dict(scope.get("headers", [])).get(self.header, b"").decode()
        request_id = incoming if 0 < len(incoming) <= 64 else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((self.header, request_id.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
