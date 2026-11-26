import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status

from app.core.deps import SessionDep
from app.core.errors import AppError
from app.core.pagination import Page, PageDep, make_page
from app.modules.audit.service import AuditDep, admin_viewed
from app.modules.auth.deps import AdminUser, require_roles
from app.modules.payments import admin, service
from app.modules.payments.models import RefundStatus
from app.modules.payments.schemas import (
    CheckoutVerifyIn,
    OrderCreateIn,
    OrderOut,
    PaymentOut,
    RefundIssueOut,
    RefundResolveIn,
    WebhookOut,
)
from app.modules.users.models import User, UserRole

ClientUser = Annotated[User, Depends(require_roles(UserRole.CLIENT))]
MAX_WEBHOOK_BYTES = 1_000_000

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/orders", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
async def create_order(data: OrderCreateIn, user: ClientUser, session: SessionDep) -> OrderOut:
    """Create the Razorpay order for a pending booking (idempotent per booking)."""
    return await service.create_order(session, user, data.booking_id)


@router.post("/verify", response_model=PaymentOut)
async def verify_checkout(
    data: CheckoutVerifyIn, user: ClientUser, session: SessionDep
) -> PaymentOut:
    """Submit the Razorpay Checkout result; confirms the booking if the signature is valid."""
    return await service.verify_checkout(session, user, data)


@router.post(
    "/webhook",
    response_model=WebhookOut,
    description="Razorpay webhook. Authenticated by `X-Razorpay-Signature` only. "
    "Rejected payloads (unknown order, amount mismatch) are logged and acknowledged "
    "with 200 so Razorpay doesn't retry them; bad signatures get 400.",
)
async def razorpay_webhook(
    request: Request,
    session: SessionDep,
    x_razorpay_signature: Annotated[str | None, Header()] = None,
    x_razorpay_event_id: Annotated[str | None, Header()] = None,
) -> WebhookOut:
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise AppError("Payload too large", code="payload_too_large", status_code=413)
    outcome = await service.handle_webhook(session, body, x_razorpay_signature, x_razorpay_event_id)
    return WebhookOut(status=outcome)


# ---------------------------------------------------------------- admin

admin_router = APIRouter(prefix="/admin/refunds", tags=["admin"])


@admin_router.get(
    "",
    response_model=Page[RefundIssueOut],
    description="Refunds that need a person: `failed` and `pending_review` (oldest first).",
    dependencies=[Depends(admin_viewed)],
)
async def list_refund_issues(
    _: AdminUser,
    session: SessionDep,
    page: PageDep,
    refund_status: RefundStatus | None = None,
) -> Page[RefundIssueOut]:
    if refund_status not in (None, *admin.NEEDS_ADMIN):
        raise AppError("Filter by failed or pending_review", code="invalid_filter")
    rows, total = await admin.list_refund_issues(session, refund_status, page)
    return make_page(rows, total, page)


@admin_router.post(
    "/{payment_id}/retry",
    response_model=PaymentOut,
    description="Queue a fresh refund attempt with Razorpay. Audited.",
)
async def retry_refund(
    payment_id: uuid.UUID, user: AdminUser, session: SessionDep, audit: AuditDep
) -> PaymentOut:
    payment = await admin.retry_refund(session, user, payment_id, audit)
    return await service.payment_out(session, payment)


@admin_router.post(
    "/{payment_id}/mark-handled",
    response_model=PaymentOut,
    description="Close a refund that was dealt with outside the system. Audited.",
)
async def mark_handled(
    payment_id: uuid.UUID,
    data: RefundResolveIn,
    user: AdminUser,
    session: SessionDep,
    audit: AuditDep,
) -> PaymentOut:
    payment = await admin.mark_handled(session, user, payment_id, data.note, audit)
    return await service.payment_out(session, payment)
