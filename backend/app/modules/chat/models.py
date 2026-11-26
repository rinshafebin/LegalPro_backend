import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

MAX_MESSAGE_CHARS = 4000


class ChatRoomKind(StrEnum):
    CASE = "case"
    BOOKING = "booking"


class ChatRoom(Base):
    """One room per case and one per confirmed booking.

    There is no participant table: who may use a room is derived from the case or booking
    on every request, so removing a member or cancelling a booking takes effect at once.
    """

    __tablename__ = "chat_rooms"
    __table_args__ = (
        CheckConstraint(
            "(kind = 'case' AND case_id IS NOT NULL AND booking_id IS NULL) OR "
            "(kind = 'booking' AND booking_id IS NOT NULL AND case_id IS NULL)",
            name="one_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[ChatRoomKind] = mapped_column(
        Enum(
            ChatRoomKind,
            name="chat_room_kind",
            native_enum=False,
            length=20,
            create_constraint=True,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), unique=True
    )
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="CASCADE"), unique=True
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        Index("ix_chat_messages_room_seq", "room_id", "seq"),
        # Lets a client safely resend after a dropped connection: same id, same message.
        UniqueConstraint(
            "room_id", "sender_id", "client_msg_id", name="uq_chat_messages_client_msg"
        ),
        CheckConstraint(
            "deleted_at IS NOT NULL OR body IS NOT NULL OR attachment_key IS NOT NULL",
            name="body_or_attachment",
        ),
        CheckConstraint(f"char_length(body) <= {MAX_MESSAGE_CHARS}", name="body_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Monotonic across all rooms: the ordering key and the pagination cursor.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    room_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_rooms.id", ondelete="CASCADE"))
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    client_msg_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    body: Mapped[str | None] = mapped_column(Text)
    attachment_key: Mapped[str | None] = mapped_column(String(512), unique=True)
    attachment_filename: Mapped[str | None] = mapped_column(String(200))
    attachment_content_type: Mapped[str | None] = mapped_column(String(100))
    attachment_size: Mapped[int | None] = mapped_column(Integer)
    attachment_sha256: Mapped[str | None] = mapped_column(String(64))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Soft delete: the body is cleared (its text goes to the audit log) and the API shows a
    # placeholder; an attachment's object is kept in storage but no longer served.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class ChatReadState(Base):
    """How far each user has read in each room (read receipts and unread counts)."""

    __tablename__ = "chat_read_states"

    room_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chat_rooms.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    last_read_seq: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
