import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Sequence,
    String,
    Text,
    Uuid,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin

# Case numbers come from a DB sequence: unique even under concurrent creation.
case_number_seq = Sequence("case_number_seq", metadata=Base.metadata)


class CaseStatus(StrEnum):
    OPEN = "open"
    ON_HOLD = "on_hold"
    CLOSED = "closed"


class CasePriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CaseResult(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    SETTLED = "settled"
    WITHDRAWN = "withdrawn"
    DISMISSED = "dismissed"


class CaseMemberRole(StrEnum):
    ASSOCIATE = "associate"
    REVIEWER = "reviewer"


class HearingStatus(StrEnum):
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    ADJOURNED = "adjourned"
    CANCELLED = "cancelled"


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=20,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Case(TimestampMixin, Base):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_number: Mapped[str] = mapped_column(String(30), unique=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    lead_advocate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True
    )
    # The consultation the case came from; one case per booking.
    booking_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("bookings.id", ondelete="SET NULL"), unique=True
    )
    status: Mapped[CaseStatus] = mapped_column(
        _enum(CaseStatus, "case_status"), default=CaseStatus.OPEN, index=True
    )
    priority: Mapped[CasePriority] = mapped_column(
        _enum(CasePriority, "case_priority"), default=CasePriority.MEDIUM
    )
    result: Mapped[CaseResult] = mapped_column(
        _enum(CaseResult, "case_result"), default=CaseResult.PENDING
    )
    court_name: Mapped[str | None] = mapped_column(String(200))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CaseMember(Base):
    """Advocates working on a case besides the lead."""

    __tablename__ = "case_members"

    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), primary_key=True
    )
    advocate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[CaseMemberRole] = mapped_column(_enum(CaseMemberRole, "case_member_role"))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class CaseNote(TimestampMixin, Base):
    __tablename__ = "case_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    body: Mapped[str] = mapped_column(Text)
    # Private notes are visible to the advocates on the case, never to the client.
    is_private: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class CaseDocument(Base):
    __tablename__ = "case_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    uploaded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    storage_key: Mapped[str] = mapped_column(String(512), unique=True)
    filename: Mapped[str] = mapped_column(String(200))  # sanitized display name
    content_type: Mapped[str] = mapped_column(String(100))  # detected from content
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    visible_to_client: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __mapper_args__ = {"eager_defaults": True}


class Hearing(TimestampMixin, Base):
    __tablename__ = "hearings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cases.id", ondelete="CASCADE"), index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)  # UTC
    court: Mapped[str | None] = mapped_column(String(200))
    purpose: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[HearingStatus] = mapped_column(
        _enum(HearingStatus, "hearing_status"), default=HearingStatus.SCHEDULED
    )
    notes: Mapped[str | None] = mapped_column(Text)
