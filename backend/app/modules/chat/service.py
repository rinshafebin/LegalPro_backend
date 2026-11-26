"""Chat: rooms, messages, read receipts, unread counts.

Every operation re-checks access through `chat.access` (on every message, not once per
connection). After a message is committed it is published once per recipient user over
Redis (`realtime.pubsub`); the recipients are computed from the database at that moment.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import UploadFile
from sqlalchemy import ColumnElement, and_, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams
from app.core.rate_limit import hit
from app.core.redis import get_redis
from app.core.storage import get_storage
from app.modules.advocates.models import AdvocateProfile
from app.modules.bookings.models import Booking
from app.modules.cases import files
from app.modules.cases.models import Case
from app.modules.chat.access import (
    get_room_for,
    participant_ids,
    require_writable,
    room_visible_to,
)
from app.modules.chat.models import ChatMessage, ChatReadState, ChatRoom, ChatRoomKind
from app.modules.chat.schemas import (
    AttachmentOut,
    MessageOut,
    ParticipantOut,
    ReadStateOut,
    RoomDetailOut,
    RoomOut,
)
from app.modules.clients.models import ClientProfile
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import link, notify
from app.modules.realtime.pubsub import publish
from app.modules.users.models import User

logger = get_logger(__name__)

MAX_PAGE = 100


# ---------------------------------------------------------------- rooms


def case_room(case_id: uuid.UUID) -> ChatRoom:
    """Added in the same transaction that creates the case."""
    return ChatRoom(kind=ChatRoomKind.CASE, case_id=case_id)


async def ensure_booking_room(session: AsyncSession, booking_id: uuid.UUID) -> None:
    """Idempotent; called whenever a booking becomes confirmed. Commits."""
    await session.execute(
        pg_insert(ChatRoom)
        .values(id=uuid.uuid4(), kind=ChatRoomKind.BOOKING, booking_id=booking_id)
        .on_conflict_do_nothing(index_elements=[ChatRoom.booking_id])
    )
    await session.commit()


def _unread_filter(user: User) -> ColumnElement[bool]:
    return and_(
        ChatMessage.room_id == ChatRoom.id,
        ChatMessage.sender_id != user.id,
        ChatMessage.seq > func.coalesce(ChatReadState.last_read_seq, 0),
    )


def _rooms_with_unread(user: User):  # type: ignore[no-untyped-def]
    unread = (
        select(func.count()).select_from(ChatMessage).where(_unread_filter(user))
    ).scalar_subquery()
    return (
        select(ChatRoom, unread.label("unread"), func.coalesce(ChatReadState.last_read_seq, 0))
        .outerjoin(
            ChatReadState,
            and_(ChatReadState.room_id == ChatRoom.id, ChatReadState.user_id == user.id),
        )
        .where(room_visible_to(user))
    )


async def _titles(
    session: AsyncSession, user: User, rooms: list[ChatRoom]
) -> dict[uuid.UUID, tuple[str, str | None]]:
    """Room title (and case number) as the user should see it."""
    case_ids = [r.case_id for r in rooms if r.case_id]
    booking_ids = [r.booking_id for r in rooms if r.booking_id]
    cases = {
        c.id: c for c in (await session.scalars(select(Case).where(Case.id.in_(case_ids)))).all()
    }
    bookings = {
        b.id: b
        for b in (await session.scalars(select(Booking).where(Booking.id.in_(booking_ids)))).all()
    }
    other_ids = {
        b.advocate_id if b.client_id == user.id else b.client_id for b in bookings.values()
    }
    names = dict(
        (
            await session.execute(
                select(AdvocateProfile.user_id, AdvocateProfile.full_name).where(
                    AdvocateProfile.user_id.in_(other_ids)
                )
            )
        ).all()
    )
    names |= dict(
        (
            await session.execute(
                select(ClientProfile.user_id, ClientProfile.full_name).where(
                    ClientProfile.user_id.in_(other_ids)
                )
            )
        ).all()
    )
    out: dict[uuid.UUID, tuple[str, str | None]] = {}
    for room in rooms:
        if room.case_id and room.case_id in cases:
            case = cases[room.case_id]
            out[room.id] = (f"{case.case_number} · {case.title}", case.case_number)
        elif room.booking_id and room.booking_id in bookings:
            b = bookings[room.booking_id]
            other = b.advocate_id if b.client_id == user.id else b.client_id
            out[room.id] = (f"Consultation with {names.get(other, 'participant')}", None)
    return out


def _room_out(room: ChatRoom, unread: int, last_read: int, title: tuple[str, str | None]) -> RoomOut:
    return RoomOut(
        id=room.id,
        kind=room.kind,
        case_id=room.case_id,
        case_number=title[1],
        booking_id=room.booking_id,
        title=title[0],
        last_message_at=room.last_message_at,
        unread_count=unread,
        last_read_seq=last_read,
    )


async def list_rooms(
    session: AsyncSession, user: User, page: PageParams
) -> tuple[list[RoomOut], int]:
    stmt = _rooms_with_unread(user).order_by(
        ChatRoom.last_message_at.desc().nulls_last(), ChatRoom.created_at.desc(), ChatRoom.id
    )
    total = await session.scalar(
        select(func.count()).select_from(ChatRoom).where(room_visible_to(user))
    )
    rows = (await session.execute(stmt.limit(page.page_size).offset(page.offset))).all()
    titles = await _titles(session, user, [r[0] for r in rows])
    return [
        _room_out(room, int(unread), int(last_read), titles.get(room.id, ("Chat", None)))
        for room, unread, last_read in rows
    ], int(total or 0)


async def get_room(session: AsyncSession, user: User, room_id: uuid.UUID) -> RoomDetailOut:
    room = await get_room_for(session, user, room_id)
    row = (await session.execute(_rooms_with_unread(user).where(ChatRoom.id == room.id))).one()
    title = (await _titles(session, user, [room])).get(room.id, ("Chat", None))
    ids = await participant_ids(session, room)
    reads = dict(
        (
            await session.execute(
                select(ChatReadState.user_id, ChatReadState.last_read_seq).where(
                    ChatReadState.room_id == room.id
                )
            )
        ).all()
    )
    names = dict(
        (
            await session.execute(
                select(AdvocateProfile.user_id, AdvocateProfile.full_name).where(
                    AdvocateProfile.user_id.in_(ids)
                )
            )
        ).all()
    ) | dict(
        (
            await session.execute(
                select(ClientProfile.user_id, ClientProfile.full_name).where(
                    ClientProfile.user_id.in_(ids)
                )
            )
        ).all()
    )
    try:
        await require_writable(session, room)
        writable = True
    except ConflictError:
        writable = False
    base = _room_out(room, int(row[1]), int(row[2]), title)
    return RoomDetailOut(
        **base.model_dump(),
        participants=[
            ParticipantOut(id=i, full_name=names.get(i, ""), last_read_seq=int(reads.get(i, 0)))
            for i in ids
        ],
        writable=writable,
    )


async def unread_total(session: AsyncSession, user: User) -> int:
    sub = _rooms_with_unread(user).subquery()
    total = await session.scalar(select(func.coalesce(func.sum(sub.c.unread), 0)))
    return int(total or 0)


# ---------------------------------------------------------------- messages


def to_message_out(m: ChatMessage) -> MessageOut:
    attachment = None
    if m.attachment_key is not None:
        attachment = AttachmentOut(
            filename=m.attachment_filename or "attachment",
            content_type=m.attachment_content_type or "application/octet-stream",
            size_bytes=m.attachment_size or 0,
        )
    return MessageOut(
        id=m.id,
        seq=m.seq,
        room_id=m.room_id,
        sender_id=m.sender_id,
        client_msg_id=m.client_msg_id,
        body=m.body,
        attachment=attachment,
        created_at=m.created_at,
    )


async def list_messages(
    session: AsyncSession,
    user: User,
    room_id: uuid.UUID,
    *,
    before: int | None,
    after: int | None,
    limit: int,
) -> tuple[list[MessageOut], int | None]:
    """`before`: older history, newest first. `after`: catch up after a reconnect, oldest first."""
    room = await get_room_for(session, user, room_id)
    limit = max(1, min(limit, MAX_PAGE))
    stmt = select(ChatMessage).where(ChatMessage.room_id == room.id)
    if after is not None:
        stmt = stmt.where(ChatMessage.seq > after).order_by(ChatMessage.seq.asc())
    else:
        if before is not None:
            stmt = stmt.where(ChatMessage.seq < before)
        stmt = stmt.order_by(ChatMessage.seq.desc())
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = rows[-1].seq if has_more and rows else None
    return [to_message_out(m) for m in rows], next_cursor


@dataclass(frozen=True)
class _Attachment:
    key: str
    filename: str
    content_type: str
    size: int
    sha256: str


async def _rate_limit(user: User) -> None:
    s = get_settings()
    await hit(f"chat:user:{user.id}", s.chat_rate_limit_messages, s.chat_rate_limit_window_seconds)


async def _existing(
    session: AsyncSession, room_id: uuid.UUID, user: User, client_msg_id: uuid.UUID | None
) -> ChatMessage | None:
    if client_msg_id is None:
        return None
    return await session.scalar(
        select(ChatMessage).where(
            ChatMessage.room_id == room_id,
            ChatMessage.sender_id == user.id,
            ChatMessage.client_msg_id == client_msg_id,
        )
    )


async def _create(
    session: AsyncSession,
    user: User,
    room: ChatRoom,
    body: str | None,
    client_msg_id: uuid.UUID | None,
    attachment: _Attachment | None = None,
) -> tuple[ChatMessage, bool]:
    """Insert, commit, then fan out. Returns (message, created)."""
    message = ChatMessage(
        room_id=room.id,
        sender_id=user.id,  # never from the payload
        client_msg_id=client_msg_id,
        body=body,
    )
    if attachment is not None:
        message.attachment_key = attachment.key
        message.attachment_filename = attachment.filename
        message.attachment_content_type = attachment.content_type
        message.attachment_size = attachment.size
        message.attachment_sha256 = attachment.sha256
    session.add(message)
    try:
        await session.flush()
    except IntegrityError:
        # Same client_msg_id sent twice at once: the other request won.
        await session.rollback()
        existing = await _existing(session, room.id, user, client_msg_id)
        if existing is None:
            raise
        return existing, False
    await session.execute(
        update(ChatRoom).where(ChatRoom.id == room.id).values(last_message_at=message.created_at)
    )
    await _upsert_read(session, room.id, user.id, message.seq)  # you've read your own message
    await session.commit()

    recipients = await participant_ids(session, room)
    out = to_message_out(message).model_dump(mode="json")
    await publish(recipients, {"type": "chat.message", "message": out})
    await _notify_offline_recipients(session, room, user, [r for r in recipients if r != user.id])
    logger.info("chat_message_sent", room_id=str(room.id), seq=message.seq)
    return message, True


async def send_message(
    session: AsyncSession,
    user: User,
    room_id: uuid.UUID,
    body: str,
    client_msg_id: uuid.UUID | None = None,
) -> tuple[ChatMessage, bool]:
    await _rate_limit(user)
    room = await get_room_for(session, user, room_id)  # membership, on every message
    await require_writable(session, room)
    existing = await _existing(session, room.id, user, client_msg_id)
    if existing is not None:
        return existing, False
    return await _create(session, user, room, body, client_msg_id)


async def send_attachment(
    session: AsyncSession,
    user: User,
    room_id: uuid.UUID,
    upload: UploadFile,
    body: str | None,
    client_msg_id: uuid.UUID | None = None,
) -> tuple[ChatMessage, bool]:
    """Same validation as case documents: size limit while streaming, type by content."""
    await _rate_limit(user)
    room = await get_room_for(session, user, room_id)
    await require_writable(session, room)
    existing = await _existing(session, room.id, user, client_msg_id)
    if existing is not None:
        return existing, False

    max_bytes = get_settings().chat_attachment_max_mb * 1024 * 1024
    data = await files.read_limited(upload, max_bytes)
    content_type = files.detect_type(data, upload.filename or "")
    key = f"chat/{room.id}/{uuid.uuid4()}{files.ALLOWED_TYPES[content_type]}"
    await get_storage().put(key, data, content_type)
    attachment = _Attachment(
        key=key,
        filename=files.safe_filename(upload.filename, content_type),
        content_type=content_type,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    try:
        message, created = await _create(session, user, room, body, client_msg_id, attachment)
    except Exception:
        await get_storage().delete(key)
        raise
    if not created:
        await get_storage().delete(key)
    return message, created


async def attachment_url(
    session: AsyncSession, user: User, room_id: uuid.UUID, message_id: uuid.UUID
) -> tuple[str, int]:
    room = await get_room_for(session, user, room_id)
    message = await session.scalar(
        select(ChatMessage).where(ChatMessage.id == message_id, ChatMessage.room_id == room.id)
    )
    if message is None or message.attachment_key is None:
        raise NotFoundError("Attachment not found")
    ttl = get_settings().s3_presign_ttl_seconds
    url = await get_storage().presigned_get(
        message.attachment_key,
        filename=message.attachment_filename or "attachment",
        content_type=message.attachment_content_type or "application/octet-stream",
        ttl_seconds=ttl,
    )
    return url, ttl


# ---------------------------------------------------------------- read receipts


async def _upsert_read(
    session: AsyncSession, room_id: uuid.UUID, user_id: uuid.UUID, seq: int
) -> int:
    """Move the read marker forward only (never backwards). Returns the stored value."""
    stmt = pg_insert(ChatReadState).values(room_id=room_id, user_id=user_id, last_read_seq=seq)
    stored = await session.scalar(
        stmt.on_conflict_do_update(
            index_elements=[ChatReadState.room_id, ChatReadState.user_id],
            set_={
                "last_read_seq": func.greatest(ChatReadState.last_read_seq, stmt.excluded.last_read_seq),
                "updated_at": func.now(),
            },
        ).returning(ChatReadState.last_read_seq)
    )
    return int(stored or 0)


async def mark_read(
    session: AsyncSession, user: User, room_id: uuid.UUID, seq: int
) -> ReadStateOut:
    room = await get_room_for(session, user, room_id)
    newest = await session.scalar(
        select(func.max(ChatMessage.seq)).where(ChatMessage.room_id == room.id)
    )
    seq = min(seq, int(newest or 0))  # can't have read messages that don't exist
    previous = await session.scalar(
        select(ChatReadState.last_read_seq).where(
            ChatReadState.room_id == room.id, ChatReadState.user_id == user.id
        )
    )
    stored = await _upsert_read(session, room.id, user.id, seq)
    await session.commit()
    state = ReadStateOut(room_id=room.id, user_id=user.id, last_read_seq=stored)
    if stored != (previous or 0):
        recipients = await participant_ids(session, room)
        await publish(recipients, {"type": "chat.read", **state.model_dump(mode="json")})
    return state


# ---------------------------------------------------------------- notifications


async def _notify_offline_recipients(
    session: AsyncSession, room: ChatRoom, sender: User, recipient_ids: list[uuid.UUID]
) -> None:
    """At most one 'new messages' notification per room per recipient per throttle window.

    Neither the in-app notification nor the email carries the message text.
    """
    if not recipient_ids:
        return
    window = timedelta(minutes=get_settings().chat_notify_throttle_minutes)
    label, path = await _room_label(session, room)
    redis = get_redis()
    for recipient_id in recipient_ids:
        key = f"chat:notified:{recipient_id}:{room.id}"
        if not await redis.set(key, "1", nx=True, ex=int(window.total_seconds())):
            continue
        recipient = await session.get(User, recipient_id)
        if recipient is None:
            continue
        await notify(
            session,
            recipient,
            NotificationType.CHAT_MESSAGE,
            title=f"New messages in {label}",
            data={"room_id": str(room.id), "case_id": str(room.case_id) if room.case_id else None},
            email=(
                f"New messages in {label}",
                f"You have new messages in {label}.\n\nOpen the conversation: {link(path)}\n",
            ),
        )


async def _room_label(session: AsyncSession, room: ChatRoom) -> tuple[str, str]:
    """A non-privileged label: the case number, never the case title."""
    if room.case_id is not None:
        number = await session.scalar(select(Case.case_number).where(Case.id == room.case_id))
        return f"case {number}", f"/chat/{room.id}"
    return "your consultation chat", f"/chat/{room.id}"

