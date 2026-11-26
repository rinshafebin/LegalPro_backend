"""Outbound HTTP for sync SDKs (Razorpay, google-auth): never wait forever on a provider."""

from typing import Any

import requests

from app.core.config import get_settings


def timeouts() -> tuple[float, float]:
    s = get_settings()
    return (s.external_connect_timeout_seconds, s.external_read_timeout_seconds)


class TimeoutSession(requests.Session):
    """A requests session that applies (connect, read) timeouts unless the caller sets one."""

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeouts())
        return super().request(method, url, *args, **kwargs)
