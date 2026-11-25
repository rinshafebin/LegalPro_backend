from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status

from app.core.deps import SessionDep
from app.core.errors import AppError
from app.modules.auth.deps import require_roles
from app.modules.payments import service
from app.modules.payments.schemas import (
    CheckoutVerifyIn,
    OrderCreateIn,
    OrderOut,
    PaymentOut,
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
