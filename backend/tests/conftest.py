import os

# Must run before any `app.*` import so cached settings pick up the test overrides.
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import engine, get_session
from app.core.email import open_message
from app.core.redis import get_redis
from app.core.storage import set_storage
from app.main import app
from app.modules.payments.gateway import set_gateway
from app.workers.queue import JobQueue, get_job_queue, set_job_queue


def _assert_safe_test_targets() -> None:
    settings = get_settings()
    db_name = make_url(settings.database_url).database or ""
    if not db_name.endswith("_test"):
        raise RuntimeError(f"Refusing to run tests against non-test database {db_name!r}")
    if settings.redis_url.rstrip("/").endswith("/0"):
        raise RuntimeError("Refusing to run tests against Redis DB 0; set TEST_REDIS_URL")


BACKEND_DIR = Path(__file__).resolve().parent.parent


def _run_migrations(database_url: str) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and arguments
        [sys.executable, "-m", "alembic", "-x", f"db_url={database_url}", "upgrade", "head"],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Migrations failed:\n{result.stderr[-3000:]}")


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> AsyncIterator[None]:
    """Build the test schema by running the real Alembic migrations.

    Testing against migrated tables (not `create_all`) catches migrations that drift from
    the models in ways `alembic check` can't see, e.g. stale CHECK constraints.
    """
    _assert_safe_test_targets()
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()
    await asyncio.to_thread(_run_migrations, get_settings().database_url)
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


class FakeJobQueue:
    """Records enqueued jobs instead of sending them to Redis."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any]]] = []
        self.aborted: list[str] = []

    async def enqueue(
        self,
        job: str,
        /,
        *,
        defer_until: datetime | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.jobs.append((job, {**kwargs, "_defer_until": defer_until, "_job_id": job_id}))

    async def abort(self, job_id: str) -> None:
        self.aborted.append(job_id)

    async def close(self) -> None:
        return None

    def emails(self) -> list[dict[str, str]]:
        return [open_message(kw["sealed"]) for name, kw in self.jobs if name == "send_email"]

    def emails_to(self, address: str) -> list[dict[str, str]]:
        return [m for m in self.emails() if m["to"] == address]


@pytest.fixture(autouse=True)
def jobs() -> Iterator[FakeJobQueue]:
    original: JobQueue = get_job_queue()
    fake = FakeJobQueue()
    set_job_queue(fake)
    yield fake
    set_job_queue(original)


class FakeGateway:
    """Stands in for Razorpay: hands out order ids and records what was requested."""

    key_id = "rzp_test_fake"

    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.refunds: list[dict[str, Any]] = []
        self.refund_status = "pending"  # Razorpay usually processes refunds asynchronously
        self.refund_error: Exception | None = None

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> str:
        order_id = f"order_fake_{len(self.orders) + 1}"
        self.orders.append(
            {"id": order_id, "amount": amount_paise, "currency": currency, "receipt": receipt}
        )
        return order_id

    async def refund(
        self, *, payment_id: str, amount_paise: int, notes: dict[str, str]
    ) -> tuple[str, str]:
        if self.refund_error is not None:
            raise self.refund_error
        refund_id = f"rfnd_fake_{len(self.refunds) + 1}"
        self.refunds.append({"id": refund_id, "payment_id": payment_id, "amount": amount_paise})
        return refund_id, self.refund_status


@pytest.fixture(autouse=True)
def gateway() -> Iterator[FakeGateway]:
    fake = FakeGateway()
    set_gateway(fake)
    yield fake
    set_gateway(None)


class FakeStorage:
    """In-memory object store; presigned URLs are fake but carry key + filename."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def presigned_get(
        self, key: str, *, filename: str, content_type: str, ttl_seconds: int
    ) -> str:
        assert key in self.objects
        return f"https://storage.test/{key}?filename={filename}&ttl={ttl_seconds}&sig=fake"

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


@pytest.fixture(autouse=True)
def storage() -> Iterator[FakeStorage]:
    fake = FakeStorage()
    set_storage(fake)
    yield fake
    set_storage(None)


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session
        # Production gets a fresh session per request; expiring everything after each
        # request keeps tests from passing because of stale identity-map state.
        db_session.expire_all()

    app.dependency_overrides[get_session] = _session_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
