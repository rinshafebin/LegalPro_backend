"""Account deletion (with a grace period) and personal data export.

Deletion, as agreed:
  - The user confirms with their password, plus a TOTP code if MFA is on. Accounts with
    neither (Google sign-in without MFA) must have logged in within the last 10 minutes.
  - Refused while the user still has obligations: an open case they lead, an upcoming or
    unpaid booking, or a refund that isn't finished.
  - The account is deactivated at once (sessions revoked, sockets closed) and anonymized
    after ACCOUNT_DELETION_GRACE_DAYS by a background job (a daily sweep backs it up).
    Logging back in during the grace period cancels the deletion.
  - Anonymizing wipes personal data (email, password, MFA, names, contact details, profile
    image, notifications, preferences, read state, availability, team memberships) and keeps
    what legal and financial records need: cases (and what the user wrote in them), bookings,
    payments, the audit log and the advocate's Bar Council ID. Those show "Deleted user".
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email import enqueue_email
from app.core.errors import BadRequestError, ConflictError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import TokenPayload, decrypt_str, verify_password
from app.core.storage import get_storage
from app.modules.advocates.models import AdvocateProfile, AdvocateTeam, TeamMember
from app.modules.audit.models import AuditAction
from app.modules.audit.service import AuditContext, record
from app.modules.auth import tokens
from app.modules.auth.models import PasswordResetCode
from app.modules.bookings.models import AvailabilityRule, Booking, BookingStatus
from app.modules.cases.models import Case, CaseStatus
from app.modules.chat.models import ChatReadState
from app.modules.clients.models import ClientProfile
from app.modules.notifications.models import Notification, NotificationPreference
from app.modules.payments.models import Payment, RefundStatus
from app.modules.users.models import User, UserRole
from app.workers.queue import get_job_queue

logger = get_logger(__name__)

DELETED_NAME = "Deleted user"
RECENT_LOGIN = timedelta(minutes=10)
UNFINISHED_REFUNDS = (RefundStatus.REQUESTED, RefundStatus.FAILED, RefundStatus.PENDING_REVIEW)


def utcnow() -> datetime:
    return datetime.now(UTC)


def deletion_job_id(user_id: uuid.UUID, scheduled_for: datetime) -> str:
    return f"user:{user_id}:delete:{int(scheduled_for.timestamp())}"


# ---------------------------------------------------------------- checks


async def blockers(session: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Obligations that must be resolved first. Empty dict = free to delete."""
    now = utcnow()
    open_cases = await session.scalar(
        select(func.count())
        .select_from(Case)
        .where(Case.lead_advocate_id == user_id, Case.status != CaseStatus.CLOSED)
    )
    upcoming = await session.scalar(
        select(func.count())
        .select_from(Booking)
        .where(
            or_(Booking.client_id == user_id, Booking.advocate_id == user_id),
            or_(
                (Booking.status == BookingStatus.CONFIRMED) & (Booking.end_at > now),
                (Booking.status == BookingStatus.PENDING_PAYMENT) & (Booking.expires_at > now),
            ),
        )
    )
    refunds = await session.scalar(
        select(func.count())
        .select_from(Payment)
        .join(Booking, Booking.id == Payment.booking_id)
        .where(
            or_(Booking.client_id == user_id, Booking.advocate_id == user_id),
            Payment.refund_status.in_(UNFINISHED_REFUNDS),
        )
    )
    found = {
        "open_cases_you_lead": int(open_cases or 0),
        "upcoming_bookings": int(upcoming or 0),
        "unfinished_refunds": int(refunds or 0),
    }
    return {k: v for k, v in found.items() if v}


async def _reauthenticate(
    user: User, token: TokenPayload, password: str | None, code: str | None
) -> None:
    if user.password_hash is not None and (
        not password or not await verify_password(password, user.password_hash)
    ):
        raise UnauthorizedError("Password is incorrect", code="invalid_credentials")
    if user.mfa_enabled and user.mfa_secret_encrypted is not None:
        if not code:
            raise BadRequestError("Enter your authenticator code", code="mfa_code_required")
        if not await tokens.verify_totp(user.id, decrypt_str(user.mfa_secret_encrypted), code):
            raise UnauthorizedError("Invalid authentication code", code="invalid_code")
    if user.password_hash is None and not user.mfa_enabled and utcnow() - token.iat > RECENT_LOGIN:
        raise UnauthorizedError(
            "Log in again, then request deletion within 10 minutes", code="recent_login_required"
        )


# ---------------------------------------------------------------- request / cancel


