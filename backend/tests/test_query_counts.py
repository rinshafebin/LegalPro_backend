"""No N+1 queries on the main list endpoints: query count must not grow with page size."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import engine
from app.modules.bookings.models import BookingStatus
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import notify
from app.modules.users.models import User, UserRole
from tests.chat_support import make_booking
from tests.helpers import API, Actor, make_advocate, make_specialization, make_user

_session: AsyncSession


@pytest.fixture(autouse=True)
def _bind_session(db_session: AsyncSession) -> None:
    global _session
    _session = db_session


@contextmanager
def counting() -> Iterator[list[str]]:
    statements: list[str] = []

    def _count(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        yield statements
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)


async def _queries(client: AsyncClient, url: str, headers: dict[str, str]) -> int:
    # Production starts every request with an empty session; so must the measurement.
    _session.expire_all()
    with counting() as statements:
        resp = await client.get(url, headers=headers, params={"page_size": 50})
    assert resp.status_code == 200, resp.text
    return len(statements)


async def _assert_flat(
    client: AsyncClient,
    url: str,
    headers: dict[str, str],
    add: Callable[[int], Awaitable[None]],
) -> None:
    await add(2)
    small = await _queries(client, url, headers)
    await add(4)
    large = await _queries(client, url, headers)
    assert large == small, f"{url}: {small} queries for 2 items, {large} for 6 (N+1?)"


async def test_advocate_search(client: AsyncClient, db_session: AsyncSession) -> None:
    await make_specialization(db_session, "family-law", "Family law")
    await make_specialization(db_session, "criminal-law", "Criminal law")
    made = 0

    async def add(n: int) -> None:
        nonlocal made
        for _ in range(n):
            made += 1
            await make_advocate(
                db_session,
                f"s{made}@example.com",
                specialization_slugs=["family-law", "criminal-law"],
            )

    await _assert_flat(client, f"{API}/advocates", {}, add)


@pytest.fixture
async def lead(db_session: AsyncSession) -> Actor:
    return await make_advocate(db_session, "lead@example.com", full_name="Adv. Lead")


async def test_bookings_cases_and_chat_rooms(
    client: AsyncClient, db_session: AsyncSession, lead: Actor
) -> None:
    made = 0

    async def add_bookings(n: int) -> None:
        nonlocal made
        for _ in range(n):
            made += 1
            c = await make_user(
                db_session, UserRole.CLIENT, f"c{made}@example.com", full_name=f"C{made}"
            )
            await make_booking(db_session, c.id, lead.id, BookingStatus.CONFIRMED, days=made)

    await _assert_flat(client, f"{API}/bookings", lead.headers, add_bookings)

    async def add_cases(n: int) -> None:
        nonlocal made
        for _ in range(n):
            made += 1
            c = await make_user(
                db_session, UserRole.CLIENT, f"c{made}@example.com", full_name=f"C{made}"
            )
            booking = await make_booking(
                db_session, c.id, lead.id, BookingStatus.COMPLETED, days=-made
            )
            resp = await client.post(
                f"{API}/cases",
                headers=lead.headers,
                json={"booking_id": str(booking), "title": "Test case"},
            )
            assert resp.status_code == 201, resp.text

    await _assert_flat(client, f"{API}/cases", lead.headers, add_cases)
    await _assert_flat(client, f"{API}/chat/rooms", lead.headers, add_cases)


async def test_teams(client: AsyncClient, db_session: AsyncSession, lead: Actor) -> None:
    made = 0

    async def add(n: int) -> None:
        nonlocal made
        for _ in range(n):
            made += 1
            members = [
                (await make_advocate(db_session, f"m{made}-{i}@example.com")).id for i in range(2)
            ]
            resp = await client.post(
                f"{API}/teams",
                headers=lead.headers,
                json={"name": f"Team {made}", "member_ids": [str(m) for m in members]},
            )
            assert resp.status_code == 201, resp.text

    await _assert_flat(client, f"{API}/teams", lead.headers, add)


async def test_notifications_and_admin_lists(
    client: AsyncClient, db_session: AsyncSession, lead: Actor
) -> None:
    admin = await make_user(db_session, UserRole.ADMIN, "admin@example.com")

    async def add_notes(n: int) -> None:
        user = await db_session.get(User, lead.id, populate_existing=True)
        assert user is not None
        for _ in range(n):
            await notify(db_session, user, NotificationType.CASE_OPENED, title="x")

    await _assert_flat(client, f"{API}/notifications", lead.headers, add_notes)

    made = 0

    async def add_users(n: int) -> None:
        nonlocal made
        for _ in range(n):
            made += 1
            await make_user(db_session, UserRole.CLIENT, f"u{made}@example.com")

    # Admin views are audited (one INSERT + COMMIT each), which is constant per request.
    await _assert_flat(client, f"{API}/admin/users", admin.headers, add_users)

    async def add_audit(n: int) -> None:
        for _ in range(n):
            await client.get(f"{API}/admin/users", headers=admin.headers)  # each one is audited

    await _assert_flat(client, f"{API}/admin/audit-logs", admin.headers, add_audit)
