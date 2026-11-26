"""Shared fixtures/helpers for booking and payment tests."""

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import time
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.advocates.models import AdvocateProfile
from app.modules.bookings.models import AvailabilityRule
from app.modules.payments.gateway import webhook_signature
from app.modules.users.models import UserRole
from tests.helpers import API, Actor, make_advocate, make_user


@dataclass
class World:
    advocate: Actor
    other_advocate: Actor
    client: Actor
    other_client: Actor
    unverified_client: Actor


async def add_all_week_availability(
    session: AsyncSession, advocate_id: uuid.UUID, start: time = time(9), end: time = time(17)
) -> None:
    session.add_all(
        AvailabilityRule(
            advocate_id=advocate_id, weekday=d, start_time=start, end_time=end, slot_minutes=30
        )
        for d in range(7)
    )
    await session.commit()


async def set_fee(session: AsyncSession, advocate_id: uuid.UUID, fee: str | None) -> None:
    await session.execute(
        update(AdvocateProfile)
        .where(AdvocateProfile.user_id == advocate_id)
        .values(consultation_fee=Decimal(fee) if fee is not None else None)
    )
    await session.commit()


@pytest.fixture
async def world(db_session: AsyncSession) -> World:
    advocate = await make_advocate(db_session, "adv@example.com", full_name="Adv. Meera")
    other_advocate = await make_advocate(db_session, "adv2@example.com", full_name="Adv. Ravi")
    for adv in (advocate, other_advocate):
        await set_fee(db_session, adv.id, "1500.00")
        await add_all_week_availability(db_session, adv.id)
    return World(
        advocate=advocate,
        other_advocate=other_advocate,
        client=await make_user(db_session, UserRole.CLIENT, "c1@example.com", full_name="Asha"),
        other_client=await make_user(
            db_session, UserRole.CLIENT, "c2@example.com", full_name="Bina"
        ),
        unverified_client=await make_user(
            db_session, UserRole.CLIENT, "c3@example.com", email_verified=False
        ),
    )


async def free_slots(client: AsyncClient, advocate_id: uuid.UUID, days: int = 3) -> list[str]:
    resp = await client.get(f"{API}/advocates/{advocate_id}/slots", params={"days": days})
    assert resp.status_code == 200, resp.text
    return [s["start_at"] for s in resp.json()["slots"]]


async def book(
    client: AsyncClient, actor: Actor, advocate_id: uuid.UUID, start_at: str
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/bookings",
        headers=actor.headers,
        json={"advocate_id": str(advocate_id), "start_at": start_at},
    )
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


def session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Lets worker jobs run inside the test's rolled-back transaction."""

    @asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return _factory


def webhook_request(
    order_id: str,
    amount: int,
    *,
    event: str = "payment.captured",
    payment_id: str = "pay_test_1",
    currency: str = "INR",
    event_id: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "order_id": order_id,
                        "amount": amount,
                        "currency": currency,
                        "status": "captured",
                    }
                }
            },
        }
    ).encode()
    headers = {
        "X-Razorpay-Signature": webhook_signature(body),
        "X-Razorpay-Event-Id": event_id or f"evt_{uuid.uuid4().hex[:12]}",
        "Content-Type": "application/json",
    }
    return body, headers
