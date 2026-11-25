"""Background-job queue interface.

App code only calls `get_job_queue().enqueue(...)`, so replacing ARQ later
means rewriting this file and `settings.py`, nothing else.
"""

from datetime import datetime
from typing import Any, Protocol

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings

from app.core.config import get_settings


class JobQueue(Protocol):
    async def enqueue(
        self,
        job: str,
        /,
        *,
        defer_until: datetime | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> None: ...

    async def close(self) -> None: ...


class ArqJobQueue:
    def __init__(self) -> None:
        self._pool: ArqRedis | None = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        return self._pool

    async def enqueue(
        self,
        job: str,
        /,
        *,
        defer_until: datetime | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        pool = await self._get_pool()
        await pool.enqueue_job(job, _defer_until=defer_until, _job_id=job_id, **kwargs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


_queue: JobQueue = ArqJobQueue()


def get_job_queue() -> JobQueue:
    return _queue


def set_job_queue(queue: JobQueue) -> None:
    """Swap the queue implementation (used by tests)."""
    global _queue
    _queue = queue
