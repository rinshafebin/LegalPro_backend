"""Refund policy and refund requests.

Policy (agreed):
  - client cancels >= CANCELLATION_FULL_REFUND_HOURS before the start -> full refund
  - client cancels later (or no-show)                                  -> no refund
  - advocate cancels at any time                                       -> full refund
  - system: payment landed after the slot was lost / booking cancelled -> full refund

Only full refunds are issued, so a retried refund can never over-refund: Razorpay rejects a
second full refund of the same payment.
"""

import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import active_admins, link, notify_many
from app.modules.payments.models import Payment, PaymentStatus, RefundStatus
from app.workers.queue import get_job_queue

logger = get_logger(__name__)


class Canceller(StrEnum):
    CLIENT = "client"
    ADVOCATE = "advocate"
    SYSTEM = "system"


def refundable_paise(
    amount_paise: int, start_at: datetime, cancelled_at: datetime, by: Canceller
) -> int:
    if by is not Canceller.CLIENT:
        return amount_paise
    notice = start_at - cancelled_at
    if notice >= timedelta(hours=get_settings().cancellation_full_refund_hours):
        return amount_paise
    return 0


def refund_job_id(payment_id: uuid.UUID) -> str:
    return f"payment:{payment_id}:refund"


def request_refund(payment: Payment, amount_paise: int, reason: str) -> bool:
    """Mark a captured payment for refund inside the caller's transaction.

    Returns True if a refund job must be enqueued after the caller commits.
    """
    if payment.status is not PaymentStatus.CAPTURED or payment.refund_status in (
        RefundStatus.REQUESTED,
        RefundStatus.PROCESSED,
    ):
        return False
    if amount_paise <= 0:
        payment.refund_status = RefundStatus.NOT_ELIGIBLE
        logger.info("refund_not_eligible", payment_id=str(payment.id), reason=reason)
        return False
    payment.refund_status = RefundStatus.REQUESTED
    payment.refund_amount_paise = amount_paise
    payment.refund_requested_at = datetime.now(UTC)
    logger.info("refund_requested", payment_id=str(payment.id), reason=reason)
    return True


async def enqueue_refund(payment_id: uuid.UUID) -> None:
    await get_job_queue().enqueue(
        "issue_refund", job_id=refund_job_id(payment_id), payment_id=str(payment_id)
    )


async def enqueue_refund_retry(payment_id: uuid.UUID, requested_at: datetime) -> None:
    """Admin retry: a fresh job id, so a lingering earlier job can never block it."""
    await get_job_queue().enqueue(
        "issue_refund",
        job_id=f"{refund_job_id(payment_id)}:retry:{int(requested_at.timestamp())}",
        payment_id=str(payment_id),
    )


async def alert_admins(session: AsyncSession, payment_id: uuid.UUID, status: RefundStatus) -> None:
    """Tell every active admin (in-app + email) that a refund needs a person."""
    subject = "Refund needs attention"
    body = (
        f"A refund could not be completed automatically (status: {status.value}).\n"
        f"Payment: {payment_id}\n\n"
        f"Review it: {link('/admin/refunds')}\n"
    )
    await notify_many(
        session,
        await active_admins(session),
        NotificationType.REFUND_ATTENTION,
        title=subject,
        data={"payment_id": str(payment_id), "refund_status": status.value},
        email=(subject, body),
    )
