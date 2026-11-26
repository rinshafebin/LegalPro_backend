"""Fixtures and helpers for chat, realtime, notification and audit tests."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.users.models import UserRole
from tests.helpers import API, Actor, make_advocate, make_user

ORIGIN = "http://localhost:5173"  # in CORS_ORIGINS locally and in CI


@dataclass
class ChatWorld:
    lead: Actor
    member: Actor
    outsider: Actor
    client: Actor
    other_client: Actor
    case_id: str
    room_id: str


async def make_booking(
    session: AsyncSession,
    client_id: uuid.UUID,
    advocate_id: uuid.UUID,
    status: BookingStatus = BookingStatus.CONFIRMED,
    days: int = 3,
) -> uuid.UUID:
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=days)
    booking = Booking(
        client_id=client_id,
        advocate_id=advocate_id,
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=status,
        fee_amount=Decimal("1000.00"),
        currency="INR",
    )
    session.add(booking)
    await session.commit()
    return booking.id


@pytest.fixture
async def chat(client: AsyncClient, db_session: AsyncSession) -> ChatWorld:
    """A case (lead + member + client) and its auto-created chat room."""
    lead = await make_advocate(db_session, "lead@example.com", full_name="Adv. Lead")
    member = await make_advocate(db_session, "member@example.com", full_name="Adv. Member")
    outsider = await make_advocate(db_session, "outsider@example.com", full_name="Adv. Out")
    owner = await make_user(db_session, UserRole.CLIENT, "owner@example.com", full_name="Owner")
    other = await make_user(db_session, UserRole.CLIENT, "other@example.com", full_name="Other")
    booking_id = await make_booking(db_session, owner.id, lead.id, BookingStatus.COMPLETED)
    resp = await client.post(
        f"{API}/cases",
        headers=lead.headers,
        json={"booking_id": str(booking_id), "title": "Secret merger dispute"},
    )
    assert resp.status_code == 201, resp.text
    case_id = resp.json()["id"]
    added = await client.post(
        f"{API}/cases/{case_id}/members",
        headers=lead.headers,
        json={"advocate_id": str(member.id), "role": "associate"},
    )
    assert added.status_code == 201, added.text
    rooms = (await client.get(f"{API}/chat/rooms", headers=owner.headers)).json()["items"]
    room_id = next(r["id"] for r in rooms if r["case_id"] == case_id)
    return ChatWorld(lead, member, outsider, owner, other, case_id, room_id)


async def send(
    client: AsyncClient, actor: Actor, room_id: str, body: str, **extra: Any
) -> dict[str, Any]:
    resp = await client.post(
        f"{API}/chat/rooms/{room_id}/messages", headers=actor.headers, json={"body": body, **extra}
    )
    assert resp.status_code in (200, 201), resp.text
    data: dict[str, Any] = resp.json()
    return data


async def get_ticket(client: AsyncClient, actor: Actor) -> str:
    resp = await client.post(f"{API}/realtime/ticket", headers=actor.headers)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["ticket"])


@asynccontextmanager
async def open_ws(ticket: str, origin: str | None = ORIGIN) -> AsyncIterator[AsyncWebSocketSession]:
    """Raw connect (no 'ready' check); raises WebSocketDisconnect if refused."""
    headers = {"Origin": origin} if origin else {}
    transport = ASGIWebSocketTransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        session: AsyncWebSocketSession
        async with aconnect_ws(
            f"http://test{API}/ws?ticket={ticket}",
            c,
            headers=headers,
            keepalive_ping_interval_seconds=None,
        ) as session:
            yield session


@asynccontextmanager
async def connect(
    ws_client: AsyncClient, actor: Actor, origin: str | None = ORIGIN
) -> AsyncIterator[AsyncWebSocketSession]:
    async with open_ws(await get_ticket(ws_client, actor), origin) as ws:
        ready = await ws.receive_json(timeout=2)
        assert ready["type"] == "ready", ready
        yield ws


async def next_event(ws: AsyncWebSocketSession, type_: str, wait: float = 2) -> dict[str, Any]:
    """The next event of `type_` within `wait` seconds, skipping others."""
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"no {type_!r} event within {wait}s")
        event: dict[str, Any] = await ws.receive_json(timeout=remaining)
        if event.get("type") == type_:
            return event


async def drain(ws: AsyncWebSocketSession, wait: float = 0.3) -> list[dict[str, Any]]:
    """Everything that arrives until nothing has come for `wait` seconds."""
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(await ws.receive_json(timeout=wait))
        except TimeoutError:
            return events


async def send_frame(ws: AsyncWebSocketSession, frame: dict[str, Any]) -> None:
    await ws.send_text(json.dumps(frame))


def _contains(exc: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(exc, kind):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains(e, kind) for e in exc.exceptions)
    return False


async def assert_refused(ticket: str, origin: str | None = ORIGIN) -> None:
    """The handshake must be rejected before the socket is accepted."""
    from httpx_ws import WebSocketDisconnect

    try:
        async with open_ws(ticket, origin):
            pass
    except BaseException as exc:  # anyio wraps it in an ExceptionGroup
        assert _contains(exc, WebSocketDisconnect), exc  # noqa: PT017
        return
    raise AssertionError("WebSocket was accepted")
