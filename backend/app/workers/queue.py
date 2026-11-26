"""Background-job queue interface.

App code only calls `get_job_queue().enqueue(...)`. The SAQ-specific code
lives here and in `app/workers/settings.py`.
"""

from datetime import datetime
from typing import Any, Protocol

from saq import Queue
from saq.job import Job

from app.core.config import get_settings

QUEUE_NAME = "advocate"
_RESERVED = frozenset(Job.__dataclass_fields__)


def create_saq_queue() -> Queue:
    return Queue.from_url(get_settings().redis_url, name=QUEUE_NAME)


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

    async def abort(self, job_id: str) -> None: ...

    async def close(self) -> None: ...


class SaqJobQueue:
    def __init__(self) -> None:
        self._queue: Queue | None = None

    async def _get_queue(self) -> Queue:
        if self._queue is None:
            queue = create_saq_queue()
            await queue.connect()
            self._queue = queue
        return self._queue

    async def enqueue(
        self,
        job: str,
        /,
        *,
        defer_until: datetime | None = None,
        job_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        clash = _RESERVED & kwargs.keys()
        if clash:
            # SAQ would silently treat these as job settings instead of function arguments.
            raise ValueError(f"Job arguments clash with SAQ job fields: {sorted(clash)}")
        queue = await self._get_queue()
        await queue.enqueue(
            job,
            key=job_id,
            scheduled=int(defer_until.timestamp()) if defer_until else 0,
            timeout=60,
            retries=5,
            retry_delay=5.0,
            retry_backoff=True,
            **kwargs,
        )

    async def abort(self, job_id: str) -> None:
        queue = await self._get_queue()
        job = await queue.job(job_id)
        if job is not None:
            await queue.abort(job, error="aborted by application")

    async def close(self) -> None:
        if self._queue is not None:
            await self._queue.disconnect()
            self._queue = None


_queue: JobQueue = SaqJobQueue()


def get_job_queue() -> JobQueue:
    return _queue


def set_job_queue(queue: JobQueue) -> None:
    """Swap the queue implementation (used by tests)."""
    global _queue
    _queue = queue
