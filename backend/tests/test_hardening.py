"""Phase 7 hardening: headers, hosts, docs switch, body limits, timeouts, log redaction,
Sentry scrubbing, metrics, production settings, DB timeouts."""

import io
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest
import requests
import structlog
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.config import Settings, get_settings
from app.core.logging import build_formatter, shared_processors
from app.core.monitoring import before_send
from app.main import create_app
from tests.helpers import API, make_advocate

# ---------------------------------------------------------------- headers / hosts / docs


async def test_security_headers_on_api_responses(client: AsyncClient) -> None:
    resp = await client.get("/health")
    h = resp.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in h  # production only


async def test_docs_get_their_own_csp(client: AsyncClient) -> None:
    resp = await client.get("/docs")
    assert resp.status_code == 200
    csp = resp.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in csp
    assert "frame-ancestors 'none'" in csp


async def test_hsts_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "environment", "production")
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.headers["strict-transport-security"].startswith("max-age=63072000")


async def test_unknown_host_rejected_but_probes_exempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "allowed_hosts", ["api.example.com"])
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://evil.test") as c:
        assert (await c.get(f"{API}/specializations")).status_code == 400
        assert (await c.get("/health")).status_code == 200
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://api.example.com"
    ) as c:
        assert (await c.get("/health")).status_code == 200


async def test_docs_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "api_docs_enabled", False)
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await c.get(path)).status_code == 404


def test_docs_default_off_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "environment", "production")
    assert s.docs_enabled is False
    monkeypatch.setattr(s, "api_docs_enabled", True)
    assert s.docs_enabled is True


# ---------------------------------------------------------------- body limits


