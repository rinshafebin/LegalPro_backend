"""WebSocket: ticket auth, Origin check, fan-out, per-message access checks, revocation."""

import asyncio
import uuid
from typing import Any, cast

import pytest
from httpx import AsyncClient
from httpx_ws import WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.websockets import WebSocket

from app.modules.auth.tokens import revoke_all_sessions
from app.modules.bookings.models import BookingStatus
from app.modules.chat.service import ensure_booking_room
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import notify
from app.modules.realtime.hub import Connection, Hub
from app.modules.realtime.pubsub import publish
from app.modules.users.models import User
from tests.chat_support import (
    ChatWorld,
    assert_refused,
    chat,
    connect,
    drain,
    get_ticket,
    make_booking,
    next_event,
    open_ws,
    send,
    send_frame,
)
from tests.helpers import API

__all__ = ["chat"]


def _of_type(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("type") == type_]


# ---------------------------------------------------------------- handshake


async def test_ticket_requires_a_logged_in_user(client: AsyncClient) -> None:
    assert (await client.post(f"{API}/realtime/ticket")).status_code == 401


async def test_ticket_is_single_use_short_lived_and_stored_hashed(
    ws_client: AsyncClient, chat: ChatWorld, redis: Redis
) -> None:
    ticket = await get_ticket(ws_client, chat.client)
    keys = await redis.keys("ws:ticket:*")
    assert len(keys) == 1
    assert ticket not in keys[0]
    assert 0 < await redis.ttl(keys[0]) <= 30

    async with open_ws(ticket) as ws:
        assert (await ws.receive_json(timeout=2))["type"] == "ready"
    await assert_refused(ticket)


@pytest.mark.parametrize("ticket", ["", "not-a-real-ticket"])
async def test_missing_or_unknown_ticket_is_refused(ws_client: AsyncClient, ticket: str) -> None:
    await assert_refused(ticket)


@pytest.mark.parametrize("origin", ["https://evil.example.com", None])
async def test_origin_must_be_an_allowed_cors_origin(
    ws_client: AsyncClient, chat: ChatWorld, origin: str | None
) -> None:
    await assert_refused(await get_ticket(ws_client, chat.client), origin=origin)


