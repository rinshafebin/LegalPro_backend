import uuid

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
