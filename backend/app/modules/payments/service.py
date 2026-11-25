import hashlib
import json
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.modules.bookings import service as bookings_service
from app.modules.bookings.models import Booking, BookingStatus
from app.modules.bookings.service import ConfirmResult
from app.modules.payments.gateway import (
    PaymentGatewayError,
    get_gateway,
    verify_checkout_signature,
    verify_webhook_signature,
)
from app.modules.payments.models import Payment, PaymentStatus, RefundStatus, WebhookEvent
from app.modules.payments.refunds import enqueue_refund, request_refund
from app.modules.payments.schemas import CheckoutVerifyIn, OrderOut, PaymentOut
from app.modules.users.models import User

logger = get_logger(__name__)

CAPTURE_EVENTS = frozenset({"payment.captured", "order.paid"})
REFUND_EVENTS = frozenset({"refund.processed", "refund.failed"})


def to_paise(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------- orders


async def _client_booking(session: AsyncSession, client: User, booking_id: uuid.UUID) -> Booking:
    booking = await session.scalar(
        select(Booking).where(Booking.id == booking_id, Booking.client_id == client.id)
    )
    if booking is None:
        raise NotFoundError("Booking not found")
    return booking


async def create_order(session: AsyncSession, client: User, booking_id: uuid.UUID) -> OrderOut:
    """Create (or return the existing) Razorpay order for a pending booking.

    The amount comes only from the fee copied onto the booking; the client never sends it.
    """
    booking = await _client_booking(session, client, booking_id)
    expired = booking.expires_at is not None and booking.expires_at <= datetime.now(UTC)
    if booking.status is not BookingStatus.PENDING_PAYMENT or expired:
        raise ConflictError("This booking can no longer be paid for", code="not_payable")

    gateway = get_gateway()
    payment = await session.scalar(select(Payment).where(Payment.booking_id == booking.id))
    if payment is None:
        amount = to_paise(booking.fee_amount)
        order_id = await gateway.create_order(
            amount_paise=amount,
            currency=booking.currency,
            receipt=str(booking.id),
            notes={"booking_id": str(booking.id)},
        )
        payment = Payment(
            booking_id=booking.id,
            razorpay_order_id=order_id,
            amount_paise=amount,
            currency=booking.currency,
            status=PaymentStatus.CREATED,
        )
        session.add(payment)
        try:
            await session.commit()
        except IntegrityError:  # a concurrent request created it first
            await session.rollback()
            payment = await session.scalar(select(Payment).where(Payment.booking_id == booking.id))
            assert payment is not None  # noqa: S101
    return OrderOut(
        booking_id=booking.id,
        order_id=payment.razorpay_order_id,
        amount=payment.amount_paise,
        currency=payment.currency,
        key_id=gateway.key_id,
    )


# ---------------------------------------------------------------- capture


async def _capture(session: AsyncSession, payment: Payment, razorpay_payment_id: str) -> str:
    """Record a captured payment and confirm its booking. Idempotent. Commits."""
    if payment.status is PaymentStatus.CAPTURED:
        await session.commit()
        return "already_captured"

    payment.status = PaymentStatus.CAPTURED
    payment.razorpay_payment_id = razorpay_payment_id
    payment.captured_at = datetime.now(UTC)
    booking = await session.get(Booking, payment.booking_id, with_for_update=True)
    assert booking is not None  # noqa: S101 - FK
    result = await bookings_service.confirm_after_payment(session, booking)
    refund_needed = False
    if result in (ConfirmResult.SLOT_LOST, ConfirmResult.NOT_CONFIRMABLE):
        # The client paid but can't get the consultation: refund in full automatically.
        logger.warning(
            "payment_needs_refund",
            booking_id=str(booking.id),
            reason=result.value,
            booking_status=booking.status.value,
        )
        refund_needed = request_refund(payment, payment.amount_paise, reason=result.value)
    await session.commit()
    if refund_needed:
        await enqueue_refund(payment.id)
    if result is ConfirmResult.CONFIRMED:
        await bookings_service.after_confirmed(session, booking)
    logger.info("payment_captured", booking_id=str(booking.id), result=result.value)
    return result.value


async def verify_checkout(
    session: AsyncSession, client: User, data: CheckoutVerifyIn
) -> PaymentOut:
    """Called by the client after Razorpay Checkout succeeds. The webhook is the backstop."""
    payment = await session.scalar(
        select(Payment)
        .join(Booking, Booking.id == Payment.booking_id)
        .where(Payment.razorpay_order_id == data.razorpay_order_id, Booking.client_id == client.id)
        .with_for_update(of=Payment)
    )
    if payment is None:
        raise NotFoundError("Payment not found")
    if not verify_checkout_signature(
        data.razorpay_order_id, data.razorpay_payment_id, data.razorpay_signature
    ):
        logger.warning("checkout_signature_rejected", order_id=data.razorpay_order_id)
        raise BadRequestError("Invalid payment signature", code="invalid_signature")

    await _capture(session, payment, data.razorpay_payment_id)
    booking = await session.get(Booking, payment.booking_id)
    assert booking is not None  # noqa: S101
    return PaymentOut(
        booking_id=booking.id,
        status=payment.status,
        refund_status=payment.refund_status,
        booking_status=booking.status.value,
    )


# ---------------------------------------------------------------- refunds


async def issue_refund(session: AsyncSession, payment_id: uuid.UUID, *, final_attempt: bool) -> str:
    """Background job body: send a requested refund to Razorpay. Safe to retry."""
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if (
        payment is None
        or payment.refund_status is not RefundStatus.REQUESTED
        or payment.razorpay_refund_id is not None
    ):
        await session.commit()
        return "skipped"
    if payment.razorpay_payment_id is None or payment.refund_amount_paise is None:
        payment.refund_status = RefundStatus.PENDING_REVIEW
        await session.commit()
        logger.error("refund_needs_review", payment_id=str(payment.id), reason="missing_ids")
        return "pending_review"

    try:
        refund_id, status = await get_gateway().refund(
            payment_id=payment.razorpay_payment_id,
            amount_paise=payment.refund_amount_paise,
            notes={"booking_id": str(payment.booking_id)},
        )
    except PaymentGatewayError:
        if not final_attempt:
            await session.rollback()
            raise  # SAQ retries with backoff
        payment.refund_status = RefundStatus.FAILED
        await session.commit()
        logger.error("refund_failed", payment_id=str(payment.id))
        return "failed"

    payment.razorpay_refund_id = refund_id
    if status == "processed":
        payment.refund_status = RefundStatus.PROCESSED
        payment.refunded_at = datetime.now(UTC)
    await session.commit()
    logger.info("refund_sent", payment_id=str(payment.id), gateway_status=status)
    return status


async def _process_refund_event(
    session: AsyncSession, event_type: str, event: dict[str, Any]
) -> str:
    try:
        entity = event["payload"]["refund"]["entity"]
        refund_id = str(entity["id"])
        payment_id = str(entity["payment_id"])
        amount = int(entity["amount"])
    except (KeyError, TypeError, ValueError):
        return _reject("malformed_payload", event_type=event_type)

    payment = await session.scalar(
        select(Payment).where(Payment.razorpay_payment_id == payment_id).with_for_update()
    )
    if payment is None:
        return _reject("unknown_payment", event_type=event_type, payment_id=payment_id)
    if payment.refund_amount_paise is not None and amount != payment.refund_amount_paise:
        return _reject(
            "refund_amount_mismatch",
            event_type=event_type,
            expected=payment.refund_amount_paise,
            received=amount,
        )
    if payment.razorpay_refund_id not in (None, refund_id):
        return _reject("unknown_refund", event_type=event_type, refund_id=refund_id)

    payment.razorpay_refund_id = refund_id
    if event_type == "refund.processed":
        if payment.refund_status is RefundStatus.PROCESSED:
            return "already_processed"
        payment.refund_status = RefundStatus.PROCESSED
        payment.refunded_at = datetime.now(UTC)
        return "refund_processed"
    payment.refund_status = RefundStatus.FAILED
    logger.error("refund_failed", payment_id=str(payment.id), via="webhook")
    return "refund_failed"


# ---------------------------------------------------------------- webhook


def _reject(reason: str, **context: Any) -> str:
    logger.warning("webhook_rejected", reason=reason, **context)
    return f"rejected_{reason}"


async def _process_event(session: AsyncSession, event_type: str, event: dict[str, Any]) -> str:
    if event_type in REFUND_EVENTS:
        return await _process_refund_event(session, event_type, event)
    if event_type not in CAPTURE_EVENTS and event_type != "payment.failed":
        return "ignored"
    try:
        entity = event["payload"]["payment"]["entity"]
        order_id = str(entity["order_id"])
        payment_id = str(entity["id"])
        amount = int(entity["amount"])
        currency = str(entity["currency"])
    except (KeyError, TypeError, ValueError):
        return _reject("malformed_payload", event_type=event_type)

    payment = await session.scalar(
        select(Payment).where(Payment.razorpay_order_id == order_id).with_for_update()
    )
    if payment is None:
        return _reject("unknown_order", event_type=event_type, order_id=order_id)
    if amount != payment.amount_paise or currency != payment.currency:
        return _reject(
            "amount_mismatch",
            event_type=event_type,
            order_id=order_id,
            expected=payment.amount_paise,
            received=amount,
            currency=currency,
        )

    if event_type == "payment.failed":
        if payment.status is PaymentStatus.CREATED:
            payment.status = PaymentStatus.FAILED  # informational; a later retry can still pay
        return "payment_failed"
    return await _capture(session, payment, payment_id)


async def handle_webhook(
    session: AsyncSession, body: bytes, signature: str | None, event_id: str | None
) -> str:
    if not verify_webhook_signature(body, signature):
        logger.warning("webhook_rejected", reason="bad_signature", event_id=event_id)
        raise BadRequestError("Invalid webhook signature", code="invalid_signature")
    try:
        event = json.loads(body)
        event_type = str(event["event"])
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("webhook_rejected", reason="malformed_json", event_id=event_id)
        raise BadRequestError("Malformed webhook payload") from exc

    event_id = event_id or "sha256:" + hashlib.sha256(body).hexdigest()
    # Claim the event id; a redelivered event finds it taken and changes nothing.
    claimed = await session.execute(
        pg_insert(WebhookEvent)
        .values(id=event_id, event_type=event_type, outcome="processing")
        .on_conflict_do_nothing(index_elements=[WebhookEvent.id])
        .returning(WebhookEvent.id)
    )
    if claimed.scalar_one_or_none() is None:
        await session.rollback()
        logger.info("webhook_duplicate", event_id=event_id, event_type=event_type)
        return "duplicate"

    # The event row commits in the same transaction as its effects, so a crash mid-way
    # rolls both back and Razorpay's retry processes it again.
    outcome = await _process_event(session, event_type, event)
    record = await session.get(WebhookEvent, event_id)
    if record is not None:
        record.outcome = outcome[:40]
    await session.commit()
    logger.info("webhook_processed", event_id=event_id, event_type=event_type, outcome=outcome)
    return outcome
