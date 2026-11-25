"""Integration tests for the SAQ adapter against the real (test) Redis."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from saq import Worker
from saq.job import Status

from app.core import email
from app.workers import tasks
from app.workers.queue import SaqJobQueue


@pytest.fixture
async def saq_queue() -> AsyncIterator[SaqJobQueue]:
    queue = SaqJobQueue()
    yield queue
    await queue.close()


async def test_enqueued_email_job_is_processed_by_a_worker(
    saq_queue: SaqJobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def _fake_send(to: str, subject: str, body: str) -> None:
        sent.append(to)

    monkeypatch.setattr(email, "send_email", _fake_send)
    await saq_queue.enqueue(
        "send_email", job_id="job-1", sealed=email.seal_message("a@example.com", "Hi", "Body")
    )

    inner = await saq_queue._get_queue()
    worker = Worker(inner, functions=[tasks.send_email])
    running = asyncio.create_task(worker.start())
    try:
        job = await inner.job("job-1")
        assert job is not None
        await job.refresh(until_complete=5)
    finally:
        await worker.stop()
        await running

    assert job.status is Status.COMPLETE
    assert sent == ["a@example.com"]


async def test_deferred_job_is_scheduled_and_can_be_aborted(saq_queue: SaqJobQueue) -> None:
    run_at = datetime.now(UTC) + timedelta(hours=24)
    await saq_queue.enqueue("heartbeat", job_id="reminder-1", defer_until=run_at)

    inner = await saq_queue._get_queue()
    job = await inner.job("reminder-1")
    assert job is not None
    assert job.scheduled == int(run_at.timestamp())

    await saq_queue.abort("reminder-1")
    job = await inner.job("reminder-1")
    assert job is not None
    assert job.status in (Status.ABORTING, Status.ABORTED)


async def test_same_job_id_is_enqueued_once(saq_queue: SaqJobQueue) -> None:
    run_at = datetime.now(UTC) + timedelta(hours=1)
    await saq_queue.enqueue("heartbeat", job_id="dedupe-1", defer_until=run_at)
    await saq_queue.enqueue("heartbeat", job_id="dedupe-1", defer_until=run_at)
    inner = await saq_queue._get_queue()
    assert await inner.count("incomplete") == 1


@pytest.mark.parametrize("reserved", ["key", "timeout", "retries"])
async def test_job_arguments_cannot_shadow_saq_fields(
    saq_queue: SaqJobQueue, reserved: str
) -> None:
    kwargs: dict[str, Any] = {reserved: 1}
    with pytest.raises(ValueError, match="clash"):
        await saq_queue.enqueue("heartbeat", **kwargs)
