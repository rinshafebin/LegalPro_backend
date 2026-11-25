import os

# Must run before any `app.*` import so cached settings pick up the test overrides.
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import engine, get_session
from app.core.redis import get_redis
from app.main import app
from app.models import Base


def _assert_safe_test_targets() -> None:
    settings = get_settings()
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        raise RuntimeError(f"Refusing to run tests against non-test database {db_name!r}")
    if settings.redis_url.rstrip("/").endswith("/0"):
        raise RuntimeError("Refusing to run tests against Redis DB 0; set TEST_REDIS_URL")


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    _assert_safe_test_targets()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Each test runs inside a transaction that is rolled back afterwards."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture(autouse=True)
async def redis() -> AsyncIterator[Redis]:
    client = get_redis()
    await client.flushdb()
    yield client
    await client.flushdb()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