async def test_oversized_json_body_is_rejected(client: AsyncClient) -> None:
    big = b'{"email": "' + b"a" * (1024 * 1024 + 10) + b'"}'
    resp = await client.post(
        f"{API}/auth/login", content=big, headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_oversized_chunked_body_is_rejected(client: AsyncClient) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(20):
            yield b"x" * 100_000  # 2 MB, no Content-Length

    resp = await client.post(
        f"{API}/auth/login", content=chunks(), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"


async def test_uploads_get_the_larger_multipart_limit(client: AsyncClient) -> None:
    # 2 MB multipart passes the body limit (and then fails auth, which is fine here).
    resp = await client.put(
        f"{API}/users/me/avatar", files={"file": ("a.png", b"x" * 2_000_000, "image/png")}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------- outbound timeouts


def test_sdk_sessions_always_have_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_request(self: requests.Session, method: str, url: str, **kwargs: Any) -> None:
        seen.update(kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    http.TimeoutSession().get("https://api.razorpay.com/v1/orders")
    assert seen["timeout"] == (5, 15)


def test_razorpay_google_and_s3_use_timeouts() -> None:
    from app.core.storage import S3Storage
    from app.modules.auth import google_oauth
    from app.modules.payments.gateway import RazorpayGateway

    assert isinstance(RazorpayGateway("k", "s")._client.session, http.TimeoutSession)
    assert isinstance(google_oauth._transport.session, http.TimeoutSession)
    config = S3Storage()._config
    assert (config.connect_timeout, config.read_timeout) == (5, 15)


# ---------------------------------------------------------------- logs never leak


def _capture(json_output: bool = True) -> tuple[structlog.stdlib.BoundLogger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(build_formatter(json_output))
    std = logging.getLogger("redaction-test")
    std.handlers = [handler]
    std.propagate = False
    std.setLevel(logging.INFO)
    log = structlog.wrap_logger(
        std,
        processors=[*shared_processors(), structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    return log, stream


def test_log_redaction_by_key_and_pattern() -> None:
    log, stream = _capture()
    log.info(
        "login attempt for alice@example.com with Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
        password="hunter2",
        code="123456",
        token="tok_live_123",
        body="Settlement offer is 4.2 crore",
        email="bob@example.com",
        user_id="u-1",
    )
    out = stream.getvalue()
    for secret in ("hunter2", "123456", "tok_live_123", "Settlement", "alice@", "bob@", "eyJhbGci"):
        assert secret not in out, secret
    assert "u-1" in out  # ids are fine
    assert "[redacted]" in out
    assert "[email]" in out


def test_stdlib_lines_and_tracebacks_are_scrubbed() -> None:
    log, stream = _capture()
    std = logging.getLogger("redaction-test")
    std.info('1.2.3.4 - "GET /reset?token=abc123&code=999999" 200')
    try:
        raise ValueError("duplicate key value (email)=(carol@example.com)")
    except ValueError:
        log.exception("db_error")
    out = stream.getvalue()
    assert "abc123" not in out
    assert "999999" not in out
    assert "carol@example.com" not in out
    assert "ValueError" in out  # the traceback is still there, only scrubbed


def test_sentry_events_are_scrubbed() -> None:
    event: dict[str, Any] = {
        "user": {"email": "dave@example.com", "ip_address": "1.2.3.4"},
        "request": {
            "url": "https://api/x",
            "data": {"password": "pw"},
            "cookies": {"s": "1"},
            "query_string": "token=abc",
        },
        "exception": {"values": [{"value": "failed for erin@example.com, code=424242"}]},
        "breadcrumbs": {"values": [{"message": "Bearer abc.def.ghi"}]},
    }
    out = before_send(event, {})
    assert out is not None
    assert "user" not in out
    assert set(out["request"]) == {"url"}
    text_ = str(out)
    assert "erin@" not in text_
    assert "424242" not in text_
    assert "abc.def.ghi" not in text_


# ---------------------------------------------------------------- metrics


async def test_metrics_are_token_gated(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    assert (await client.get("/metrics")).status_code == 404  # off by default
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr("m" * 40))
    assert (await client.get("/metrics")).status_code == 401
    wrong = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401

    adv = await make_advocate(db_session, "metric@example.com")
    await client.get(f"{API}/advocates/{adv.id}")
    resp = await client.get("/metrics", headers={"Authorization": f"Bearer {'m' * 40}"})
    assert resp.status_code == 200
    assert 'route="/api/v1/advocates/{advocate_id}"' in resp.text
    assert str(adv.id) not in resp.text  # templates only, never raw ids
    assert "/metrics" not in (await client.get("/openapi.json")).text


# ---------------------------------------------------------------- production settings


def _prod(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": "s" * 40,
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "jwt_secret_key": "j" * 40,
        "field_encryption_key": Fernet.generate_key().decode(),
        "allowed_hosts": ["api.example.com"],
        "frontend_url": "https://app.example.com",
        "cors_origins": ["https://app.example.com"],
        "razorpay_key_id": "rzp_live_x",
        "razorpay_key_secret": "k",
        "razorpay_webhook_secret": "w",
        "s3_access_key": "a",
        "s3_secret_key": "b",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


def test_complete_production_config_is_accepted() -> None:
    s = _prod()
    assert s.docs_enabled is False
    assert s.is_production


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"allowed_hosts": ["*"]}, "ALLOWED_HOSTS"),
        ({"frontend_url": "http://app.example.com"}, "FRONTEND_URL"),
        ({"cors_origins": ["http://app.example.com"]}, "CORS_ORIGINS"),
        ({"razorpay_webhook_secret": ""}, "RAZORPAY"),
        ({"s3_secret_key": ""}, "S3_"),
        ({"metrics_token": "short"}, "METRICS_TOKEN"),
        ({"ws_allow_missing_origin": True}, "WS_ALLOW_MISSING_ORIGIN"),
    ],
)
def test_production_rejects_unsafe_config(override: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _prod(**override)


# ---------------------------------------------------------------- database timeouts


async def test_statement_timeout_is_set_on_connections(db_session: AsyncSession) -> None:
    assert await db_session.scalar(text("SHOW statement_timeout")) == "15s"
    assert await db_session.scalar(text("SHOW idle_in_transaction_session_timeout")) == "1min"


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("METRICS_TOKEN", "SENTRY_DSN", "API_DOCS_ENABLED"):
        monkeypatch.setenv(name, "")
    s = Settings()
    assert s.metrics_token is None
    assert s.sentry_dsn is None
    assert s.api_docs_enabled is None


async def test_empty_metrics_token_never_enables_metrics(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", SecretStr(""))
    assert (await client.get("/metrics", headers={"Authorization": "Bearer "})).status_code == 404
