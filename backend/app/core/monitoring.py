"""Optional error monitoring with Sentry (enabled by SENTRY_DSN).

PII stays out: no user identity, cookies, bodies, query strings or local variables are
collected, request headers are reduced to an allowlist, and every string in an event goes
through the same scrubbing as the logs (emails, tokens, codes) before it leaves the process.
"""

from typing import Any

import sentry_sdk

from app.core.config import get_settings
from app.core.logging import get_logger, scrub_text

logger = get_logger(__name__)

SAFE_HEADERS = ["user-agent", "content-type", "content-length", "x-request-id", "accept"]


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
    event.pop("user", None)
    request = event.get("request")
    if isinstance(request, dict):
        for key in ("data", "cookies", "query_string", "env"):
            request.pop(key, None)
    return _scrub(event)  # type: ignore[no-any-return]


def init_sentry(component: str) -> bool:
    s = get_settings()
    if s.sentry_dsn is None or not s.sentry_dsn.get_secret_value():
        return False
    sentry_sdk.init(
        dsn=s.sentry_dsn.get_secret_value(),
        environment=s.environment,
        server_name=component,
        traces_sample_rate=s.sentry_traces_sample_rate,
        data_collection={
            "user_info": False,
            "cookies": {"mode": "off"},
            "http_headers": {"request": {"mode": "allowlist", "terms": SAFE_HEADERS}},
            "http_bodies": [],
            "url_query_params": {"mode": "off"},
            "database_query_data": False,
            "stack_frame_variables": False,
            "queues": False,
        },
        before_send=before_send,  # type: ignore[arg-type]
    )
    logger.info("sentry_enabled", component=component)
    return True