async def request_deletion(
    session: AsyncSession,
    user: User,
    token: TokenPayload,
    password: str | None,
    code: str | None,
    audit: AuditContext,
) -> datetime:
    if user.role is UserRole.ADMIN:
        raise ConflictError(
            "Admin accounts are removed by another admin (deactivate), not self-deleted",
            code="admin_cannot_self_delete",
        )
    await _reauthenticate(user, token, password, code)
    found = await blockers(session, user.id)
    if found:
        raise ConflictError(
            "Resolve these first: close or hand over cases you lead, cancel upcoming "
            "bookings, and wait for refunds to finish",
            code="deletion_blocked",
            details=found,
        )

    scheduled = utcnow() + timedelta(days=get_settings().account_deletion_grace_days)
    user.deletion_scheduled_for = scheduled
    user.is_active = False
    user.token_version += 1
    record(
        session,
        audit,
        user.id,
        AuditAction.USER_DELETION_REQUESTED,
        entity_type="user",
        entity_id=user.id,
        details={"scheduled_for": scheduled.isoformat()},
    )
    await session.commit()
    await tokens.revoke_all_sessions(user.id)  # and close open sockets
    await get_job_queue().enqueue(
        "delete_account",
        job_id=deletion_job_id(user.id, scheduled),
        defer_until=scheduled,
        user_id=str(user.id),
        scheduled_ts=int(scheduled.timestamp()),
    )
    when = f"{scheduled:%d %b %Y}"
    await enqueue_email(
        user.email,
        "Your account will be deleted",
        f"Your account is scheduled for deletion on {when}.\n\n"
        "Changed your mind? Just log in before then and the deletion is cancelled.\n",
    )
    logger.info("account_deletion_requested", user_id=str(user.id))
    return scheduled


def deletion_pending(user: User) -> bool:
    return user.deletion_scheduled_for is not None and user.deleted_at is None


async def cancel_deletion_on_login(session: AsyncSession, user: User) -> None:
    """Called after a fully successful login (password, plus MFA if enabled)."""
    if not deletion_pending(user):
        return
    assert user.deletion_scheduled_for is not None  # noqa: S101 - checked above
    job_id = deletion_job_id(user.id, user.deletion_scheduled_for)
    user.deletion_scheduled_for = None
    user.is_active = True
    record(
        session,
        AuditContext(ip=None, request_id=None),
        user.id,
        AuditAction.USER_DELETION_CANCELLED,
        entity_type="user",
        entity_id=user.id,
    )
    await session.commit()
    await get_job_queue().abort(job_id)
    await enqueue_email(
        user.email,
        "Account deletion cancelled",
        "You logged in, so your account will not be deleted. If this wasn't you, change "
        "your password now.\n",
    )
    logger.info("account_deletion_cancelled", user_id=str(user.id))


# ---------------------------------------------------------------- anonymize (job)


async def run_scheduled_deletion(
    session: AsyncSession, user_id: uuid.UUID, scheduled_ts: int | None = None
) -> str:
    user = await session.get(User, user_id, with_for_update=True)
    if user is None or not deletion_pending(user):
        await session.commit()
        return "skipped"
    assert user.deletion_scheduled_for is not None  # noqa: S101
    if scheduled_ts is not None and int(user.deletion_scheduled_for.timestamp()) != scheduled_ts:
        await session.commit()
        return "skipped"  # rescheduled since this job was queued
    if user.deletion_scheduled_for > utcnow():
        await session.commit()
        return "not_due"
    found = await blockers(session, user.id)
    if found:
        await session.commit()
        logger.error("account_deletion_blocked", user_id=str(user.id), blockers=found)
        return "blocked"
    await anonymize(session, user)
    return "deleted"


async def purge_due_deletions(session: AsyncSession) -> int:
    """Daily safety net in case a deletion job was lost."""
    due = (
        await session.scalars(
            select(User.id).where(
                User.deletion_scheduled_for <= utcnow(), User.deleted_at.is_(None)
            )
        )
    ).all()
    count = 0
    for user_id in due:
        if await run_scheduled_deletion(session, user_id) == "deleted":
            count += 1
    return count


