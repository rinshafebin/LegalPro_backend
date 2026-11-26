import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.payments.models import PaymentStatus, RefundStatus


class OrderCreateIn(BaseModel):
    booking_id: uuid.UUID


class OrderOut(BaseModel):
    """Everything Razorpay Checkout needs. The amount is set by the server only."""

    booking_id: uuid.UUID
    order_id: str
    amount: int = Field(description="Amount in the smallest currency unit (paise)")
    currency: str
    key_id: str


class CheckoutVerifyIn(BaseModel):
    razorpay_order_id: str = Field(max_length=64)
    razorpay_payment_id: str = Field(max_length=64)
    razorpay_signature: str = Field(max_length=128)


class PaymentOut(BaseModel):
    booking_id: uuid.UUID
    status: PaymentStatus
    refund_status: RefundStatus
    booking_status: str


class WebhookOut(BaseModel):
    status: str


class RefundIssueOut(BaseModel):
    """A refund that needs an admin (failed or pending review)."""

    payment_id: uuid.UUID
    booking_id: uuid.UUID
    client_id: uuid.UUID
    client_email: str
    amount_paise: int
    refund_amount_paise: int | None
    currency: str
    refund_status: RefundStatus
    razorpay_payment_id: str | None
    razorpay_refund_id: str | None
    refund_requested_at: datetime | None
    updated_at: datetime
    refund_resolution_note: str | None


class RefundResolveIn(BaseModel):
    note: str = Field(
        min_length=3,
        max_length=1000,
        description="What was done, e.g. 'Refunded manually in the Razorpay dashboard, rfnd_...'",
    )
