import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from app.core.config import Settings
from app.workers.tasks import heartbeat


async def test_liveness(client: AsyncClient) -> None:
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readiness_checks_db_and_redis(client: AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": True, "redis": True}


async def test_unknown_route_uses_error_shape(client: AsyncClient) -> None:
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"error": {"code": "not_found", "message": "Not Found", "details": None}}


async def test_request_id_is_generated_and_echoed(client: AsyncClient) -> None:
    generated = await client.get("/health")
    assert len(generated.headers["x-request-id"]) == 32

    echoed = await client.get("/health", headers={"X-Request-ID": "abc123"})
    assert echoed.headers["x-request-id"] == "abc123"


async def test_openapi_docs_are_served(client: AsyncClient) -> None:
    assert (await client.get("/docs")).status_code == 200
    schema = (await client.get("/openapi.json")).json()
    assert "/health/ready" in schema["paths"]


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [("http://localhost:5173", True), ("https://evil.example.com", False)],
)
async def test_cors_only_allows_configured_origins(
    client: AsyncClient, origin: str, allowed: bool
) -> None:
    resp = await client.options(
        "/health",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert (resp.headers.get("access-control-allow-origin") == origin) is allowed


def test_production_settings_reject_unsafe_values() -> None:
    base = {
        "environment": "production",
        "secret_key": "x" * 40,
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "jwt_secret_key": "y" * 40,
    }
    with pytest.raises(ValidationError, match="DEBUG"):
        Settings(**base, debug=True)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="Wildcard"):
        Settings(**base, cors_origins=["*"])  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="32 characters"):
        Settings(**{**base, "jwt_secret_key": "short"})  # type: ignore[arg-type]


def test_cors_origins_accepts_comma_separated_string() -> None:
    s = Settings(
        secret_key="x",
        database_url="postgresql+asyncpg://u:p@db/x",
        jwt_secret_key="y",
        cors_origins="http://a.com, http://b.com",
    )
    assert s.cors_origins == ["http://a.com", "http://b.com"]


async def test_worker_heartbeat_job() -> None:
    assert await heartbeat({}) == "ok"
