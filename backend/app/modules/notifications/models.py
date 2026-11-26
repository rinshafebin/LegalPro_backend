import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Uuid, func, text, true
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class NotificationType(StrEnum):
    BOOKING_CONFIRMED = "booking.confirmed"
    BOOKING_CANCELLED = "booking.cancelled"
    BOOKING_RESCHEDULED = "booking.rescheduled"
    BOOKING_REMINDER = "booking.reminder"
    CASE_OPENED = "case.opened"
    CASE_MEMBER_ADDED = "case.member_added"
    HEARING_REMINDER = "hearing.reminder"
    CASE_STATUS_CHANGED = "case.status_changed"
    CASE_NOTE_ADDED = "case.note_added"  # non-private notes only
    CASE_DOCUMENT_ADDED = "case.document_added"  # client-visible documents only
    CASE_HEARING_CHANGED = "case.hearing_changed"  # added, moved or status changed
    CHAT_MESSAGE = "chat.message"
    REFUND_ATTENTION = "refund.attention"  # admins only


class Notification(Base):
    """In-app notification. `data` holds ids and non-privileged facts only."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index(
            "ix_notifications_user_unread",
            "user_id",
            postgresql_where=text("read_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    # Plain string (no CHECK constraint) so adding a type needs no migration.
    type: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # clock_timestamp(), not now(): several notifications made in one transaction still sort
    # in the order they were created.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )

    __mapper_args__ = {"eager_defaults": True}


class NotificationPreference(Base):
    """Per-type email opt-out. No row means email is on."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    type: Mapped[str] = mapped_column(String(50), primary_key=True)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
