import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Index, String, Uuid, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class AuditAction(StrEnum):
    CASE_CREATED = "case.created"
    CASE_VIEWED = "case.viewed"
    CASE_STATUS_CHANGED = "case.status_changed"
    MEMBER_ADDED = "case.member_added"
    MEMBER_REMOVED = "case.member_removed"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_DOWNLOADED = "document.downloaded"
    DOCUMENT_DELETED = "document.deleted"
    ADMIN_VIEWED = "admin.viewed"
    ADVOCATE_VERIFIED = "advocate.verified"
    ADVOCATE_UNVERIFIED = "advocate.unverified"
    SPECIALIZATION_CREATED = "specialization.created"
    USER_DEACTIVATED = "user.deactivated"
    USER_REACTIVATED = "user.reactivated"
    USER_DELETION_REQUESTED = "user.deletion_requested"
    USER_DELETION_CANCELLED = "user.deletion_cancelled"
    USER_DELETED = "user.deleted"
    USER_DATA_EXPORTED = "user.data_exported"
    CHAT_MESSAGE_EDITED = "chat.message_edited"
    CHAT_MESSAGE_DELETED = "chat.message_deleted"
    REFUND_RETRIED = "payment.refund_retried"
    REFUND_MARKED_HANDLED = "payment.refund_marked_handled"


class AuditLog(Base):
    """Append-only: a database trigger rejects UPDATE, DELETE and TRUNCATE.

    Holds who/what/when/where. The one exception is the original text of an edited or
    deleted chat message (`details.previous_body`), kept for disputes; the admin listing
    never returns it. `entity_id` has no foreign key on purpose: the entry must outlive
    the document or member it refers to.
    """

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_case_occurred", "case_id", "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(50), index=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="RESTRICT"))
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    ip: Mapped[str | None] = mapped_column(INET)
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __mapper_args__ = {"eager_defaults": True}
