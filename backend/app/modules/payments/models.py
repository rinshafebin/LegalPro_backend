import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin


class PaymentStatus(StrEnum):
    CREATED = "created"  # Razorpay order exists, nothing paid yet
    CAPTURED = "captured"
    FAILED = "failed"


class RefundStatus(StrEnum):
    NONE = "none"
    NOT_ELIGIBLE = "not_eligible"  # cancelled too late under the refund policy
    REQUESTED = "requested"  # refund job queued / sent to Razorpay, awaiting processing
    PROCESSED = "processed"
    FAILED = "failed"  # Razorpay refused or kept failing; needs an admin
    PENDING_REVIEW = "pending_review"  # can't be refunded automatically; needs an admin
    RESOLVED = "resolved"  # an admin handled it outside the system (see resolution note)


def _enum(enum_cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=20,
        create_constraint=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    booking_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("bookings.id", ondelete="RESTRICT"), unique=True
    )
    razorpay_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Always computed on the server from the booking's copied fee.
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[PaymentStatus] = mapped_column(_enum(PaymentStatus, "payment_status"))
    refund_status: Mapped[RefundStatus] = mapped_column(
        _enum(RefundStatus, "refund_status"), default=RefundStatus.NONE, server_default="none"
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    refund_amount_paise: Mapped[int | None] = mapped_column(Integer)
    razorpay_refund_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    refund_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_resolved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    refund_resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refund_resolution_note: Mapped[str | None] = mapped_column(Text)


class WebhookEvent(Base):
    """One row per processed webhook delivery; makes redelivery a no-op."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)  # Razorpay event id
    event_type: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(40))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