async def anonymize(session: AsyncSession, user: User) -> None:
    old_email = user.email
    avatar_keys: list[str] = []

    client = await session.get(ClientProfile, user.id)
    if client is not None:
        if client.avatar_key:
            avatar_keys.append(client.avatar_key)
        client.full_name = DELETED_NAME
        client.phone = client.address_line1 = client.address_line2 = None
        client.city = client.state = client.pincode = client.avatar_key = None

    advocate = await session.get(AdvocateProfile, user.id)
    if advocate is not None:
        if advocate.avatar_key:
            avatar_keys.append(advocate.avatar_key)
        advocate.full_name = DELETED_NAME
        advocate.phone = advocate.gender = advocate.bio = None
        advocate.address_line1 = advocate.address_line2 = None
        advocate.city = advocate.state = advocate.pincode = advocate.avatar_key = None
        advocate.languages = []
        advocate.consultation_fee = None  # never bookable again
        advocate.is_verified = False
        # bar_council_id is kept: it identifies who handled the retained case records.

    for model, column in (
        (Notification, Notification.user_id),
        (NotificationPreference, NotificationPreference.user_id),
        (ChatReadState, ChatReadState.user_id),
        (AvailabilityRule, AvailabilityRule.advocate_id),
        (PasswordResetCode, PasswordResetCode.user_id),
        (TeamMember, TeamMember.advocate_id),
        (AdvocateTeam, AdvocateTeam.lead_id),
    ):
        await session.execute(delete(model).where(column == user.id))

    now = utcnow()
    user.email = f"deleted-{user.id.hex}@deleted.invalid"
    user.password_hash = None
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.email_verified = False
    user.is_active = False
    user.last_login_at = None
    user.token_version += 1
    user.deleted_at = now
    user.deletion_scheduled_for = None
    record(
        session,
        AuditContext(ip=None, request_id=None),
        None,  # done by the system
        AuditAction.USER_DELETED,
        entity_type="user",
        entity_id=user.id,
    )
    await session.commit()

    for key in avatar_keys:
        await get_storage().delete(key)
    await tokens.revoke_all_sessions(user.id)
    await enqueue_email(  # the last message to the address we just wiped
        old_email,
        "Your account has been deleted",
        "Your personal data has been removed. Records we must keep by law (cases, bookings "
        "and payments) remain, without your name or contact details.\n",
    )
    logger.info("account_anonymized", user_id=str(user.id))


# ---------------------------------------------------------------- export


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


async def export_data(session: AsyncSession, user: User, audit: AuditContext) -> dict[str, Any]:
    profile: dict[str, Any] | None = None
    client = await session.get(ClientProfile, user.id)
    if client is not None:
        profile = {
            "full_name": client.full_name,
            "phone": client.phone,
            "address_line1": client.address_line1,
            "address_line2": client.address_line2,
            "city": client.city,
            "state": client.state,
            "pincode": client.pincode,
            "has_profile_image": client.avatar_key is not None,
        }
    advocate = await session.get(AdvocateProfile, user.id)
    if advocate is not None:
        profile = {
            "full_name": advocate.full_name,
            "phone": advocate.phone,
            "gender": advocate.gender,
            "bio": advocate.bio,
            "bar_council_id": advocate.bar_council_id,
            "enrollment_year": advocate.enrollment_year,
            "experience_years": advocate.experience_years,
            "languages": advocate.languages,
            "consultation_fee": str(advocate.consultation_fee)
            if advocate.consultation_fee is not None
            else None,
            "timezone": advocate.timezone,
            "address_line1": advocate.address_line1,
            "address_line2": advocate.address_line2,
            "city": advocate.city,
            "state": advocate.state,
            "pincode": advocate.pincode,
            "is_verified": advocate.is_verified,
            "has_profile_image": advocate.avatar_key is not None,
        }

    bookings = (
        await session.scalars(
            select(Booking)
            .where(or_(Booking.client_id == user.id, Booking.advocate_id == user.id))
            .order_by(Booking.start_at)
        )
    ).all()
    payments = (
        await session.scalars(
            select(Payment)
            .join(Booking, Booking.id == Payment.booking_id)
            .where(Booking.client_id == user.id)  # what the user paid
            .order_by(Payment.created_at)
        )
    ).all()
    prefs = (
        await session.execute(
            select(NotificationPreference.type, NotificationPreference.email_enabled).where(
                NotificationPreference.user_id == user.id
            )
        )
    ).all()

    record(
        session,
        audit,
        user.id,
        AuditAction.USER_DATA_EXPORTED,
        entity_type="user",
        entity_id=user.id,
    )
    await session.commit()
    return {
        "exported_at": utcnow().isoformat(),
        "account": {
            "id": str(user.id),
            "email": user.email,
            "role": user.role.value,
            "email_verified": user.email_verified,
            "mfa_enabled": user.mfa_enabled,
            "created_at": _iso(user.created_at),
            "last_login_at": _iso(user.last_login_at),
        },
        "profile": profile,
        "bookings": [
            {
                "id": str(b.id),
                "your_role": "client" if b.client_id == user.id else "advocate",
                "start_at": _iso(b.start_at),
                "end_at": _iso(b.end_at),
                "status": b.status.value,
                "fee_amount": str(b.fee_amount),
                "currency": b.currency,
                "cancelled_at": _iso(b.cancelled_at),
                "cancel_reason": b.cancel_reason,
                "completed_at": _iso(b.completed_at),
                "created_at": _iso(b.created_at),
            }
            for b in bookings
        ],
        "payments": [
            {
                "booking_id": str(p.booking_id),
                "amount_paise": p.amount_paise,
                "currency": p.currency,
                "status": p.status.value,
                "captured_at": _iso(p.captured_at),
                "refund_status": p.refund_status.value,
                "refund_amount_paise": p.refund_amount_paise,
                "refunded_at": _iso(p.refunded_at),
            }
            for p in payments
        ],
        "notification_email_preferences": dict(prefs),
    }
