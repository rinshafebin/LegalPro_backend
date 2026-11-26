"""Background job functions. Each one must be listed in `settings["functions"]`.

SAQ calls them as `fn(ctx, **kwargs)`. Jobs that need the database open a session from
`ctx["session_factory"]` (tests inject one), falling back to the app's SessionLocal.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email
from app.core.db import SessionLocal
from app.modules.bookings import service as bookings
from app.modules.cases import service as cases
from app.modules.payments import service as payments
from app.modules.users import account

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@asynccontextmanager
async def _session(ctx: Any) -> AsyncIterator[AsyncSession]:
    factory: SessionFactory | None = (ctx or {}).get("session_factory")
    if factory is None:
        async with SessionLocal() as session:
            yield session
    else:
        async with factory() as session:
            yield session


async def heartbeat(_ctx: Any) -> str:
    """Trivial job used by health checks and smoke tests."""
    return "ok"


async def send_email(_ctx: Any, *, sealed: str) -> None:
    """`sealed` comes from `email.seal_message`, so no PII or secrets appear in job logs."""
    message = email.open_message(sealed)
    await email.send_email(message["to"], message["subject"], message["body"])


async def expire_booking(ctx: Any, *, booking_id: str) -> bool:
    async with _session(ctx) as session:
        return await bookings.expire_booking(session, uuid.UUID(booking_id))


async def expire_stale_bookings(ctx: Any) -> int:
    async with _session(ctx) as session:
        return await bookings.expire_stale_bookings(session)


async def send_booking_reminder(ctx: Any, *, booking_id: str, kind: str, start_ts: int) -> bool:
    async with _session(ctx) as session:
        return await bookings.send_reminder(session, uuid.UUID(booking_id), kind, start_ts)


async def issue_refund(ctx: Any, *, payment_id: str) -> str:
    job = (ctx or {}).get("job")
    final_attempt = job is None or not job.retryable  # SAQ bumps attempts before running
    async with _session(ctx) as session:
        return await payments.issue_refund(
            session, uuid.UUID(payment_id), final_attempt=final_attempt
        )


async def send_hearing_reminder(ctx: Any, *, hearing_id: str, kind: str, start_ts: int) -> int:
    async with _session(ctx) as session:
        return await cases.send_hearing_reminder(session, uuid.UUID(hearing_id), kind, start_ts)


async def delete_account(ctx: Any, *, user_id: str, scheduled_ts: int) -> str:
    async with _session(ctx) as session:
        return await account.run_scheduled_deletion(session, uuid.UUID(user_id), scheduled_ts)


async def purge_due_deletions(ctx: Any) -> int:
    async with _session(ctx) as session:
        return await account.purge_due_deletions(session)
