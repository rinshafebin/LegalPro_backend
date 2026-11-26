"""Admin handling of refunds that couldn't complete automatically."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.pagination import PageParams, paginate
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditContext, record
from app.modules.bookings.models import Booking
from app.modules.payments.models import Payment, RefundStatus
from app.modules.payments.refunds import enqueue_refund_retry
from app.modules.payments.schemas import RefundIssueOut
from app.modules.users.models import User

logger = get_logger(__name__)

NEEDS_ADMIN = (RefundStatus.FAILED, RefundStatus.PENDING_REVIEW)


async def list_refund_issues(
    session: AsyncSession, status: RefundStatus | None, page: PageParams
) -> tuple[list[RefundIssueOut], int]:
    statuses = [status] if status is not None else list(NEEDS_ADMIN)
    stmt = (
        select(Payment, Booking.client_id, User.email)
        .join(Booking, Booking.id == Payment.booking_id)
        .join(User, User.id == Booking.client_id)
        .where(Payment.refund_status.in_(statuses))
        .order_by(Payment.updated_at.asc(), Payment.id)  # oldest problem first
    )
    total, rows = await _page(session, stmt, page)
    return [
        RefundIssueOut(
            payment_id=p.id,
            booking_id=p.booking_id,
            client_id=client_id,
            client_email=email,
            amount_paise=p.amount_paise,
            refund_amount_paise=p.refund_amount_paise,
            currency=p.currency,
            refund_status=p.refund_status,
            razorpay_payment_id=p.razorpay_payment_id,
            razorpay_refund_id=p.razorpay_refund_id,
            refund_requested_at=p.refund_requested_at,
            updated_at=p.updated_at,
            refund_resolution_note=p.refund_resolution_note,
        )
        for p, client_id, email in rows
    ], total


async def _page(session: AsyncSession, stmt, page: PageParams):  # type: ignore[no-untyped-def]
    _, total = await paginate(session, stmt.with_only_columns(Payment.id), page)
    rows = (await session.execute(stmt.limit(page.page_size).offset(page.offset))).all()
    return total, rows


async def _actionable(session: AsyncSession, payment_id: uuid.UUID) -> Payment:
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        raise NotFoundError("Payment not found")
    if payment.refund_status not in NEEDS_ADMIN:
        raise ConflictError(
            f"Refund is {payment.refund_status.value}; only failed or pending_review refunds "
            "can be retried or marked handled",
            code="refund_not_actionable",
        )
    return payment


async def retry_refund(
    session: AsyncSession, admin: User, payment_id: uuid.UUID, audit: AuditContext
) -> Payment:
    payment = await _actionable(session, payment_id)
    if payment.razorpay_payment_id is None or payment.refund_amount_paise is None:
        raise ConflictError(
            "This refund can't be sent automatically; refund it in the Razorpay dashboard "
            "and mark it handled",
            code="refund_needs_manual_handling",
        )
    previous = payment.refund_status
    payment.refund_status = RefundStatus.REQUESTED
    payment.razorpay_refund_id = None  # the failed refund is dead; a new one will be created
    payment.refund_requested_at = datetime.now(UTC)
    record(
        session,
        audit,
        admin.id,
        AuditAction.REFUND_RETRIED,
        entity_type="payment",
        entity_id=payment.id,
        details={"from": previous.value},
    )
    await session.commit()
    await enqueue_refund_retry(payment.id, payment.refund_requested_at)
    logger.info("refund_retried", payment_id=str(payment.id), admin_id=str(admin.id))
    return payment


async def mark_handled(
    session: AsyncSession, admin: User, payment_id: uuid.UUID, note: str, audit: AuditContext
) -> Payment:
    payment = await _actionable(session, payment_id)
    previous = payment.refund_status
    payment.refund_status = RefundStatus.RESOLVED
    payment.refund_resolved_by_id = admin.id
    payment.refund_resolved_at = datetime.now(UTC)
    payment.refund_resolution_note = note.strip()
    record(
        session,
        audit,
        admin.id,
        AuditAction.REFUND_MARKED_HANDLED,
        entity_type="payment",
        entity_id=payment.id,
        details={"from": previous.value},
    )
    await session.commit()
    logger.info("refund_marked_handled", payment_id=str(payment.id), admin_id=str(admin.id))
    return payment
