import uuid
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Time,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class BookingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"
    CONFIRMED = "confirmed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


# Bookings in these states hold their time slot.
ACTIVE_STATUSES = (BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED)
_ACTIVE_SQL = "status IN ('pending_payment', 'confirmed')"


class AvailabilityRule(TimestampMixin, Base):
    """A weekly window, in the advocate's own timezone, split into fixed-length slots."""

    __tablename__ = "availability_rules"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday_range"),
        CheckConstraint("end_time > start_time", name="end_after_start"),
        CheckConstraint("slot_minutes BETWEEN 15 AND 240", name="slot_minutes_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    advocate_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger)  # 0 = Monday ... 6 = Sunday
    start_time: Mapped[time] = mapped_column(Time)  # local wall-clock time
    end_time: Mapped[time] = mapped_column(Time)
    slot_minutes: Mapped[int] = mapped_column(Integer)


class Booking(TimestampMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="end_after_start"),
        CheckConstraint("fee_amount >= 0", name="fee_non_negative"),
        # The database itself refuses overlapping active bookings, even under concurrency.
        ExcludeConstraint(
            ("advocate_id", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            where=text(_ACTIVE_SQL),
            using="gist",
            name="ex_bookings_advocate_overlap",
        ),
        ExcludeConstraint(
            ("client_id", "="),
            (text("tstzrange(start_at, end_at, '[)')"), "&&"),
            where=text(_ACTIVE_SQL),
            using="gist",
            name="ex_bookings_client_overlap",
        ),
        Index("ix_bookings_advocate_start", "advocate_id", "start_at"),
        Index("ix_bookings_client_start", "client_id", "start_at"),
        Index(
            "ix_bookings_pending_expiry",
            "expires_at",
            postgresql_where=text("status = 'pending_payment'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # RESTRICT: booking (financial) records must not disappear with a user.
    client_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    advocate_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # UTC
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # UTC
    status: Mapped[BookingStatus] = mapped_column(
        Enum(
            BookingStatus,
            name="booking_status",
            native_enum=False,
            length=20,
            create_constraint=True,
            values_callable=lambda e: [m.value for m in e],
        ),
        index=True,
    )
    # Copied from the advocate's profile at creation; later fee changes don't affect it.
    fee_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3))

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    cancel_reason: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