async def test_ticket_from_a_revoked_session_is_refused(
    ws_client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    ticket = await get_ticket(ws_client, chat.client)
    await db_session.execute(
        update(User).where(User.id == chat.client.id).values(token_version=User.token_version + 1)
    )
    await db_session.commit()
    await assert_refused(ticket)


# ---------------------------------------------------------------- delivery


async def test_message_reaches_each_participant_exactly_once(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with (
        connect(ws_client, chat.client) as client_ws,
        connect(ws_client, chat.lead) as lead_ws,
        connect(ws_client, chat.member) as member_ws,
        connect(ws_client, chat.outsider) as outsider_ws,
        connect(ws_client, chat.other_client) as other_ws,
    ):
        await send_frame(
            client_ws,
            {
                "type": "chat.send",
                "ref": "m1",
                "room_id": chat.room_id,
                "body": "Hello counsel",
                "sender_id": str(chat.lead.id),  # spoof attempt: ignored
            },
        )
        mine = await drain(client_ws, 0.5)
        [ack] = _of_type(mine, "ack")
        assert ack["ref"] == "m1"
        assert ack["created"] is True
        assert ack["message"]["sender_id"] == str(chat.client.id)
        assert len(_of_type(mine, "chat.message")) == 1  # the sender's own copy, once

        for ws in (lead_ws, member_ws):
            received = _of_type(await drain(ws), "chat.message")
            assert len(received) == 1
            assert received[0]["message"]["body"] == "Hello counsel"
            assert received[0]["message"]["sender_id"] == str(chat.client.id)
        for ws in (outsider_ws, other_ws):
            assert _of_type(await drain(ws), "chat.message") == []


async def test_two_devices_of_one_user_each_get_it_once(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with connect(ws_client, chat.lead) as phone, connect(ws_client, chat.lead) as laptop:
        await send(ws_client, chat.client, chat.room_id, "hi")
        for ws in (phone, laptop):
            assert len(_of_type(await drain(ws), "chat.message")) == 1


async def test_resent_client_msg_id_is_stored_and_delivered_once(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    cmid = str(uuid.uuid4())
    async with (
        connect(ws_client, chat.client) as client_ws,
        connect(ws_client, chat.lead) as lead_ws,
    ):
        for ref in ("a", "b"):
            await send_frame(
                client_ws,
                {
                    "type": "chat.send",
                    "ref": ref,
                    "room_id": chat.room_id,
                    "body": "once",
                    "client_msg_id": cmid,
                },
            )
        first, second = await next_event(client_ws, "ack"), await next_event(client_ws, "ack")
        assert first["created"] is True
        assert second["created"] is False
        assert first["message"]["id"] == second["message"]["id"]
        assert len(_of_type(await drain(lead_ws), "chat.message")) == 1


async def test_separate_processes_deliver_exactly_once() -> None:
    """Two hubs stand in for two API workers sharing Redis."""
    worker_a, worker_b = Hub(), Hub()
    alice, bob = uuid.uuid4(), uuid.uuid4()
    fake_ws = cast(WebSocket, object())
    alice_on_a = Connection(websocket=fake_ws, user_id=alice, token_version=0)
    alice_on_b = Connection(websocket=fake_ws, user_id=alice, token_version=0)
    bob_on_b = Connection(websocket=fake_ws, user_id=bob, token_version=0)
    try:
        await worker_a.register(alice_on_a)
        await worker_b.register(alice_on_b)
        await worker_b.register(bob_on_b)
        await asyncio.sleep(0.1)  # let both subscriptions settle

        await publish([alice], {"type": "chat.message", "n": 1})
        for conn in (alice_on_a, alice_on_b):
            assert '"n":1' in str(await asyncio.wait_for(conn.outbound.get(), 2))
        await asyncio.sleep(0.2)
        assert alice_on_a.outbound.empty()
        assert alice_on_b.outbound.empty()
        assert bob_on_b.outbound.empty()

        await worker_a.unregister(alice_on_a)
        await publish([alice], {"type": "chat.message", "n": 2})
        assert '"n":2' in str(await asyncio.wait_for(alice_on_b.outbound.get(), 2))
        await asyncio.sleep(0.2)
        assert alice_on_a.outbound.empty()
    finally:
        await worker_a.close()
        await worker_b.close()


async def test_rest_messages_and_read_receipts_are_pushed(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with (
        connect(ws_client, chat.client) as client_ws,
        connect(ws_client, chat.lead) as lead_ws,
    ):
        message = await send(ws_client, chat.lead, chat.room_id, "Documents received")
        pushed = await next_event(client_ws, "chat.message")
        assert pushed["message"]["id"] == message["id"]

        await send_frame(
            client_ws,
            {"type": "chat.read", "ref": "r", "room_id": chat.room_id, "seq": message["seq"]},
        )
        assert (await next_event(client_ws, "ack"))["read"]["last_read_seq"] == message["seq"]
        receipt = await next_event(lead_ws, "chat.read")
        assert receipt["user_id"] == str(chat.client.id)
        assert receipt["last_read_seq"] == message["seq"]


async def test_notifications_are_pushed(
    ws_client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    async with connect(ws_client, chat.client) as ws:
        user = await db_session.get(User, chat.client.id)
        assert user is not None
        await notify(db_session, user, NotificationType.CASE_OPENED, title="Case CASE-1 opened")
        event = await next_event(ws, "notification")
        assert event["notification"]["title"] == "Case CASE-1 opened"


# ---------------------------------------------------------------- losing access


async def test_removed_member_is_cut_off_immediately(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with (
        connect(ws_client, chat.member) as member_ws,
        connect(ws_client, chat.client) as client_ws,
    ):
        await send_frame(member_ws, {"type": "chat.send", "room_id": chat.room_id, "body": "in"})
        assert (await next_event(member_ws, "ack"))["created"] is True

        removed = await ws_client.delete(
            f"{API}/cases/{chat.case_id}/members/{chat.member.id}", headers=chat.lead.headers
        )
        assert removed.status_code == 204
        await drain(member_ws)

        # Still connected, but the next message is checked again and refused...
        await send_frame(
            member_ws, {"type": "chat.send", "ref": "x", "room_id": chat.room_id, "body": "out"}
        )
        error = await next_event(member_ws, "error")
        assert error["code"] == "not_found"
        assert error["ref"] == "x"
        # ...and messages from others no longer reach them.
        await send_frame(client_ws, {"type": "chat.send", "room_id": chat.room_id, "body": "x"})
        await next_event(client_ws, "ack")
        assert _of_type(await drain(member_ws), "chat.message") == []


async def test_cancelled_booking_closes_its_room_at_once(
    ws_client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    booking_id = await make_booking(db_session, chat.client.id, chat.lead.id, days=4)
    await ensure_booking_room(db_session, booking_id)
    rooms = (await ws_client.get(f"{API}/chat/rooms", headers=chat.client.headers)).json()
    room_id = next(r["id"] for r in rooms["items"] if r["booking_id"] == str(booking_id))

    async with connect(ws_client, chat.client) as ws:
        await send_frame(ws, {"type": "chat.send", "room_id": room_id, "body": "see you"})
        assert (await next_event(ws, "ack"))["created"] is True
        cancelled = await ws_client.post(
            f"{API}/bookings/{booking_id}/cancel", headers=chat.lead.headers, json={}
        )
        assert cancelled.status_code == 200
        await send_frame(ws, {"type": "chat.send", "room_id": room_id, "body": "hello?"})
        assert (await next_event(ws, "error"))["code"] == "not_found"


async def test_logout_everywhere_closes_open_sockets(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with connect(ws_client, chat.client) as ws:
        await revoke_all_sessions(chat.client.id)
        with pytest.raises(WebSocketDisconnect) as closed:
            await next_event(ws, "never")
        assert closed.value.code == 4401


async def test_deactivated_user_is_cut_off_on_next_frame(
    ws_client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    async with connect(ws_client, chat.client) as ws:
        await db_session.execute(
            update(User).where(User.id == chat.client.id).values(is_active=False)
        )
        await db_session.commit()
        await send_frame(ws, {"type": "ping"})
        with pytest.raises(WebSocketDisconnect) as closed:
            await next_event(ws, "pong")
        assert closed.value.code == 4401


# ---------------------------------------------------------------- frames and limits


async def test_ping_and_bad_frames(ws_client: AsyncClient, chat: ChatWorld) -> None:
    async with connect(ws_client, chat.client) as ws:
        await send_frame(ws, {"type": "ping"})
        assert (await next_event(ws, "pong"))["type"] == "pong"
        await send_frame(ws, {"type": "rm -rf", "ref": "q"})
        error = await next_event(ws, "error")
        assert error["code"] == "invalid_frame"
        assert error["ref"] == "q"
        await send_frame(ws, {"type": "chat.send", "room_id": chat.room_id, "body": "x" * 4001})
        assert (await next_event(ws, "error"))["code"] == "invalid_frame"
        await ws.send_text("not json")
        assert (await next_event(ws, "error"))["code"] == "invalid_frame"


async def test_oversized_frame_closes_the_socket(ws_client: AsyncClient, chat: ChatWorld) -> None:
    async with connect(ws_client, chat.client) as ws:
        await ws.send_text("x" * 20_000)
        with pytest.raises(WebSocketDisconnect) as closed:
            await next_event(ws, "never")
        assert closed.value.code == 1009


async def test_per_user_rate_limit_applies_over_websocket(
    ws_client: AsyncClient, chat: ChatWorld
) -> None:
    async with connect(ws_client, chat.client) as ws:
        for i in range(21):
            await send_frame(
                ws, {"type": "chat.send", "ref": str(i), "room_id": chat.room_id, "body": f"m{i}"}
            )
        events = await drain(ws, 0.5)
        assert len(_of_type(events, "ack")) == 20
        errors = _of_type(events, "error")
        assert [e["code"] for e in errors] == ["rate_limited"]
        assert errors[0]["ref"] == "20"


async def test_booking_status_used_for_access_includes_completed(
    ws_client: AsyncClient, chat: ChatWorld, db_session: AsyncSession
) -> None:
    booking_id = await make_booking(
        db_session, chat.client.id, chat.lead.id, BookingStatus.COMPLETED
    )
    await ensure_booking_room(db_session, booking_id)
    rooms = (await ws_client.get(f"{API}/chat/rooms", headers=chat.lead.headers)).json()["items"]
    assert str(booking_id) in {r["booking_id"] for r in rooms}


def test_tickets_are_redacted_from_logs() -> None:
    from app.core.logging import redact_secrets

    line = '1.2.3.4:5 - "WebSocket /api/v1/ws?ticket=abc-DEF_123" [accepted]'
    out = redact_secrets(None, "info", {"event": line})
    assert out["event"] == '1.2.3.4:5 - "WebSocket /api/v1/ws?ticket=[redacted]" [accepted]'


async def test_edits_and_deletes_are_broadcast(ws_client: AsyncClient, chat: ChatWorld) -> None:
    message = await send(ws_client, chat.client, chat.room_id, "typo")
    url = f"{API}/chat/rooms/{chat.room_id}/messages/{message['id']}"
    async with (
        connect(ws_client, chat.lead) as lead_ws,
        connect(ws_client, chat.outsider) as out_ws,
    ):
        await ws_client.patch(url, headers=chat.client.headers, json={"body": "fixed"})
        edited = await next_event(lead_ws, "chat.message_updated")
        assert edited["message"]["body"] == "fixed"
        assert edited["message"]["edited"] is True

        await ws_client.delete(url, headers=chat.client.headers)
        deleted = await next_event(lead_ws, "chat.message_updated")
        assert deleted["message"]["deleted"] is True
        assert deleted["message"]["body"] is None
        assert _of_type(await drain(out_ws), "chat.message_updated") == []
