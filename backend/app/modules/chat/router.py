import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.deps import SessionDep
from app.core.pagination import Page, PageDep, make_page
from app.modules.auth.deps import require_roles
from app.modules.cases.schemas import DownloadOut
from app.modules.chat import service
from app.modules.chat.schemas import (
    MessageBody,
    MessageIn,
    MessageOut,
    MessagePage,
    ReadIn,
    ReadStateOut,
    RoomDetailOut,
    RoomOut,
    UnreadTotalOut,
)
from app.modules.users.models import User, UserRole

ChatUser = Annotated[User, Depends(require_roles(UserRole.CLIENT, UserRole.ADVOCATE))]

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/rooms", response_model=Page[RoomOut], summary="Rooms I can use, newest activity first")
async def list_rooms(user: ChatUser, session: SessionDep, page: PageDep) -> Page[RoomOut]:
    rows, total = await service.list_rooms(session, user, page)
    return make_page(rows, total, page)


@router.get("/unread", response_model=UnreadTotalOut)
async def unread_total(user: ChatUser, session: SessionDep) -> UnreadTotalOut:
    return UnreadTotalOut(unread=await service.unread_total(session, user))


@router.get("/rooms/{room_id}", response_model=RoomDetailOut)
async def get_room(room_id: uuid.UUID, user: ChatUser, session: SessionDep) -> RoomDetailOut:
    return await service.get_room(session, user, room_id)


@router.get(
    "/rooms/{room_id}/messages",
    response_model=MessagePage,
    description="Cursor pagination on `seq`. Default/`before`: newest first (history). "
    "`after`: oldest first (catch up after reconnecting).",
)
async def list_messages(
    room_id: uuid.UUID,
    user: ChatUser,
    session: SessionDep,
    before: Annotated[int | None, Query(ge=1)] = None,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=service.MAX_PAGE)] = 50,
) -> MessagePage:
    items, cursor = await service.list_messages(
        session, user, room_id, before=before, after=after, limit=limit
    )
    return MessagePage(items=items, next_cursor=cursor)


@router.post("/rooms/{room_id}/messages", response_model=MessageOut, status_code=201)
async def send_message(
    room_id: uuid.UUID, data: MessageIn, user: ChatUser, session: SessionDep, response: Response
) -> MessageOut:
    message, created = await service.send_message(
        session, user, room_id, data.body, data.client_msg_id
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return service.to_message_out(message)


@router.post(
    "/rooms/{room_id}/attachments",
    response_model=MessageOut,
    status_code=201,
    description="Multipart. Same rules as case documents: PDF, PNG, JPEG, DOCX or TXT detected "
    "from the content, max `CHAT_ATTACHMENT_MAX_MB`.",
)
async def send_attachment(
    room_id: uuid.UUID,
    user: ChatUser,
    session: SessionDep,
    response: Response,
    file: Annotated[UploadFile, File()],
    body: Annotated[MessageBody | None, Form()] = None,
    client_msg_id: Annotated[uuid.UUID | None, Form()] = None,
) -> MessageOut:
    message, created = await service.send_attachment(
        session, user, room_id, file, body, client_msg_id
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    return service.to_message_out(message)


@router.get("/rooms/{room_id}/messages/{message_id}/attachment", response_model=DownloadOut)
async def download_attachment(
    room_id: uuid.UUID, message_id: uuid.UUID, user: ChatUser, session: SessionDep
) -> DownloadOut:
    url, ttl = await service.attachment_url(session, user, room_id, message_id)
    return DownloadOut(url=url, expires_in=ttl)


@router.post("/rooms/{room_id}/read", response_model=ReadStateOut, summary="Read receipt")
async def mark_read(
    room_id: uuid.UUID, data: ReadIn, user: ChatUser, session: SessionDep
) -> ReadStateOut:
    return await service.mark_read(session, user, room_id, data.seq)
