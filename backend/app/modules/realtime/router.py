"""WebSocket endpoint: one socket per client carries chat events and notifications.

Connect: POST /realtime/ticket (Bearer token) -> connect to /ws?ticket=... from an allowed
Origin. Frames are JSON. Client -> server:

    {"type": "ping"}
    {"type": "chat.send", "ref": "1", "room_id": "...", "body": "...", "client_msg_id": "..."}
    {"type": "chat.read", "ref": "2", "room_id": "...", "seq": 42}

Server -> client: ready, pong, ack (with the same `ref`), error, chat.message, chat.read,
notification. Each frame re-checks that the account is still active and the session still
valid, and each chat frame re-checks room access.
"""

import asyncio
import contextlib
import json
import os
import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from app.core.config import get_settings
from app.core.db import SessionFactory, get_session_factory
from app.core.errors import AppError
from app.core.logging import get_logger
from app.core.rate_limit import hit
from app.modules.auth.deps import CurrentUser
from app.modules.chat import service as chat
from app.modules.chat.schemas import MessageBody
from app.modules.realtime.hub import CLOSE_SESSION_REVOKED, Connection, hub
from app.modules.realtime.tickets import TicketClaim, consume_ticket, issue_ticket
from app.modules.users.models import User

logger = get_logger(__name__)

router = APIRouter(tags=["realtime"])

MAX_FRAME_CHARS = 16_000
FRAMES_PER_10S = 60
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009


class TicketOut(BaseModel):
    ticket: str
    expires_in: int


@router.post(
    "/realtime/ticket",
    response_model=TicketOut,
    description="Single-use ticket for opening the WebSocket (`/ws?ticket=...`). "
    "Valid for `WS_TICKET_TTL_SECONDS`.",
)
async def create_ticket(user: CurrentUser) -> TicketOut:
    await hit(f"ws_ticket:user:{user.id}", 30, 60)
    ticket, ttl = await issue_ticket(user)
    return TicketOut(ticket=ticket, expires_in=ttl)


# ---------------------------------------------------------------- frames


class _Frame(BaseModel):
    ref: str | None = Field(default=None, max_length=64)


class PingFrame(_Frame):
    type: Literal["ping"]


class ChatSendFrame(_Frame):
    type: Literal["chat.send"]
    room_id: uuid.UUID
    body: MessageBody
    client_msg_id: uuid.UUID | None = None


class ChatReadFrame(_Frame):
    type: Literal["chat.read"]
    room_id: uuid.UUID
    seq: int = Field(ge=0)


ClientFrame = Annotated[PingFrame | ChatSendFrame | ChatReadFrame, Field(discriminator="type")]
_frame_adapter: TypeAdapter[ClientFrame] = TypeAdapter(ClientFrame)


def origin_allowed(origin: str | None) -> bool:
    settings = get_settings()
    if origin is None:
        return settings.ws_allow_missing_origin
    return origin in settings.cors_origins


async def _current_user(session: Any, claim: TicketClaim) -> User | None:
    user: User | None = await session.get(User, claim.user_id, populate_existing=True)
    if user is None or not user.is_active or user.token_version != claim.token_version:
        return None
    return user


async def _handle(session: Any, user: User, frame: ClientFrame) -> dict[str, Any]:
    if isinstance(frame, PingFrame):
        return {"type": "pong"}
    if isinstance(frame, ChatSendFrame):
        message, created = await chat.send_message(
            session, user, frame.room_id, frame.body, frame.client_msg_id
        )
        out = chat.to_message_out(message).model_dump(mode="json")
        return {"type": "ack", "ref": frame.ref, "created": created, "message": out}
    state = await chat.mark_read(session, user, frame.room_id, frame.seq)
    return {"type": "ack", "ref": frame.ref, "read": state.model_dump(mode="json")}


def _error(code: str, message: str, ref: str | None = None) -> dict[str, Any]:
    return {"type": "error", "ref": ref, "code": code, "message": message}


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    factory: Annotated[SessionFactory, Depends(get_session_factory)],
    ticket: str | None = None,
) -> None:
    # Reject before accepting: the handshake fails with 403 and nothing is exchanged.
    if not origin_allowed(websocket.headers.get("origin")):
        logger.warning("ws_rejected", reason="origin", origin=websocket.headers.get("origin"))
        await websocket.close(code=CLOSE_POLICY)
        return
    claim = await consume_ticket(ticket or "")
    if claim is None:
        logger.warning("ws_rejected", reason="ticket")
        await websocket.close(code=CLOSE_POLICY)
        return
    async with factory() as session:
        user = await _current_user(session, claim)
    if user is None:
        logger.warning("ws_rejected", reason="user", user_id=str(claim.user_id))
        await websocket.close(code=CLOSE_POLICY)
        return

    await websocket.accept()
    conn = Connection(websocket=websocket, user_id=claim.user_id, token_version=claim.token_version)
    await hub.register(conn)
    writer = asyncio.create_task(conn.run_writer())
    logger.info("ws_connected", user_id=str(claim.user_id), worker_pid=os.getpid())
    conn.send_json({"type": "ready", "user_id": str(claim.user_id)})
    try:
        await _read_loop(websocket, conn, claim, factory, writer)
    finally:
        await hub.unregister(conn)
        if not writer.done():
            conn.close(1000)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer, 2)
            writer.cancel()
        logger.info("ws_disconnected", user_id=str(claim.user_id))


async def _read_loop(
    websocket: WebSocket,
    conn: Connection,
    claim: TicketClaim,
    factory: SessionFactory,
    writer: asyncio.Task[None],
) -> None:
    window_start, frames = time.monotonic(), 0
    while True:
        receive = asyncio.ensure_future(websocket.receive())
        done, _ = await asyncio.wait({receive, writer}, return_when=asyncio.FIRST_COMPLETED)
        if receive not in done:  # the writer closed the socket (kicked / too slow)
            receive.cancel()
            return
        message = receive.result()
        if message["type"] == "websocket.disconnect":
            return
        raw = message.get("text")
        if raw is None:
            conn.send_json(_error("unsupported", "Send JSON text frames"))
            continue
        if len(raw) > MAX_FRAME_CHARS:
            conn.close(CLOSE_TOO_BIG)
            continue

        now = time.monotonic()
        if now - window_start > 10:
            window_start, frames = now, 0
        frames += 1
        if frames > FRAMES_PER_10S:
            conn.close(CLOSE_POLICY)
            continue

        try:
            frame = _frame_adapter.validate_python(json.loads(raw))
        except (ValueError, ValidationError):
            ref = None
            with contextlib.suppress(Exception):
                ref = str(json.loads(raw).get("ref"))[:64]
            conn.send_json(_error("invalid_frame", "Malformed or unknown frame", ref))
            continue

        try:
            async with factory() as session:
                user = await _current_user(session, claim)
                if user is None:  # deactivated, or logged out everywhere
                    conn.close(CLOSE_SESSION_REVOKED)
                    continue
                conn.send_json(await _handle(session, user, frame))
        except AppError as exc:
            conn.send_json(_error(exc.code, exc.message, frame.ref))
        except Exception:
            logger.exception("ws_frame_failed", frame_type=frame.type)
            conn.send_json(_error("internal_error", "Something went wrong", frame.ref))
